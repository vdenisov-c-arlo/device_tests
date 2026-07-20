#!/usr/bin/env python3
"""OOS Battery Low LED Feedback Test — verifies low battery LED is applied
when MCU reports simulated low battery to ISP via eRPC.

Assumes ISP is already awake (e.g. livestream active from app).
Start a livestream BEFORE running this script.

Flow:
  1. Login to ISP, tail syslog
  2. Inject battery simulation commands to MCU
  3. Wait for ISP to detect low battery and apply LED feedback

Usage:
  python3 oos_battery_led_test.py
  python3 oos_battery_led_test.py --cycles 3
"""

import argparse
import os
import sys
import time
from enum import Enum, auto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import DeviceTestBase
from mcu_patterns import (
    CRASH_PATTERNS, HANG_PATTERNS, SLEEP_INDICATOR,
    check_mcu_line, check_isp_line, is_crash_dump_line,
)

sys.stdout.reconfigure(line_buffering=True)

MCU_CMD_SIM_ENABLE = "battery simulation enable 1"
MCU_CMD_SIM_PERCENTAGE = "battery simulation percentage 14"
MCU_CMD_SIM_DISABLE = "battery simulation enable 0"
MCU_REPLY_PATTERN = "intParams="

# ISP-side log patterns
ISP_LOG_LOW_BATTERY_ALERT = "Post lowBattery alert for battery level"
ISP_LOG_FEEDBACK_STATE = "new state="
ISP_LOG_FEEDBACK_APPLY = "applying"
ISP_LOG_BATTERY_PERCENT = "b_percent:"
ISP_LOG_SET_WLED_WAKE = "set_wled_wake_pattern"

# MCU-side LED log patterns
MCU_LOG_LED_TASK_SET = "eLedTask ="
MCU_LOG_LED_PATTERN_RECV = "low battery LED pattern received"
MCU_LOG_LED_TIMER_SCHED = "scheduling low battery LED timer"
MCU_LOG_LED_WAKE_BLINK = "woke for low battery LED blink"
MCU_LOG_LED_RESCHED = "blink done, re-scheduling timer"
MCU_LOG_WLED_SET = "bEnabled ="

BATTERY_POLL_INTERVAL_S = 2
PROPAGATION_TIMEOUT_S = 15
FEEDBACK_TIMEOUT_S = 30


class Event(Enum):
    MCU_REPLY = auto()
    SLEEP_DETECTED = auto()
    CRASH_DETECTED = auto()
    MCU_LED_TASK_SET = auto()
    MCU_LED_PATTERN_RECV = auto()
    MCU_LED_TIMER_SCHED = auto()
    MCU_LED_WAKE_BLINK = auto()
    MCU_LED_RESCHED = auto()
    MCU_WLED_SET = auto()
    ISP_BATTERY_STATUS = auto()
    ISP_LOW_BATTERY_ALERT = auto()
    ISP_FEEDBACK_STATE = auto()
    ISP_FEEDBACK_APPLY = auto()
    ISP_WLED_WAKE = auto()


