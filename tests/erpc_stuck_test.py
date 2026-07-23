#!/usr/bin/env python3
"""eRPC Stuck Detection Test — reproduces PEGA-1132.

Cycles the device through wake/sleep transitions and detects the stuck state
where MCU prints "eRPC from DOWN to STANDBY" but never progresses to actual
deep sleep ("Network Stack Suspended").

Detection heuristics:
  1. After "eRPC from DOWN to STANDBY", if "Network Stack Suspended" doesn't
     appear within STANDBY_TO_SLEEP_TIMEOUT, the device may be stuck.
  2. If the MCU console is still responsive (echoes CR/LF), it's confirmed
     stuck — awake but not progressing.
  3. If "eRPC from DOWN to STANDBY" repeats 3+ times within a short window
     without sustained sleep, the device is cycling without entering deep sleep.

Usage:
  python3 erpc_stuck_test.py [NUM_CYCLES]       # default 20
  python3 erpc_stuck_test.py 50                  # 50 cycles
"""

import time
import sys
import os
from enum import Enum, auto

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase
from lib.mcu_patterns import (
    CRASH_PATTERNS, HANG_PATTERNS, SLEEP_INDICATOR,
    is_crash_dump_line,
)

sys.stdout.reconfigure(line_buffering=True)

NUM_CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 20

from testbot4.testbot4_channels import DO_SYNC as SYNC_DO_CHANNEL, DO_RESET as RESET_DO_CHANNEL

ERPC_STANDBY_PATTERN = "eRPC from DOWN to STANDBY"
ERPC_DOWN_PATTERN = "pga_sm_set_rpc_down"
COREDUMP_PATTERN = "Core dump"

# ISP boot messages are expected during wake — only flag actual crashes
MCU_ONLY_CRASH_PATTERNS = [
    "HardFault", "BusFault", "MemManage", "UsageFault",
    "Assertion failed", "Unhandled exception", "abort()",
]
MCU_HANG_PATTERNS_LOCAL = [
    "xQueueSend fail", "Erpc xQueueSend fail",
    "erpc error: 14 Server is stopped",
    "deadlock", "watchdog reset", "WDT expired",
]
# ISP patterns that indicate a CRASH (not normal boot) — only after ISP is up
ISP_UNEXPECTED_REBOOT_PATTERNS = [
    "kernel panic", "Oops:", "segfault",
    "BUG: soft lockup", "Unable to handle kernel",
]

STANDBY_TO_SLEEP_TIMEOUT = 30
WAKE_SETTLE_TIME = 10
SLEEP_TIMEOUT = 180


class Event(Enum):
    SLEEP_DETECTED = auto()
    STANDBY_DETECTED = auto()
    WAKE_DETECTED = auto()
    ISP_BOOTED = auto()
    CRASH_DETECTED = auto()
    COREDUMP_DETECTED = auto()


WAKE_PATTERNS = [
    "pegaDp_sleep_wakeup_by_reason",
    "wakeup_reason",
    "IspPowerOnProcess",
    "pegaERPC_NotifyButtonState",
    "BUTTON",
]

ISP_BOOT_COMPLETE = "start arlod"


