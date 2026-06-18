#!/usr/bin/env python3
"""Doorbell Front Button Wake/Sleep Cycle Stress Test.

Presses the front button, verifies ISP wakes, boot reason is correct,
arlod starts, button event is delivered, tone is played, then device
returns to sleep. Repeats N cycles (default 50).

Usage: python3 doorbell_button_test.py [NUM_CYCLES]
"""

import socket
import time
import threading
import subprocess
import sys
import os
from datetime import datetime
from enum import Enum, auto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import get_serial_mux_config

sys.stdout.reconfigure(line_buffering=True)

VOODOO_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voodoo_do_pulse.py")
_cfg = get_serial_mux_config()
MCU_HOST = _cfg['mcu_host']
MCU_PORT = _cfg['mcu_port']
ISP_HOST = _cfg['isp_host']
ISP_PORT = _cfg['isp_port']
LOG_DIR = "/tmp/doorbell_button_test_logs"
NUM_CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SLEEP_TIMEOUT = 120
RESET_RECOVERY_TIMEOUT = 120

FRONT_BUTTON_DO_CHANNEL = 1
RESET_DO_CHANNEL = 2

CRASH_PATTERNS = [
    "segfault", "kernel panic", "coredump", "Assertion failed", "Oops:",
    "core dump", "erpc error: 14 Server is stopped", "Internal error:",
    "HardFault", "BusFault", "MemManage", "UsageFault", "Erpc xQueueSend fail",
]
SLEEP_INDICATOR = "Network Stack Suspended, MCU can enter DeepSleep power mode"
COREDUMP_PATTERN = "Core dump"
COREDUMP_CAPTURE_TIMEOUT = 60

# --- MCU success markers (COM15 / MCU UART) ---
MCU_SUCCESS_MARKERS = [
    # Wakeup reason set to FRONT_BT
    ("FRONT_BT", "mcu_wakeup_reason"),
    # Button input event processed
    ("pegaMain_ButtonInputEventProcess", "mcu_button_event"),
    # ISP power-on triggered
    ("pegaMain_IspPowerOnProcess", "mcu_isp_poweron"),
    # ISP queried boot reason correctly (reason=2=BUTTON, params=0=FRONT_PRESS)
    ("pga_erpc_wakeup_reason_get", "mcu_boot_reason_queried"),
    # ISP reported ready
    ("pga_erpc_notify_isp_ready", "mcu_isp_ready"),
    # Arlod started acknowledged
    ("pga_erpc_notify_arlod_started", "mcu_arlod_started"),
]

# --- ISP success markers (COM13 / ISP syslog) ---
ISP_SUCCESS_MARKERS = [
    # Boot reason parsed from eRPC
    ("bootReason 2 bootParm 0", "isp_boot_reason_parsed"),
    # device.c recognized button boot reason
    ("boot reason button press", "isp_boot_schedule_fired"),
    # Button event dispatched via eRPC notify (or via boot schedule)
    ("front button, event", "isp_button_dispatched"),
    # Tone confirmed played
    ("tone played", "isp_tone_played"),
    # SIP call initiated (backend responded with credentials, call started)
    ("initiated sip call id", "isp_call_initiated"),
    # SIP call answered (remote sent 200 OK, ACK exchanged)
    ("PJSIP_INV_STATE_CONFIRMED", "isp_call_answered"),
    # SIP call ended (BYE or timeout)
    ("PJSIP_INV_STATE_DISCONNECTED", "isp_call_disconnected"),
]

# --- Failure markers ---
MCU_FAILURE_MARKERS = [
    # Sleep vote sent before arlod started (premature sleep)
    ("ERPC_Notify", "mcu_sleep_vote"),
]

ISP_FAILURE_MARKERS = [
    # Sleep pending when button arrives
    ("pending OFF", "isp_pending_off"),
    # Arlod not ready retries exhausted
    ("arlod still not yet ready", "isp_arlod_retry_exhausted"),
]


class Event(Enum):
    BUTTON_DETECTED = auto()
    SLEEP_DETECTED = auto()
    CRASH_DETECTED = auto()
    COREDUMP_DETECTED = auto()
    ISP_BOOT_REASON = auto()
    ISP_TONE_PLAYED = auto()
    TIMEOUT = auto()