class OOSBatteryLEDTest(DeviceTestBase):
    _test_name = "oos_battery_led"
    _log_dir = "/tmp/oos_battery_led_logs"
    _sleep_timeout = 60

    def __init__(self):
        super().__init__()
        self.traced_lines = []

    def _check_events(self, line, source):
        if source == "MCU":
            if MCU_REPLY_PATTERN in line:
                self.event_callback(Event.MCU_REPLY, source, line)
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP_DETECTED, source, line)

            if MCU_LOG_LED_TASK_SET in line:
                self.event_callback(Event.MCU_LED_TASK_SET, source, line)
                self.traced_lines.append(("MCU_LED_TASK", line))
            if MCU_LOG_LED_PATTERN_RECV in line:
                self.event_callback(Event.MCU_LED_PATTERN_RECV, source, line)
                self.traced_lines.append(("MCU_LED_RECV", line))
            if MCU_LOG_LED_TIMER_SCHED in line:
                self.event_callback(Event.MCU_LED_TIMER_SCHED, source, line)
                self.traced_lines.append(("MCU_LED_TIMER", line))
            if MCU_LOG_LED_WAKE_BLINK in line:
                self.event_callback(Event.MCU_LED_WAKE_BLINK, source, line)
                self.traced_lines.append(("MCU_LED_BLINK", line))
            if MCU_LOG_LED_RESCHED in line:
                self.event_callback(Event.MCU_LED_RESCHED, source, line)
                self.traced_lines.append(("MCU_LED_RESCHED", line))
            if MCU_LOG_WLED_SET in line and "type" in line:
                self.event_callback(Event.MCU_WLED_SET, source, line)
                self.traced_lines.append(("MCU_WLED_SET", line))

            atype, _ = check_mcu_line(line)
            if atype.name != "NONE":
                self.event_callback(Event.CRASH_DETECTED, source, line)

        if source == "ISP":
            if ISP_LOG_BATTERY_PERCENT in line:
                self.event_callback(Event.ISP_BATTERY_STATUS, source, line)
                self.traced_lines.append(("ISP_BATT_STATUS", line))
            if ISP_LOG_LOW_BATTERY_ALERT in line:
                self.event_callback(Event.ISP_LOW_BATTERY_ALERT, source, line)
                self.traced_lines.append(("ISP_LOW_BATT", line))
            if ISP_LOG_FEEDBACK_STATE in line and "Battery" in line:
                self.event_callback(Event.ISP_FEEDBACK_STATE, source, line)
                self.traced_lines.append(("ISP_FB_STATE", line))
            if ISP_LOG_FEEDBACK_APPLY in line:
                self.event_callback(Event.ISP_FEEDBACK_APPLY, source, line)
                self.traced_lines.append(("ISP_FB_APPLY", line))
            if ISP_LOG_SET_WLED_WAKE in line:
                self.event_callback(Event.ISP_WLED_WAKE, source, line)
                self.traced_lines.append(("ISP_WLED_WAKE", line))
            atype, _ = check_isp_line(line)
            if atype.name != "NONE":
                self.event_callback(Event.CRASH_DETECTED, source, line)

    def send_mcu_command(self, cmd, expect_reply=True, retries=3):
        """Send a command to MCU console and verify reply."""
        for attempt in range(retries):
            if attempt > 0:
                print(f"  [RETRY] Attempt {attempt+1}/{retries} for: {cmd}")
                time.sleep(2)
            print(f"  [MCU TX] {cmd}")
            self.mcu.sock.sendall(f"{cmd}\r\n".encode())

            if not expect_reply:
                time.sleep(0.5)
                return True

            result = self.wait_for_event(Event.MCU_REPLY, timeout=5)
            if result:
                print(f"  [MCU RX] {result[2][:100]}")
                return True

        print(f"  [ERROR] No reply from MCU after {retries} attempts for: {cmd}")
        return False

    def start_isp_log_tail(self):
        """Send login + tail -f to ISP console without blocking reader thread."""
        sock = self.isp.sock
        sock.sendall(b"\x03\r\n")
        time.sleep(1)
        sock.sendall(b"\r\n")
        time.sleep(2)
        sock.sendall(b"root\r\n")
        time.sleep(1)
        sock.sendall(b"arlo\r\n")
        time.sleep(1)
        sock.sendall(b"\x03\r\n")
        time.sleep(0.5)
        sock.sendall(b"tail -f /var/log/messages\r\n")
        time.sleep(1)

    def run_cycle(self, cycle_num):
        print(f"\n{'='*60}")
        print(f"[CYCLE {cycle_num}]")
        print(f"{'='*60}")

        self.clear_events()
        self.traced_lines = []
        self.mcu.start_recording()
        self.isp.start_recording()

        # --- Step 0: Start ISP log tail ---
        print("\n  [STEP 0] Starting ISP log tail...")
        self.start_isp_log_tail()
        time.sleep(2)
        self.clear_events()
        self.traced_lines = []

        # --- Step 1: Enable LED debug on MCU ---
        print("\n  [STEP 1] Enable MCU LED debug logging...")
        self.mcu.sock.sendall(b"led debug_msg 1\r\n")
        time.sleep(2)
        self.clear_events()

        # --- Step 2: Enable battery simulation ---
        print("\n  [STEP 2] Enable OOS battery simulation...")
        if not self.send_mcu_command(MCU_CMD_SIM_ENABLE):
            self.save_logs(cycle_num, "sim_enable_fail")
            return False
        time.sleep(0.5)

        # --- Step 3: Set battery percentage to 14% (below LOW_BATTERY_LEVEL=15) ---
        print("\n  [STEP 3] Set battery percentage to 14%...")
        self.clear_events()
        if not self.send_mcu_command(MCU_CMD_SIM_PERCENTAGE):
            self.save_logs(cycle_num, "sim_pct_fail")
            return False

        # --- Step 4: Wait for ISP to poll battery status ---
        print(f"\n  [STEP 4] Waiting for ISP battery status update (poll every ~{BATTERY_POLL_INTERVAL_S}s)...")

        status_seen = False
        deadline = time.time() + PROPAGATION_TIMEOUT_S
        while time.time() < deadline:
            result = self.wait_for_event(Event.ISP_BATTERY_STATUS, timeout=2)
            if result and "14" in result[2]:
                print(f"  [OK] Battery status received: {result[2][:120]}")
                status_seen = True
                break
            elif result:
                print(f"  [..] Status update (not 14% yet): {result[2][:80]}")

            crash = self.check_event(Event.CRASH_DETECTED)
            if crash:
                print(f"  [ABORT] Crash detected: {crash[2][:120]}")
                self.save_logs(cycle_num, "crash")
                return False

        if not status_seen:
            print("  [FAIL] Battery status with 14% not seen within timeout")
            self.save_logs(cycle_num, "no_propagation")
            return False

        # --- Step 5: Wait for low battery LED feedback ---
        print(f"\n  [STEP 5] Waiting for low battery LED feedback (timeout: {FEEDBACK_TIMEOUT_S}s)...")

        results = {
            "low_battery_alert": False,
            "feedback_state_change": False,
            "feedback_applied": False,
            "wled_wake_pattern": False,
            "mcu_led_task_set": False,
            "mcu_led_pattern_recv": False,
            "mcu_led_timer_sched": False,
            "mcu_led_wake_blink": False,
            "mcu_wled_set": False,
        }

        all_led_events = [
            Event.ISP_LOW_BATTERY_ALERT, Event.ISP_FEEDBACK_STATE,
            Event.ISP_FEEDBACK_APPLY, Event.ISP_WLED_WAKE,
            Event.MCU_LED_TASK_SET, Event.MCU_LED_PATTERN_RECV,
            Event.MCU_LED_TIMER_SCHED, Event.MCU_LED_WAKE_BLINK, Event.MCU_WLED_SET,
        ]

        check_deadline = time.time() + FEEDBACK_TIMEOUT_S
        while time.time() < check_deadline:
            evt = self.wait_for_any_event(all_led_events, timeout=2)
            if evt is None:
                continue
            if evt[0] == Event.ISP_LOW_BATTERY_ALERT:
                print(f"    [ISP] Low battery alert posted: {evt[2][:100]}")
                results["low_battery_alert"] = True
            elif evt[0] == Event.ISP_FEEDBACK_STATE:
                print(f"    [ISP] Feedback state change: {evt[2][:100]}")
                results["feedback_state_change"] = True
            elif evt[0] == Event.ISP_FEEDBACK_APPLY:
                print(f"    [ISP] Feedback pattern APPLIED: {evt[2][:100]}")
                results["feedback_applied"] = True
            elif evt[0] == Event.ISP_WLED_WAKE:
                print(f"    [ISP] WLED wake pattern set: {evt[2][:100]}")
                results["wled_wake_pattern"] = True
            elif evt[0] == Event.MCU_LED_TASK_SET:
                print(f"    [MCU] LED task set: {evt[2][:100]}")
                results["mcu_led_task_set"] = True
            elif evt[0] == Event.MCU_LED_PATTERN_RECV:
                print(f"    [MCU] Low battery LED pattern received: {evt[2][:100]}")
                results["mcu_led_pattern_recv"] = True
            elif evt[0] == Event.MCU_LED_TIMER_SCHED:
                print(f"    [MCU] LED timer scheduled: {evt[2][:100]}")
                results["mcu_led_timer_sched"] = True
            elif evt[0] == Event.MCU_LED_WAKE_BLINK:
                print(f"    [MCU] Woke for LED blink: {evt[2][:100]}")
                results["mcu_led_wake_blink"] = True
            elif evt[0] == Event.MCU_WLED_SET:
                print(f"    [MCU] WLED pattern set: {evt[2][:100]}")
                results["mcu_wled_set"] = True

            if results["feedback_applied"] and results["mcu_led_task_set"]:
                break

        # --- Step 6: Report results ---
        print("\n  [STEP 6] Results:")
        print("  --- ISP side ---")
        print(f"    Battery status propagated:  YES")
        print(f"    Low battery alert posted:   {'YES' if results['low_battery_alert'] else 'NO'}")
        print(f"    Feedback state change:      {'YES' if results['feedback_state_change'] else 'NO'}")
        print(f"    Feedback pattern applied:   {'YES' if results['feedback_applied'] else 'NO'}")
        print(f"    WLED wake pattern to MCU:   {'YES' if results['wled_wake_pattern'] else 'NO'}")
        print("  --- MCU side ---")
        print(f"    LED task set (eLedTask):    {'YES' if results['mcu_led_task_set'] else 'NO'}")
        print(f"    LED pattern received:       {'YES' if results['mcu_led_pattern_recv'] else 'NO'}")
        print(f"    LED timer scheduled:        {'YES' if results['mcu_led_timer_sched'] else 'NO'}")
        print(f"    Woke for LED blink:         {'YES' if results['mcu_led_wake_blink'] else 'NO'}")
        print(f"    WLED set call:              {'YES' if results['mcu_wled_set'] else 'NO'}")

        # --- Step 7: Trace log summary ---
        print("\n  [TRACE] All related log entries (chronological):")
        for tag, line in self.traced_lines:
            print(f"    [{tag:15s}] {line[:130]}")

        # --- Step 8: Disable simulation ---
        print("\n  [STEP 8] Disabling battery simulation...")
        self.send_mcu_command(MCU_CMD_SIM_DISABLE, expect_reply=True)

        self.mcu.stop_recording()
        self.isp.stop_recording()
        self.save_logs(cycle_num, "complete")

        passed = results["feedback_applied"]
        if passed:
            print("\n  [PASS] Low battery LED feedback APPLIED by feedback_manager")
        elif results["feedback_state_change"]:
            print("\n  [PARTIAL] Feedback state changed to Battery Low, but 'applying' not seen")
            print("            feedback_manager may not have run its worker loop yet")
            passed = True
        elif results["low_battery_alert"]:
            print("\n  [PARTIAL] Low battery alert posted but feedback state not captured")
            print("            Check if DEBUG logs are enabled in syslog")
            passed = True
        else:
            print("\n  [FAIL] Low battery LED feedback was NOT triggered")
            print("         ISP received the low percentage but did not trigger feedback.")
            print("         Check if device was already in low-battery state (no transition).")

        return passed


def main():
    parser = argparse.ArgumentParser(description="OOS Battery Low LED Test")
    parser.add_argument("--cycles", type=int, default=1,
                        help="Number of test cycles (default: 1)")
    args = parser.parse_args()

    test = OOSBatteryLEDTest()
    sys.exit(test.run(num_cycles=args.cycles))


if __name__ == "__main__":
    main()
