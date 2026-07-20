#!/usr/bin/env python3
"""Doorbell Front Button Wake/Sleep Cycle Stress Test.

Presses the front button, verifies ISP wakes, boot reason is correct,
arlod starts, button event is delivered, tone is played, then device
returns to sleep. Repeats N cycles (default 50).

Usage: python3 doorbell_button_test.py [NUM_CYCLES]
"""

import time
import sys
import os
from datetime import datetime
from enum import Enum, auto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import DeviceTestBase, isp_init_console

sys.stdout.reconfigure(line_buffering=True)

NUM_CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 50

from voodoo_channels import DO_FRONT as FRONT_BUTTON_DO_CHANNEL, DO_RESET as RESET_DO_CHANNEL

CRASH_PATTERNS = [
    "segfault", "kernel panic", "coredump", "Assertion failed", "Oops:",
    "core dump", "erpc error: 14 Server is stopped", "Internal error:",
    "HardFault", "BusFault", "MemManage", "UsageFault", "Erpc xQueueSend fail",
]
SLEEP_INDICATOR = "Network Stack Suspended, MCU can enter DeepSleep power mode"
COREDUMP_PATTERN = "Core dump"

# --- MCU success markers ---
MCU_SUCCESS_MARKERS = [
    ("FRONT_BT", "mcu_wakeup_reason"),
    ("pegaMain_ButtonInputEventProcess", "mcu_button_event"),
    ("pegaMain_IspPowerOnProcess", "mcu_isp_poweron"),
    ("pga_erpc_wakeup_reason_get", "mcu_boot_reason_queried"),
    ("pga_erpc_notify_isp_ready", "mcu_isp_ready"),
    ("pga_erpc_notify_arlod_started", "mcu_arlod_started"),
]

# --- ISP success markers ---
ISP_SUCCESS_MARKERS = [
    ("bootReason 2 bootParm 0", "isp_boot_reason_parsed"),
    ("boot reason button press", "isp_boot_schedule_fired"),
    ("front button, event", "isp_button_dispatched"),
    ("tone played", "isp_tone_played"),
    ("initiated sip call id", "isp_call_initiated"),
    ("PJSIP_INV_STATE_CONFIRMED", "isp_call_answered"),
    ("PJSIP_INV_STATE_DISCONNECTED", "isp_call_disconnected"),
]

# --- Failure markers ---
MCU_FAILURE_MARKERS = [
    ("ERPC_Notify", "mcu_sleep_vote"),
]

ISP_FAILURE_MARKERS = [
    ("pending OFF", "isp_pending_off"),
    ("arlod still not yet ready", "isp_arlod_retry_exhausted"),
]


class Event(Enum):
    BUTTON_DETECTED = auto()
    SLEEP_DETECTED = auto()
    CRASH_DETECTED = auto()
    COREDUMP_DETECTED = auto()
    ISP_BOOT_REASON = auto()
    ISP_TONE_PLAYED = auto()


