#!/usr/bin/env python3
"""PIR Wake/Sleep Cycle Stress Test - event-driven, dual-threaded console readers."""

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
LOG_DIR = "/tmp/pir_test_logs"
NUM_CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SLEEP_TIMEOUT = 120
RESET_RECOVERY_TIMEOUT = 120

PIR_DO_CHANNEL = 7
RESET_DO_CHANNEL = 2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcu_patterns import CRASH_PATTERNS, HANG_PATTERNS, SLEEP_INDICATOR, is_crash_dump_line

CRASH_PATTERNS = CRASH_PATTERNS + HANG_PATTERNS  # Merge for backward compat with existing checks
PIR_EVENT_PATTERNS = ["PIR", "pir", "BUTTON", "motion", "wakeup_reason"]


class Event(Enum):
    PIR_DETECTED = auto()
    SLEEP_DETECTED = auto()
    CRASH_DETECTED = auto()
    COREDUMP_DETECTED = auto()
    TIMEOUT = auto()

COREDUMP_PATTERN = "Core dump"
COREDUMP_CAPTURE_TIMEOUT = 60  # seconds to capture full coredump


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
                # Process complete lines only
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    with self.lock:
                        if self.recording:
                            self.lines.append(line)
                    self._check_events(line)
                # If buffer grows large without newline, flush it as a line
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
            if any(p in line for p in PIR_EVENT_PATTERNS):
                self.event_callback(Event.PIR_DETECTED, self.console_name, line)
            if COREDUMP_PATTERN in line:
                self.event_callback(Event.COREDUMP_DETECTED, self.console_name, line)
                return

        if is_crash_dump_line(line):
            self.event_callback(Event.CRASH_DETECTED, self.console_name, line)
        else:
            for pattern in CRASH_PATTERNS:
                if pattern in line:
                    self.event_callback(Event.CRASH_DETECTED, self.console_name, line)
                    break


