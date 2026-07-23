#!/usr/bin/env python3
"""PIR Wake/Sleep Cycle Stress Test - event-driven, dual-threaded console readers."""

import time
import sys
import os
from enum import Enum, auto

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase
from lib.mcu_patterns import CRASH_PATTERNS, HANG_PATTERNS, SLEEP_INDICATOR, is_crash_dump_line

sys.stdout.reconfigure(line_buffering=True)

NUM_CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 50

from testbot4.testbot4_channels import DO_PIR as PIR_DO_CHANNEL, DO_RESET as RESET_DO_CHANNEL
PIR_EVENT_PATTERNS = ["PIR", "pir", "BUTTON", "motion", "wakeup_reason"]
COREDUMP_PATTERN = "Core dump"
ALL_CRASH_PATTERNS = CRASH_PATTERNS + HANG_PATTERNS


class Event(Enum):
    PIR_DETECTED = auto()
    SLEEP_DETECTED = auto()
    CRASH_DETECTED = auto()
    COREDUMP_DETECTED = auto()


class PIRTest(DeviceTestBase):
    _test_name = "pir_wake_sleep"
    _log_dir = "/tmp/pir_test_logs"
    _sleep_timeout = 120
    _reset_recovery_timeout = 120

    def _check_events(self, line, source):
        if source == "MCU":
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP_DETECTED, source, line)
            if any(p in line for p in PIR_EVENT_PATTERNS):
                self.event_callback(Event.PIR_DETECTED, source, line)
            if COREDUMP_PATTERN in line:
                self.event_callback(Event.COREDUMP_DETECTED, source, line)
                return

        if is_crash_dump_line(line):
            self.event_callback(Event.CRASH_DETECTED, source, line)
        else:
            for pattern in ALL_CRASH_PATTERNS:
                if pattern in line:
                    self.event_callback(Event.CRASH_DETECTED, source, line)
                    break

    def run_cycle(self, cycle_num):
        print(f"\n{'='*60}")
        print(f"[CYCLE {cycle_num}/{NUM_CYCLES}]")
        print(f"{'='*60}")

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

        # Init ISP console
        print("  [ISP] Initializing console...")
        self.init_isp_console()

        # Check for coredump or crash that may have already fired
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
            self.save_logs(cycle_num, "crash")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Wait for sleep or crash or coredump or timeout
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
            self.save_logs(cycle_num, "sleep_fail")
            return False

        # Verify sleep
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

    def recovery(self, cycle):
        print(f"\n  [RECOVERY] Cycle {cycle} failed. Resetting immediately...")
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
            print("  [RECOVERY] Timeout - device didn't sleep")
            return False


if __name__ == "__main__":
    test = PIRTest()
    sys.exit(test.run(num_cycles=NUM_CYCLES))