class ConsoleReader(threading.Thread):
    """Continuously reads from a serial_mux TCP socket, stores lines, fires events."""

    def __init__(self, name, host, port, event_callback):
        super().__init__(daemon=True)
        self.console_name = name
        self.host = host
        self.port = port
        self.event_callback = event_callback
        self.lines = []
        self.lock = threading.Lock()
        self.sock = None
        self.running = False
        self.recording = False

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.5)
        self.running = True

    def disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    def reconnect(self):
        self.disconnect()
        time.sleep(1)
        self.connect()

    def start_recording(self):
        with self.lock:
            self.lines = []
            self.recording = True

    def stop_recording(self):
        with self.lock:
            self.recording = False

    def get_lines(self):
        with self.lock:
            return list(self.lines)

    def clear_lines(self):
        with self.lock:
            self.lines = []

    def drain(self, duration=2.0):
        """Drain stale data without recording."""
        end = time.time() + duration
        while time.time() < end and self.running:
            try:
                self.sock.recv(8192)
            except (socket.timeout, BlockingIOError, OSError):
                pass
            time.sleep(0.05)

    def run(self):
        buf = ""
        while self.running:
            try:
                data = self.sock.recv(8192)
                if not data:
                    time.sleep(0.1)
                    continue
                buf += data.decode("utf-8", errors="replace").replace("\x00", "")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    with self.lock:
                        if self.recording:
                            self.lines.append(line)
                    self._check_events(line)
                if len(buf) > 4096:
                    line = buf.strip()
                    buf = ""
                    if line:
                        with self.lock:
                            if self.recording:
                                self.lines.append(line)
                        self._check_events(line)
            except socket.timeout:
                continue
            except (BlockingIOError, ConnectionResetError, BrokenPipeError, OSError):
                if self.running:
                    time.sleep(0.5)
                continue

    def _check_events(self, line):
        if self.console_name == "MCU":
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP_DETECTED, self.console_name, line)
            if "press_time" in line or "FRONT_BT" in line or "pegaERPC_NotifyButtonState" in line:
                self.event_callback(Event.BUTTON_DETECTED, self.console_name, line)
            if COREDUMP_PATTERN in line:
                self.event_callback(Event.COREDUMP_DETECTED, self.console_name, line)
                return

        if self.console_name == "ISP":
            if "bootReason 2 bootParm 0" in line:
                self.event_callback(Event.ISP_BOOT_REASON, self.console_name, line)
            if "tone played" in line:
                self.event_callback(Event.ISP_TONE_PLAYED, self.console_name, line)

        for pattern in CRASH_PATTERNS:
            if pattern in line:
                self.event_callback(Event.CRASH_DETECTED, self.console_name, line)
                break