class ERPCStuckTest(DeviceTestBase):
    _test_name = "erpc_stuck"
    _log_dir = "/tmp/erpc_stuck_logs"
    _sleep_timeout = SLEEP_TIMEOUT
    _reset_recovery_timeout = 120

    def __init__(self):
        super().__init__()
        self.standby_count = 0
        self.standby_times = []
        self.isp_booted = False

    def _check_events(self, line, source):
        if source == "MCU":
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP_DETECTED, source, line)
            elif ERPC_STANDBY_PATTERN in line:
                self.event_callback(Event.STANDBY_DETECTED, source, line)
            elif any(p in line for p in WAKE_PATTERNS):
                self.event_callback(Event.WAKE_DETECTED, source, line)
            elif COREDUMP_PATTERN in line:
                self.event_callback(Event.COREDUMP_DETECTED, source, line)
                return
            for pattern in MCU_ONLY_CRASH_PATTERNS + MCU_HANG_PATTERNS_LOCAL:
                if pattern in line:
                    self.event_callback(Event.CRASH_DETECTED, source, line)
                    return
            if is_crash_dump_line(line):
                self.event_callback(Event.CRASH_DETECTED, source, line)

        elif source == "ISP":
            if ISP_BOOT_COMPLETE in line:
                self.isp_booted = True
                self.event_callback(Event.ISP_BOOTED, source, line)
            elif self.isp_booted:
                for pattern in ISP_UNEXPECTED_REBOOT_PATTERNS:
                    if pattern in line:
                        self.event_callback(Event.CRASH_DETECTED, source, line)
                        return

    def _check_stuck(self):
        """After STANDBY detected, wait for sleep or detect stuck.

        Returns:
            "sleep"   — device reached deep sleep normally
            "stuck"   — device stuck (responsive but not sleeping)
            "crash"   — crash/coredump detected
            "cycling" — repeated STANDBY without sleeping
            "timeout" — overall timeout expired
        """
        standby_start = time.time()
        deadline = time.time() + STANDBY_TO_SLEEP_TIMEOUT

        while time.time() < deadline:
            remaining = deadline - time.time()
            self.event_signal.wait(timeout=min(remaining, 1.0))
            self.event_signal.clear()

            coredump = self.check_event(Event.COREDUMP_DETECTED)
            if coredump:
                print(f"    [COREDUMP!] {coredump[2][:120]}")
                return "crash"

            crash = self.check_event(Event.CRASH_DETECTED)
            if crash:
                print(f"    [CRASH!] {crash[2][:120]}")
                return "crash"

            sleep = self.check_event(Event.SLEEP_DETECTED)
            if sleep:
                elapsed = time.time() - standby_start
                print(f"    [SLEEP] Reached deep sleep in {elapsed:.1f}s after STANDBY")
                return "sleep"

            standby = self.check_event(Event.STANDBY_DETECTED)
            if standby:
                self.standby_count += 1
                self.standby_times.append(time.time())
                elapsed = time.time() - standby_start
                print(f"    [STANDBY] Repeated ({self.standby_count}x) "
                      f"after {elapsed:.1f}s — device cycling without sleep!")
                if self.standby_count >= 3:
                    return "cycling"
                deadline = time.time() + STANDBY_TO_SLEEP_TIMEOUT

        # Timeout — check if MCU is responsive (stuck awake)
        print(f"    [TIMEOUT] No sleep within {STANDBY_TO_SLEEP_TIMEOUT}s after STANDBY")
        print(f"    [VERIFY] Sending CR/LF to check if MCU is responsive...")
        if not self.verify_sleep(probes=5, interval=0.1, wait_after=2.0):
            print(f"    [STUCK!] MCU is responsive — awake but not progressing to sleep")
            return "stuck"
        else:
            print(f"    [OK] MCU unresponsive — may have slept without log indicator")
            return "sleep"

    def run_cycle(self, cycle_num):
        print(f"\n{'='*60}")
        print(f"[CYCLE {cycle_num}/{NUM_CYCLES}]")
        print(f"{'='*60}")

        self.clear_events()
        self.standby_count = 0
        self.standby_times = []
        self.isp_booted = False
        self.mcu.start_recording()
        self.isp.start_recording()

        # Wake device via SYNC button
        print("  [SYNC] Waking device (DO0, 2s)...")
        self.press_button(SYNC_DO_CHANNEL, 2.0)

        # Wait for wake confirmation
        print("  [WAKE] Waiting for wake event...")
        wake = self.wait_for_event(Event.WAKE_DETECTED, timeout=10)
        if wake:
            print(f"    [OK] {wake[2][:120]}")
        else:
            print("    [WARN] No wake event in 10s (device may already be awake)")

        # Let the device run for a bit (ISP boot, eRPC init, xagent connect, etc.)
        print(f"  [SETTLE] Waiting {WAKE_SETTLE_TIME}s for ISP/eRPC initialization...")
        time.sleep(WAKE_SETTLE_TIME)

        # Check for early crash
        coredump = self.check_event(Event.COREDUMP_DETECTED)
        if coredump:
            print(f"    [COREDUMP!] {coredump[2][:120]}")
            self.capture_coredump(cycle_num)
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        crash = self.check_event(Event.CRASH_DETECTED)
        if crash:
            print(f"    [CRASH!] {crash[2][:120]}")
            self.save_logs(cycle_num, "crash")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Now wait for device to go back to sleep (the critical phase)
        print(f"  [MONITOR] Watching for sleep transition (timeout {SLEEP_TIMEOUT}s)...")
        overall_deadline = time.time() + SLEEP_TIMEOUT
        result = None

        while time.time() < overall_deadline:
            remaining = overall_deadline - time.time()
            if remaining <= 0:
                break

            evt = self.wait_for_any_event(
                [Event.STANDBY_DETECTED, Event.SLEEP_DETECTED,
                 Event.CRASH_DETECTED, Event.COREDUMP_DETECTED],
                timeout=min(remaining, 5.0))

            if not evt:
                continue

            event_type, source, line = evt

            if event_type == Event.COREDUMP_DETECTED:
                print(f"    [COREDUMP!] {line[:120]}")
                self.capture_coredump(cycle_num)
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False

            if event_type == Event.CRASH_DETECTED:
                print(f"    [CRASH!] {line[:120]}")
                self.save_logs(cycle_num, "crash")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False

            if event_type == Event.SLEEP_DETECTED:
                print(f"    [SLEEP] Device reached deep sleep directly")
                result = "sleep"
                break

            if event_type == Event.STANDBY_DETECTED:
                self.standby_count += 1
                self.standby_times.append(time.time())
                print(f"    [STANDBY] 'eRPC from DOWN to STANDBY' detected "
                      f"(count: {self.standby_count})")
                result = self._check_stuck()
                break

        self.mcu.stop_recording()
        self.isp.stop_recording()

        if result is None:
            print(f"  [FAIL] Overall timeout ({SLEEP_TIMEOUT}s) — device never transitioned")
            self.save_logs(cycle_num, "timeout")
            return False

        if result == "sleep":
            # Verify actual sleep
            print("  [VERIFY] Checking MCU is actually asleep...")
            time.sleep(1)
            if self.verify_sleep():
                print("  [PASS] Device confirmed asleep")
                time.sleep(5)
                return True
            else:
                print("  [FAIL] Device NOT asleep despite sleep indicator")
                self.save_logs(cycle_num, "false_sleep")
                return False

        if result == "stuck":
            print("  [FAIL] *** PEGA-1132 REPRODUCED: eRPC stuck at STANDBY ***")
            self.save_logs(cycle_num, "STUCK_REPRO")
            return False

        if result == "cycling":
            window = self.standby_times[-1] - self.standby_times[0]
            print(f"  [FAIL] *** PEGA-1132 REPRODUCED: Cycling {self.standby_count}x "
                  f"in {window:.1f}s without sleeping ***")
            self.save_logs(cycle_num, "CYCLING_REPRO")
            return False

        if result == "crash":
            self.save_logs(cycle_num, "crash")
            return False

        # Shouldn't reach here
        self.save_logs(cycle_num, "unknown")
        return False

    def recovery(self, cycle):
        print(f"\n  [RECOVERY] Cycle {cycle} failed. Resetting device...")
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
            # Maybe it's stuck — try verify_sleep anyway
            if self.verify_sleep():
                print("  [RECOVERY] Device appears asleep (no log indicator)")
                time.sleep(5)
                return True
            print("  [RECOVERY] Timeout — device didn't sleep after reset")
            return False


if __name__ == "__main__":
    test = ERPCStuckTest()
    sys.exit(test.run(num_cycles=NUM_CYCLES))