class DoorbellButtonTest(DeviceTestBase):
    _test_name = "doorbell_button"
    _log_dir = "/tmp/doorbell_button_test_logs"
    _sleep_timeout = 120
    _reset_recovery_timeout = 120
    _coredump_capture_timeout = 60

    def _check_events(self, line, source):
        if source == "MCU":
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP_DETECTED, source, line)
            if "press_time" in line or "FRONT_BT" in line or "pegaERPC_NotifyButtonState" in line:
                self.event_callback(Event.BUTTON_DETECTED, source, line)
            if COREDUMP_PATTERN in line:
                self.event_callback(Event.COREDUMP_DETECTED, source, line)
                return

        if source == "ISP":
            if "bootReason 2 bootParm 0" in line:
                self.event_callback(Event.ISP_BOOT_REASON, source, line)
            if "tone played" in line:
                self.event_callback(Event.ISP_TONE_PLAYED, source, line)

        for pattern in CRASH_PATTERNS:
            if pattern in line:
                self.event_callback(Event.CRASH_DETECTED, source, line)
                break

    def _init_isp_with_cat(self):
        """Login, dump full syslog via cat, then switch to tail -f."""
        sock = self.isp.sock
        if not sock:
            return

        print("  [ISP] Waiting for boot to complete...")
        time.sleep(10)

        try:
            for _ in range(4):
                sock.sendall(b"\x03")
                time.sleep(0.1)
            time.sleep(0.5)
            sock.sendall(b"\r\n")

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
                sock.sendall(b"root\r\n")
                time.sleep(1.0)
                sock.sendall(b"arlo\r\n")
                time.sleep(1.0)

            sock.sendall(b"cat /var/log/messages; echo __CAT_DONE__\r\n")
            self._wait_for_pattern("__CAT_DONE__", timeout=15)
            time.sleep(0.3)
            sock.sendall(b"tail -f /var/log/messages\r\n")
            time.sleep(0.5)
        except OSError:
            pass

    def _wait_for_pattern(self, pattern, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.3)
            lines = self.isp.get_lines()
            for line in lines[-10:]:
                if pattern in line:
                    return True
        return False

    def _stitch_isp_logs(self):
        """Combine cat dump and tail-f output, deduplicating overlap."""
        lines = self.isp.get_lines()
        if not lines:
            return []

        cat_end_idx = -1
        for i, line in enumerate(lines):
            if "__CAT_DONE__" in line:
                cat_end_idx = i
                break

        if cat_end_idx < 0:
            return lines

        cat_start_idx = 0
        for i, line in enumerate(lines):
            if "cat /var/log/messages" in line:
                cat_start_idx = i + 1
                break

        boot_lines = lines[:cat_start_idx - 1] if cat_start_idx > 0 else []
        boot_lines = [l for l in boot_lines if not l.startswith("cat ") and "__CAT_DONE__" not in l]

        cat_lines = lines[cat_start_idx:cat_end_idx]

        tail_start = cat_end_idx + 1
        for i in range(tail_start, min(tail_start + 3, len(lines))):
            if i < len(lines) and "tail -f" in lines[i]:
                tail_start = i + 1
                break
        tail_lines = lines[tail_start:]

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

        combined = boot_lines + cat_lines + tail_lines
        combined = [l for l in combined if not l.startswith("# ") and
                    "tail -f /var/log" not in l and "__CAT_DONE__" not in l and
                    "cat /var/log/messages" not in l]
        return combined

    def _scan_isp_lines_for_events(self):
        """Re-scan ISP lines for events from cat dump (before tail -f)."""
        lines = self.isp.get_lines()
        for line in lines:
            if "bootReason 2 bootParm 0" in line:
                self.event_callback(Event.ISP_BOOT_REASON, "ISP", line)
            if "tone played" in line:
                self.event_callback(Event.ISP_TONE_PLAYED, "ISP", line)

    def analyze_cycle(self):
        """Check MCU and ISP logs for success/failure markers. Returns passed bool."""
        mcu_lines = self.mcu.get_lines()
        isp_lines = self._stitch_isp_logs()
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

        mcu_pass = (mcu_hits["mcu_wakeup_reason"]
                    and mcu_hits["mcu_button_event"]
                    and mcu_hits["mcu_isp_poweron"]
                    and mcu_hits["mcu_arlod_started"])

        isp_pass = (isp_hits["isp_boot_reason_parsed"]
                    and isp_hits["isp_boot_schedule_fired"]
                    and isp_hits["isp_button_dispatched"]
                    and isp_hits["isp_tone_played"])

        # Call markers: informational only
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

        premature_sleep = mcu_fails["mcu_sleep_vote"] and not mcu_hits["mcu_arlod_started"]
        passed = mcu_pass and isp_pass and not premature_sleep

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

    def save_logs(self, cycle, label):
        """Override to use stitched ISP logs."""
        os.makedirs(self._log_dir, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        mcu_lines = self.mcu.get_lines() if self.mcu else []
        isp_lines = self._stitch_isp_logs()
        if mcu_lines:
            path = os.path.join(self._log_dir, f"btn_cycle_{cycle}_{label}_mcu_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(mcu_lines))
            print(f"  [SAVED] {path}")
        if isp_lines:
            path = os.path.join(self._log_dir, f"btn_cycle_{cycle}_{label}_isp_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [SAVED] {path}")

    def run_cycle(self, cycle_num):
        print(f"\n{'='*60}")
        print(f"[CYCLE {cycle_num}/{NUM_CYCLES}]")
        print(f"{'='*60}")

        self.clear_events()
        self.mcu.start_recording()
        self.isp.start_recording()

        # Press front button
        print("  [BUTTON] Pressing front button (DO1, 1s)...")
        self.press_button(FRONT_BUTTON_DO_CHANNEL, 1.0)

        # Wait for button event on MCU
        print("  [MCU] Waiting for button event...")
        btn = self.wait_for_event(Event.BUTTON_DETECTED, timeout=5)
        if btn:
            print(f"    [BTN] {btn[2][:120]}")
        else:
            print("  [WARN] No button event in 5s")

        # Init ISP console with cat dump
        print("  [ISP] Initializing console...")
        self._init_isp_with_cat()
        self._scan_isp_lines_for_events()

        # Check for coredump
        coredump = self.check_event(Event.COREDUMP_DETECTED)
        if coredump:
            print(f"    [COREDUMP!] [{coredump[1]}] {coredump[2][:120]}")
            self.capture_coredump(cycle_num)
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Check for crash
        crash = self.check_event(Event.CRASH_DETECTED)
        if crash:
            print(f"    [CRASH!] [{crash[1]}] {crash[2][:120]}")
            self.save_logs(cycle_num, "crash")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Wait for sleep or crash or timeout
        print(f"  [MONITOR] Watching for sleep (timeout {self._sleep_timeout}s)...")
        deadline = time.time() + self._sleep_timeout
        sleep_seen = False
        crashed = False

        while time.time() < deadline:
            remaining = deadline - time.time()
            self.event_signal.wait(timeout=min(remaining, 1.0))
            self.event_signal.clear()

            coredump = self.check_event(Event.COREDUMP_DETECTED)
            if coredump:
                print(f"    [COREDUMP!] [{coredump[1]}] {coredump[2][:120]}")
                self.capture_coredump(cycle_num)
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
            return False

        if not sleep_seen:
            print(f"  [FAIL] Sleep not reached within {self._sleep_timeout}s")
            self.save_logs(cycle_num, "sleep_timeout")
            return False

        # Analyze logs for success/failure markers
        print("  [ANALYZE] Checking log markers...")
        passed = self.analyze_cycle()

        # Verify sleep
        print("  [VERIFY] Checking MCU is actually asleep...")
        if not self.verify_sleep():
            print("  [FAIL] Device NOT asleep despite sleep indicator")
            self.save_logs(cycle_num, "fail")
            return False

        label = "pass" if passed else "fail"
        self.save_logs(cycle_num, label)

        if passed:
            print(f"  [PASS] Cycle {cycle_num} complete")
            time.sleep(5)
        else:
            print(f"  [FAIL] Cycle {cycle_num} — markers incomplete (device slept OK)")

        return passed

    def recovery(self, cycle):
        print(f"\n  [RECOVERY] Cycle {cycle} failed. Resetting...")
        self.press_button(RESET_DO_CHANNEL, 5.0)
        time.sleep(3)

        print("  [RECOVERY] Reconnecting...")
        self.reconnect_consoles()
        self.clear_events()
        self.mcu.start_recording()

        print(f"  [RECOVERY] Waiting for sleep (up to {self._reset_recovery_timeout}s)...")
        result = self.wait_for_event(Event.SLEEP_DETECTED, timeout=self._reset_recovery_timeout)
        self.mcu.stop_recording()

        if result:
            print("  [RECOVERY] Device sleeping again")
            time.sleep(5)
            return True
        else:
            print("  [RECOVERY] Timeout — device didn't sleep")
            return False


if __name__ == "__main__":
    test = DoorbellButtonTest()
    sys.exit(test.run(num_cycles=NUM_CYCLES))