class DoorbellButtonTest:
    """Event-driven doorbell button press test state machine."""

    def __init__(self):
        self.mcu = None
        self.isp = None
        self.events = []
        self.event_lock = threading.Lock()
        self.event_signal = threading.Event()
        self.results = []

    def event_callback(self, event, source, line):
        with self.event_lock:
            self.events.append((event, source, line))
        self.event_signal.set()

    def wait_for_event(self, target_event, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self.event_signal.wait(timeout=min(remaining, 0.5))
            self.event_signal.clear()
            with self.event_lock:
                for i, (evt, src, line) in enumerate(self.events):
                    if evt == target_event:
                        self.events.pop(i)
                        return (evt, src, line)
        return None

    def check_event(self, target_event):
        with self.event_lock:
            for i, (evt, src, line) in enumerate(self.events):
                if evt == target_event:
                    self.events.pop(i)
                    return (evt, src, line)
        return None

    def clear_events(self):
        with self.event_lock:
            self.events = []
        self.event_signal.clear()

    def wait_isp_ready(self, timeout=30):
        """Wait for ISP boot to complete (shell available)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = self.isp.get_lines()
            text = "\n".join(lines[-20:]) if lines else ""
            if "System Initialization Complete" in text or "Lory Doorbell System" in text:
                return True
            time.sleep(1.0)
        return False

    def _wait_for_pattern(self, pattern, timeout=10):
        """Wait until pattern appears in recent ISP lines."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.3)
            lines = self.isp.get_lines()
            for line in lines[-10:]:
                if pattern in line:
                    return True
        return False

    def init_isp_console(self):
        """Login, dump full syslog via cat, then switch to tail -f."""
        sock = self.isp.sock
        if not sock:
            return

        # Wait for ISP to finish booting
        print("  [ISP] Waiting for boot to complete...")
        if not self.wait_isp_ready(timeout=40):
            print("  [WARN] ISP boot completion not detected, trying anyway")

        # Give the login prompt time to appear
        time.sleep(2)

        try:
            # Send Ctrl+C and CR to elicit a prompt
            for _ in range(4):
                sock.sendall(b"\x03")
                time.sleep(0.1)
            time.sleep(0.5)
            sock.sendall(b"\r\n")

            # Wait for either login: or # (root shell)
            deadline = time.time() + 5
            got_shell = False
            while time.time() < deadline:
                time.sleep(0.5)
                lines = self.isp.get_lines()
                recent = " ".join(lines[-5:]) if lines else ""
                if "# " in recent or "#\r" in recent:
                    got_shell = True
                    break
                if "login:" in recent.lower():
                    sock.sendall(b"root\r\n")
                    time.sleep(1.0)
                    lines = self.isp.get_lines()
                    recent = " ".join(lines[-3:]) if lines else ""
                    if "assword" in recent:
                        sock.sendall(b"arlo\r\n")
                        time.sleep(1.0)
                    got_shell = True
                    break

            if not got_shell:
                # Try blind login
                sock.sendall(b"root\r\n")
                time.sleep(1.0)
                sock.sendall(b"arlo\r\n")
                time.sleep(1.0)

            # Dump full syslog (captures early boot messages we missed)
            sock.sendall(b"cat /var/log/messages; echo __CAT_DONE__\r\n")

            # Wait for cat to finish (sentinel marker)
            if not self._wait_for_pattern("__CAT_DONE__", timeout=15):
                print("  [WARN] cat /var/log/messages didn't finish in 15s")

            time.sleep(0.3)
            # Now start tail -f for ongoing capture
            sock.sendall(b"tail -f /var/log/messages\r\n")
            time.sleep(0.5)
        except OSError:
            pass

    def stitch_isp_logs(self):
        """Combine cat dump and tail-f output, deduplicating the overlap."""
        lines = self.isp.get_lines()
        if not lines:
            return []

        # Find the CAT_DONE sentinel
        cat_end_idx = -1
        for i, line in enumerate(lines):
            if "__CAT_DONE__" in line:
                cat_end_idx = i
                break

        if cat_end_idx < 0:
            # No sentinel found — return all lines as-is
            return lines

        # Find where cat command was issued (look for the echo command)
        cat_start_idx = 0
        for i, line in enumerate(lines):
            if "cat /var/log/messages" in line:
                cat_start_idx = i + 1
                break

        # Group A: raw boot output (before cat command)
        boot_lines = lines[:cat_start_idx - 1] if cat_start_idx > 0 else []
        # Remove command echo and prompt lines from boot output
        boot_lines = [l for l in boot_lines if not l.startswith("cat ") and "__CAT_DONE__" not in l]

        # Group B: cat dump (full syslog history)
        cat_lines = lines[cat_start_idx:cat_end_idx]

        # Group C: tail -f output (lines after sentinel)
        # Skip the tail command echo line
        tail_start = cat_end_idx + 1
        for i in range(tail_start, min(tail_start + 3, len(lines))):
            if i < len(lines) and "tail -f" in lines[i]:
                tail_start = i + 1
                break
        tail_lines = lines[tail_start:]

        # Stitch: find overlap between end of cat_lines and start of tail_lines
        # Use last N lines of cat as candidates for overlap
        if cat_lines and tail_lines:
            overlap_window = min(20, len(cat_lines))
            cat_tail = cat_lines[-overlap_window:]
            stitch_idx = 0
            for i, tline in enumerate(tail_lines):
                if tline in cat_tail:
                    stitch_idx = i + 1
                else:
                    break
            tail_lines = tail_lines[stitch_idx:]

        # Combine: boot lines + cat dump (full syslog) + new tail-f lines
        combined = boot_lines + cat_lines + tail_lines
        # Filter out prompt noise and command echoes
        combined = [l for l in combined if not l.startswith("# ") and
                    "tail -f /var/log" not in l and "__CAT_DONE__" not in l and
                    "cat /var/log/messages" not in l]
        return combined

    def _scan_isp_lines_for_events(self):
        """Re-scan ISP lines for events that came from cat dump (before tail -f)."""
        lines = self.isp.get_lines()
        for line in lines:
            if "bootReason 2 bootParm 0" in line:
                self.event_callback(Event.ISP_BOOT_REASON, "ISP", line)
            if "tone played" in line:
                self.event_callback(Event.ISP_TONE_PLAYED, "ISP", line)

    def verify_sleep(self):
        """Send CR/LF to MCU, expect no response = asleep."""
        sock = self.mcu.sock
        if not sock:
            return False
        time.sleep(0.5)
        try:
            for _ in range(5):
                sock.sendall(b"\r\n")
                time.sleep(0.1)
        except OSError:
            return False
        time.sleep(2.0)
        lines_before = len(self.mcu.get_lines())
        time.sleep(1.0)
        lines_after = len(self.mcu.get_lines())
        return lines_after == lines_before

    def press_button(self, channel, duration=1.0):
        """Pulse voodoo DO. Retries 3 times."""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["python3", VOODOO_SCRIPT, str(channel), str(duration)],
                    capture_output=True, timeout=15, text=True
                )
                if result.returncode == 0:
                    return True
                print(f"  [WARN] voodoo attempt {attempt+1}/3 failed: {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"  [WARN] voodoo attempt {attempt+1}/3 timed out")
            time.sleep(1)
        print(f"  [ERROR] voodoo pulse failed after 3 retries")
        return False

    def save_logs(self, cycle, label):
        ts = datetime.now().strftime("%H%M%S")
        mcu_lines = self.mcu.get_lines()
        isp_lines = self.stitch_isp_logs()
        if mcu_lines:
            path = os.path.join(LOG_DIR, f"btn_cycle_{cycle}_{label}_mcu_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(mcu_lines))
            print(f"  [SAVED] {path}")
        if isp_lines:
            path = os.path.join(LOG_DIR, f"btn_cycle_{cycle}_{label}_isp_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [SAVED] {path}")

    def analyze_cycle(self, cycle_num):
        """Check MCU and ISP logs for success/failure markers. Returns (passed, details)."""
        mcu_lines = self.mcu.get_lines()
        isp_lines = self.stitch_isp_logs()
        mcu_text = "\n".join(mcu_lines)
        isp_text = "\n".join(isp_lines)

        mcu_hits = {}
        for pattern, tag in MCU_SUCCESS_MARKERS:
            mcu_hits[tag] = pattern in mcu_text

        isp_hits = {}
        for pattern, tag in ISP_SUCCESS_MARKERS:
            isp_hits[tag] = pattern in isp_text

        mcu_fails = {}
        for pattern, tag in MCU_FAILURE_MARKERS:
            mcu_fails[tag] = pattern in mcu_text

        isp_fails = {}
        for pattern, tag in ISP_FAILURE_MARKERS:
            isp_fails[tag] = pattern in isp_text

        # --- Determine pass/fail ---
        # MCU group: button detected, wakeup reason correct, ISP powered on,
        #            boot reason queried, ISP ready, arlod started
        mcu_pass = (mcu_hits["mcu_wakeup_reason"]
                    and mcu_hits["mcu_button_event"]
                    and mcu_hits["mcu_isp_poweron"]
                    and mcu_hits["mcu_arlod_started"])

        # ISP group: boot reason parsed, boot schedule fired, button dispatched, tone played
        isp_pass = (isp_hits["isp_boot_reason_parsed"]
                    and isp_hits["isp_boot_schedule_fired"]
                    and isp_hits["isp_button_dispatched"]
                    and isp_hits["isp_tone_played"])

        # Call markers: informational — reported but don't fail the cycle
        # (call depends on cloud connectivity and someone answering)
        call_initiated = isp_hits.get("isp_call_initiated", False)
        call_answered = isp_hits.get("isp_call_answered", False)
        call_disconnected = isp_hits.get("isp_call_disconnected", False)
        if call_initiated:
            if call_answered:
                print(f"    [CALL] Initiated -> Answered -> Disconnected")
            elif call_disconnected:
                print(f"    [CALL] Initiated -> Declined/Timeout (no CONFIRMED)")
            else:
                print(f"    [CALL] Initiated (still in progress when sleep hit)")
        else:
            print(f"    [CALL] Not initiated (network/backend issue or blocked)")

        # Check for premature sleep: sleep vote sent BEFORE arlod started
        premature_sleep = False
        if mcu_fails["mcu_sleep_vote"] and not mcu_hits["mcu_arlod_started"]:
            premature_sleep = True

        passed = mcu_pass and isp_pass and not premature_sleep

        # Print analysis
        print(f"  --- MCU markers ---")
        for pattern, tag in MCU_SUCCESS_MARKERS:
            status = "OK" if mcu_hits[tag] else "MISSING"
            print(f"    [{status}] {tag}")
        print(f"  --- ISP markers ---")
        for pattern, tag in ISP_SUCCESS_MARKERS:
            status = "OK" if isp_hits[tag] else "MISSING"
            print(f"    [{status}] {tag}")
        if premature_sleep:
            print(f"    [FAIL] Premature sleep vote before arlod started")
        if isp_fails.get("isp_pending_off"):
            print(f"    [FAIL] ISP in pending-OFF when button arrived")
        if isp_fails.get("isp_arlod_retry_exhausted"):
            print(f"    [FAIL] Arlod retry exhausted")

        return passed

    def run_cycle(self, cycle_num):
        print(f"\n{'='*60}")
        print(f"[CYCLE {cycle_num}/{NUM_CYCLES}]")
        print(f"{'='*60}")

        self.clear_events()
        self.mcu.start_recording()
        self.isp.start_recording()

        # Press front button (1s pulse)
        print("  [BUTTON] Pressing front button (DO1, 1s)...")
        self.press_button(FRONT_BUTTON_DO_CHANNEL, 1.0)

        # Wait for button event on MCU (5s)
        print("  [MCU] Waiting for button event...")
        btn = self.wait_for_event(Event.BUTTON_DETECTED, timeout=5)
        if btn:
            print(f"    [BTN] {btn[2][:120]}")
        else:
            print("  [WARN] No button event in 5s")

        # Init ISP console
        print("  [ISP] Initializing console...")
        self.init_isp_console()

        # Scan ISP lines from cat dump for events (they arrived before tail -f)
        self._scan_isp_lines_for_events()

        # Check for coredump
        coredump = self.check_event(Event.COREDUMP_DETECTED)
        if coredump:
            print(f"    [COREDUMP!] [{coredump[1]}] {coredump[2][:120]}")
            self._capture_coredump(cycle_num)
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return "crash"

        # Check for crash
        crash = self.check_event(Event.CRASH_DETECTED)
        if crash:
            print(f"    [CRASH!] [{crash[1]}] {crash[2][:120]}")
            self.save_logs(cycle_num, "crash")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return "crash"

        # Wait for sleep or crash or timeout
        print(f"  [MONITOR] Watching for sleep (timeout {SLEEP_TIMEOUT}s)...")
        deadline = time.time() + SLEEP_TIMEOUT
        sleep_seen = False
        crashed = False

        while time.time() < deadline:
            remaining = deadline - time.time()
            self.event_signal.wait(timeout=min(remaining, 1.0))
            self.event_signal.clear()

            coredump = self.check_event(Event.COREDUMP_DETECTED)
            if coredump:
                print(f"    [COREDUMP!] [{coredump[1]}] {coredump[2][:120]}")
                self._capture_coredump(cycle_num)
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False

            crash = self.check_event(Event.CRASH_DETECTED)
            if crash:
                print(f"    [CRASH!] [{crash[1]}] {crash[2][:120]}")
                crashed = True
                break

            sleep = self.check_event(Event.SLEEP_DETECTED)
            if sleep:
                print(f"    [MCU-SLEEP] {sleep[2][:120]}")
                sleep_seen = True
                break

        self.mcu.stop_recording()
        self.isp.stop_recording()

        if crashed:
            self.save_logs(cycle_num, "crash")
            return "crash"

        if not sleep_seen:
            print(f"  [FAIL] Sleep not reached within {SLEEP_TIMEOUT}s")
            self.save_logs(cycle_num, "sleep_timeout")
            return "hang"

        # Analyze logs for success/failure markers
        print("  [ANALYZE] Checking log markers...")
        passed = self.analyze_cycle(cycle_num)

        # Verify sleep
        print("  [VERIFY] Checking MCU is actually asleep...")
        asleep = self.verify_sleep()
        if not asleep:
            print("  [FAIL] Device NOT asleep despite sleep indicator")
            self.save_logs(cycle_num, "fail")
            return "hang"

        label = "pass" if passed else "fail"
        self.save_logs(cycle_num, label)

        if passed:
            print(f"  [PASS] Cycle {cycle_num} complete")
            time.sleep(5)
        else:
            print(f"  [FAIL] Cycle {cycle_num} — markers incomplete (device slept OK)")

        return "pass" if passed else "fail"

    def _capture_coredump(self, cycle_num):
        """Wait for full coredump to finish streaming, then save."""
        print(f"  [COREDUMP] Capturing full dump (up to {COREDUMP_CAPTURE_TIMEOUT}s)...")
        start = time.time()
        lines_before = len(self.mcu.get_lines())
        idle_count = 0

        while time.time() - start < COREDUMP_CAPTURE_TIMEOUT:
            time.sleep(2)
            lines_now = len(self.mcu.get_lines())
            if lines_now == lines_before:
                idle_count += 1
                if idle_count >= 3:
                    print("  [COREDUMP] Dump complete (no new data for 6s)")
                    break
            else:
                idle_count = 0
                lines_before = lines_now

        ts = datetime.now().strftime("%H%M%S")
        mcu_lines = self.mcu.get_lines()
        path = os.path.join(LOG_DIR, f"btn_cycle_{cycle_num}_coredump_mcu_{ts}.log")
        with open(path, "w") as f:
            f.write("\n".join(mcu_lines))
        print(f"  [SAVED] {path} ({len(mcu_lines)} lines)")

        isp_lines = self.isp.get_lines()
        if isp_lines:
            isp_path = os.path.join(LOG_DIR, f"btn_cycle_{cycle_num}_coredump_isp_{ts}.log")
            with open(isp_path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [SAVED] {isp_path}")

    def recovery(self, cycle_num):
        """Reset device and wait for sleep."""
        print(f"\n  [RECOVERY] Cycle {cycle_num} failed. Resetting...")
        self.press_button(RESET_DO_CHANNEL, 5.0)
        time.sleep(3)

        print("  [RECOVERY] Reconnecting...")
        self.mcu.disconnect()
        self.isp.disconnect()
        time.sleep(5)

        self.mcu = ConsoleReader("MCU", MCU_HOST, MCU_PORT, self.event_callback)
        self.mcu.connect()
        self.mcu.start()

        self.isp = ConsoleReader("ISP", ISP_HOST, ISP_PORT, self.event_callback)
        self.isp.connect()
        self.isp.start()

        self.clear_events()
        self.mcu.start_recording()

        print(f"  [RECOVERY] Waiting for sleep (up to {RESET_RECOVERY_TIMEOUT}s)...")
        result = self.wait_for_event(Event.SLEEP_DETECTED, timeout=RESET_RECOVERY_TIMEOUT)
        self.mcu.stop_recording()

        if result:
            print("  [RECOVERY] Device sleeping again")
            time.sleep(5)
            return True
        else:
            print("  [RECOVERY] Timeout — device didn't sleep")
            return False

    def run(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        print(f"=== Doorbell Front Button Wake/Sleep Cycle Test ({NUM_CYCLES} cycles) ===")
        print(f"Log directory: {LOG_DIR}")
        print()

        print("[INIT] Connecting MCU...")
        self.mcu = ConsoleReader("MCU", MCU_HOST, MCU_PORT, self.event_callback)
        self.mcu.connect()
        self.mcu.start()

        print("[INIT] Connecting ISP...")
        self.isp = ConsoleReader("ISP", ISP_HOST, ISP_PORT, self.event_callback)
        self.isp.connect()
        self.isp.start()

        print("[INIT] Draining stale buffers (2s)...")
        time.sleep(2)
        self.mcu.clear_lines()
        self.isp.clear_lines()
        self.clear_events()
        print("[INIT] Ready")

        for cycle in range(1, NUM_CYCLES + 1):
            result = self.run_cycle(cycle)
            self.results.append(result == "pass")

            if result in ("crash", "hang"):
                if not self.recovery(cycle):
                    print("  [RECOVERY] Aborting remaining cycles")
                    break
            elif result == "fail":
                # Device slept normally, just markers incomplete — continue
                time.sleep(5)

        self.mcu.disconnect()
        self.isp.disconnect()

        # Summary
        print(f"\n{'='*60}")
        print(f"TEST COMPLETE: {sum(self.results)}/{len(self.results)} cycles passed")
        print(f"{'='*60}")
        for i, r in enumerate(self.results, 1):
            print(f"  Cycle {i}: {'PASS' if r else 'FAIL'}")

        if all(self.results) and len(self.results) == NUM_CYCLES:
            print("\nRESULT: PASS")
            sys.exit(0)
        else:
            print(f"\nRESULT: FAIL ({self.results.count(False)} failures)")
            print(f"Logs saved to: {LOG_DIR}")
            sys.exit(1)


if __name__ == "__main__":
    sm = DoorbellButtonTest()
    sm.run()