class PIRTestStateMachine:
    """Event-driven test state machine."""

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
        """Wait for a specific event or timeout. Returns (event, source, line) or None."""
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
        """Non-blocking check for event. Returns (event, source, line) or None."""
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

    def init_isp_console(self):
        """Send Ctrl+C, login if needed, start tail."""
        sock = self.isp.sock
        if not sock:
            return
        try:
            for _ in range(4):
                sock.sendall(b"\x03")
                time.sleep(0.1)
            time.sleep(0.3)
            sock.sendall(b"\r\n")
            time.sleep(1.0)

            # Check for login prompt in recent lines
            lines = self.isp.get_lines()
            recent = " ".join(lines[-5:]) if lines else ""
            if "login:" in recent.lower():
                sock.sendall(b"root\r\n")
                time.sleep(1.0)
                lines = self.isp.get_lines()
                recent = " ".join(lines[-3:]) if lines else ""
                if "assword" in recent:
                    sock.sendall(b"arlo\r\n")
                    time.sleep(1.0)

            sock.sendall(b"tail -f /var/log/messages\r\n")
            time.sleep(0.5)
        except OSError:
            pass

    def verify_sleep(self):
        """Send CR/LF to MCU, expect no response."""
        sock = self.mcu.sock
        if not sock:
            return False
        # Briefly pause recording to avoid capturing our own CR/LF echo
        time.sleep(0.5)
        try:
            for _ in range(5):
                sock.sendall(b"\r\n")
                time.sleep(0.1)
        except OSError:
            return False
        time.sleep(2.0)
        # Check if MCU produced any new lines during verify
        lines_before = len(self.mcu.get_lines())
        time.sleep(1.0)
        lines_after = len(self.mcu.get_lines())
        # If no new meaningful output, device is asleep
        return lines_after == lines_before

    def press_button(self, channel, duration=0.3):
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
        isp_lines = self.isp.get_lines()
        if mcu_lines:
            path = os.path.join(LOG_DIR, f"pir_cycle_{cycle}_{label}_mcu_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(mcu_lines))
            print(f"  [SAVED] {path}")
        if isp_lines:
            path = os.path.join(LOG_DIR, f"pir_cycle_{cycle}_{label}_isp_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [SAVED] {path}")

    def run_cycle(self, cycle_num):
        print(f"\n{'='*60}")
        print(f"[CYCLE {cycle_num}/{NUM_CYCLES}]")
        print(f"{'='*60}")

        # Clear state, start recording on both
        self.clear_events()
        self.mcu.start_recording()
        self.isp.start_recording()

        # Trigger PIR: 3 short pulses
        print("  [PIR] Triggering PIR (DO7, 3x 1s pulses)...")
        for i in range(3):
            self.press_button(PIR_DO_CHANNEL, 1)
            time.sleep(0.5)

        # Wait for PIR event (5s)
        print("  [MCU] Waiting for PIR event...")
        pir = self.wait_for_event(Event.PIR_DETECTED, timeout=5)
        if pir:
            print(f"    [PIR] {pir[2][:120]}")
            print("  [OK] PIR event detected")
        else:
            print("  [WARN] No PIR event in 5s")

        # Init ISP console (both readers already running)
        print("  [ISP] Initializing console...")
        self.init_isp_console()

        # Check for coredump or crash that may have already fired
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
            self.save_logs(cycle_num, "crash")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Wait for sleep or crash or coredump or timeout
        print(f"  [MONITOR] Watching for sleep (timeout {SLEEP_TIMEOUT}s)...")
        deadline = time.time() + SLEEP_TIMEOUT
        sleep_seen = False
        crashed = False

        while time.time() < deadline:
            remaining = deadline - time.time()
            self.event_signal.wait(timeout=min(remaining, 1.0))
            self.event_signal.clear()

            # Check coredump first
            coredump = self.check_event(Event.COREDUMP_DETECTED)
            if coredump:
                print(f"    [COREDUMP!] [{coredump[1]}] {coredump[2][:120]}")
                self._capture_coredump(cycle_num)
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False

            # Check crash
            crash = self.check_event(Event.CRASH_DETECTED)
            if crash:
                print(f"    [CRASH!] [{crash[1]}] {crash[2][:120]}")
                crashed = True
                break

            # Check sleep
            sleep = self.check_event(Event.SLEEP_DETECTED)
            if sleep:
                print(f"    [MCU-SLEEP] {sleep[2][:120]}")
                sleep_seen = True
                break

        self.mcu.stop_recording()
        self.isp.stop_recording()

        if crashed:
            self.save_logs(cycle_num, "crash")
            return False

        if not sleep_seen:
            print(f"  [FAIL] Sleep not reached within {SLEEP_TIMEOUT}s")
            self.save_logs(cycle_num, "sleep_fail")
            return False

        # Verify sleep immediately
        print("  [VERIFY] Checking MCU is actually asleep...")
        if self.verify_sleep():
            print("  [OK] Device confirmed asleep")
            self.save_logs(cycle_num, "pass")
            print(f"  [PASS] Cycle {cycle_num} complete, waiting 5s...")
            time.sleep(5)
            return True
        else:
            print("  [FAIL] Device NOT asleep despite sleep indicator")
            self.save_logs(cycle_num, "sleep_fail")
            return False

    def _capture_coredump(self, cycle_num):
        """Wait for full coredump to finish streaming, then save to separate file."""
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

        # Save coredump to separate file
        ts = datetime.now().strftime("%H%M%S")
        mcu_lines = self.mcu.get_lines()
        path = os.path.join(LOG_DIR, f"pir_cycle_{cycle_num}_coredump_mcu_{ts}.log")
        with open(path, "w") as f:
            f.write("\n".join(mcu_lines))
        print(f"  [SAVED] {path} ({len(mcu_lines)} lines)")

        # Also save ISP log
        isp_lines = self.isp.get_lines()
        if isp_lines:
            isp_path = os.path.join(LOG_DIR, f"pir_cycle_{cycle_num}_coredump_isp_{ts}.log")
            with open(isp_path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [SAVED] {isp_path}")

    def recovery(self, cycle_num):
        """Reset device immediately and wait for sleep."""
        print(f"\n  [RECOVERY] Cycle {cycle_num} failed. Resetting immediately...")
        self.press_button(RESET_DO_CHANNEL, 5.0)
        time.sleep(3)

        # Reconnect both consoles (create new threads)
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

        # Wait for sleep indicator
        print(f"  [RECOVERY] Waiting for sleep (up to {RESET_RECOVERY_TIMEOUT}s)...")
        result = self.wait_for_event(Event.SLEEP_DETECTED, timeout=RESET_RECOVERY_TIMEOUT)
        self.mcu.stop_recording()

        if result:
            print("  [RECOVERY] Device sleeping again")
            time.sleep(5)
            return True
        else:
            print("  [RECOVERY] Timeout - device didn't sleep")
            return False

    def run(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        print(f"=== PIR Wake/Sleep Cycle Test ({NUM_CYCLES} cycles) ===")
        print(f"Log directory: {LOG_DIR}")
        print()

        # Connect both consoles
        print("[INIT] Connecting MCU...")
        self.mcu = ConsoleReader("MCU", MCU_HOST, MCU_PORT, self.event_callback)
        self.mcu.connect()
        self.mcu.start()

        print("[INIT] Connecting ISP...")
        self.isp = ConsoleReader("ISP", ISP_HOST, ISP_PORT, self.event_callback)
        self.isp.connect()
        self.isp.start()

        # Drain stale data
        print("[INIT] Draining stale buffers (2s)...")
        time.sleep(2)
        self.mcu.clear_lines()
        self.isp.clear_lines()
        self.clear_events()
        print("[INIT] Ready")

        for cycle in range(1, NUM_CYCLES + 1):
            passed = self.run_cycle(cycle)
            self.results.append(passed)

            if not passed:
                if not self.recovery(cycle):
                    print("  [RECOVERY] Aborting remaining cycles")
                    break

        # Cleanup
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
    sm = PIRTestStateMachine()
    sm.run()
