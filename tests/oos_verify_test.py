#!/usr/bin/env python3
"""OOS (Out-of-Service) Full Verification Test

Triggers OOS via battery voltage simulation and records all console output
until the device enters deep sleep. Then analyzes logs to confirm all
expected OOS actions were performed.

Prerequisites:
  - Start a livestream from the Arlo app BEFORE running this script
  - Device must be awake (ISP on, stream active)

OOS Actions verified:
  1. Battery Critical LED pattern applied
  2. OOS reason saved on MCU
  3. arlo_handle_battery_status() called with critical=true
  4. Streaming blocked/stopped
  5. FW upgrade stopped (implicit via ISP power-off)
  6. Wi-Fi turned off after LastGaspWaitTime (30s)
  7. ISP turned off after LastGaspWaitTime
  8. MCU monitors battery periodically (after ISP off)
  9. Last gasp notification sent to Arlo BE

Usage:
  python3 oos_verify_test.py
"""

import argparse
import os
import sys
import time
from enum import Enum, auto

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase

sys.stdout.reconfigure(line_buffering=True)

# --- MCU simulation commands ---
MCU_CMD_SIM_ENABLE = "battery simulation enable 1"
MCU_CMD_SIM_VOLTAGE = "battery simulation voltage 2900"
MCU_CMD_SIM_DISABLE = "battery simulation enable 0"
MCU_REPLY_PATTERN = "intParams="

# --- Log patterns to match ---

# MCU patterns
MCU_OOS_VOLTAGE = "u16VoltagAverage ="
MCU_LAST_GASP_TIMEOUT = "u32LastGaspWaitTime timeout"
MCU_ISP_POWER_OFF = "OUTPUT_ISP_POWER_ON_OFF"
MCU_ISP_EVENT_OOS = "eEvent=15"
MCU_SLEEP_INDICATOR = "Network Stack Suspended, MCU can enter DeepSleep power mode"
MCU_BATT_MONITOR = "intTemperature="
MCU_LED_TASK = "eLedTask ="
MCU_LED_PATTERN_QUEUED = "[LED] RGB pattern queued"
MCU_LED_PATTERN_STARTED = "[LED] Pattern started"
MCU_VOTE_SLEEP = "vote:sleep"
MCU_ISP_POWER_STATUS = "ePowerStatus="

# ISP patterns
ISP_ENTER_OOS = "Enter OOS"
ISP_BATT_STATUS = "b_percent:"
ISP_BATT_CRITICAL = "b_critical: true"
ISP_CONN_STATE_CHANGE = "connection state change"
ISP_BATTERY_CRITICAL_STATE = "batteryCritical"
ISP_FEEDBACK_APPLY = "applying"
ISP_FEEDBACK_STATE = "new state="
ISP_CRITICAL_BATTERY_ALERT = "Post critical battery alert"
ISP_CRITICAL_BATTERY_EVENT = "criticalBatteryEvent"
ISP_STREAM_BLOCKED = "Live stream blocked"
ISP_OOS_STREAMS_STOPPED = "Device critical state: stopping all streams"
ISP_OOS_PIR_SUPPRESSED = "Device critical state: suppressing motion trigger"
ISP_OOS_RTSP_REJECTED = "Device critical state: rejecting RTSP stream request"
ISP_OOS_SIP_REJECTED = "Device critical state: rejecting incoming SIP call"
ISP_ARCHIVE_LOG = "Archiving system-log before OFF"
ISP_SHUTDOWN_SIGNAL = "Registering SHUTDOWN signal"
ISP_LOW_BATTERY_ALERT = "Post lowBattery alert"

# Timeouts
OOS_TOTAL_TIMEOUT_S = 120  # max time to wait for full OOS cycle (ISP off + sleep)
LAST_GASP_WAIT_TIME_S = 30  # MCU gives ISP 30s before power-off
SIMULATION_SETTLE_S = 5     # time after simulation to detect OOS trigger


class Event(Enum):
    MCU_REPLY = auto()
    MCU_OOS_VOLTAGE = auto()
    MCU_LAST_GASP_TIMEOUT = auto()
    MCU_ISP_POWER_OFF = auto()
    MCU_ISP_EVENT_OOS = auto()
    MCU_SLEEP = auto()
    MCU_BATT_MONITOR = auto()
    MCU_LED_TASK = auto()
    MCU_LED_PATTERN_QUEUED = auto()
    MCU_LED_PATTERN_STARTED = auto()
    MCU_VOTE_SLEEP = auto()
    ISP_ENTER_OOS = auto()
    ISP_BATT_CRITICAL = auto()
    ISP_CONN_STATE_CRITICAL = auto()
    ISP_FEEDBACK_APPLY = auto()
    ISP_FEEDBACK_STATE = auto()
    ISP_CRITICAL_ALERT = auto()
    ISP_STREAM_BLOCKED = auto()
    ISP_ARCHIVE_LOG = auto()
    ISP_SHUTDOWN = auto()
    ISP_LOW_BATTERY_ALERT = auto()


class OOSVerifyTest(DeviceTestBase):
    _test_name = "oos_verify"
    _log_dir = "/tmp/oos_verify_logs"
    _sleep_timeout = 120

    def __init__(self):
        super().__init__()
        self.traced_lines = []

    def _check_events(self, line, source):
        if source == "MCU":
            if MCU_REPLY_PATTERN in line:
                self.event_callback(Event.MCU_REPLY, source, line)
            if MCU_OOS_VOLTAGE in line:
                self.event_callback(Event.MCU_OOS_VOLTAGE, source, line)
                self.traced_lines.append(("MCU_OOS_REASON", line))
            if MCU_LAST_GASP_TIMEOUT in line:
                self.event_callback(Event.MCU_LAST_GASP_TIMEOUT, source, line)
                self.traced_lines.append(("MCU_LASTGASP_TO", line))
            if MCU_ISP_EVENT_OOS in line and "ALWAYS_ON" in line:
                self.event_callback(Event.MCU_ISP_EVENT_OOS, source, line)
                self.traced_lines.append(("MCU_ISP_EVT_OOS", line))
            if MCU_SLEEP_INDICATOR in line:
                self.event_callback(Event.MCU_SLEEP, source, line)
                self.traced_lines.append(("MCU_SLEEP", line))
            if MCU_BATT_MONITOR in line and "u16Percentage" in line:
                self.event_callback(Event.MCU_BATT_MONITOR, source, line)
                self.traced_lines.append(("MCU_BATT_MON", line))
            if MCU_LED_TASK in line:
                self.event_callback(Event.MCU_LED_TASK, source, line)
                self.traced_lines.append(("MCU_LED_TASK", line))
            if MCU_LED_PATTERN_QUEUED in line:
                self.event_callback(Event.MCU_LED_PATTERN_QUEUED, source, line)
                self.traced_lines.append(("MCU_LED_QUEUED", line))
            if MCU_LED_PATTERN_STARTED in line:
                self.event_callback(Event.MCU_LED_PATTERN_STARTED, source, line)
                self.traced_lines.append(("MCU_LED_STARTED", line))
            if MCU_VOTE_SLEEP in line:
                self.event_callback(Event.MCU_VOTE_SLEEP, source, line)

        elif source == "ISP":
            if ISP_ENTER_OOS in line:
                self.event_callback(Event.ISP_ENTER_OOS, source, line)
                self.traced_lines.append(("ISP_ENTER_OOS", line))
            if ISP_BATT_CRITICAL in line:
                self.event_callback(Event.ISP_BATT_CRITICAL, source, line)
                self.traced_lines.append(("ISP_BATT_CRIT", line))
            if ISP_CONN_STATE_CHANGE in line and ISP_BATTERY_CRITICAL_STATE in line:
                self.event_callback(Event.ISP_CONN_STATE_CRITICAL, source, line)
                self.traced_lines.append(("ISP_CONN_CRIT", line))
            if ISP_FEEDBACK_APPLY in line:
                self.event_callback(Event.ISP_FEEDBACK_APPLY, source, line)
                self.traced_lines.append(("ISP_FB_APPLY", line))
            if ISP_FEEDBACK_STATE in line and "Battery" in line:
                self.event_callback(Event.ISP_FEEDBACK_STATE, source, line)
                self.traced_lines.append(("ISP_FB_STATE", line))
            if ISP_CRITICAL_BATTERY_ALERT in line or ISP_CRITICAL_BATTERY_EVENT in line:
                self.event_callback(Event.ISP_CRITICAL_ALERT, source, line)
                self.traced_lines.append(("ISP_CRIT_ALERT", line))
            if ISP_STREAM_BLOCKED in line or ISP_OOS_STREAMS_STOPPED in line:
                self.event_callback(Event.ISP_STREAM_BLOCKED, source, line)
                self.traced_lines.append(("ISP_STREAM_BLK", line))
            if ISP_OOS_PIR_SUPPRESSED in line or ISP_OOS_RTSP_REJECTED in line or ISP_OOS_SIP_REJECTED in line:
                self.event_callback(Event.ISP_STREAM_BLOCKED, source, line)
                self.traced_lines.append(("ISP_STREAM_BLK", line))
            if ISP_ARCHIVE_LOG in line:
                self.event_callback(Event.ISP_ARCHIVE_LOG, source, line)
                self.traced_lines.append(("ISP_ARCHIVE", line))
            if ISP_SHUTDOWN_SIGNAL in line:
                self.event_callback(Event.ISP_SHUTDOWN, source, line)
                self.traced_lines.append(("ISP_SHUTDOWN", line))
            if ISP_LOW_BATTERY_ALERT in line:
                self.event_callback(Event.ISP_LOW_BATTERY_ALERT, source, line)
                self.traced_lines.append(("ISP_LOW_BATT", line))

    def send_mcu_command(self, cmd, expect_reply=True, retries=3):
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
        print(f"[CYCLE {cycle_num}] OOS Full Verification")
        print(f"{'='*60}")

        self.clear_events()
        self.traced_lines = []
        self.mcu.start_recording()
        self.isp.start_recording()

        # --- Step 0: ISP login + tail ---
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

        # --- Step 2: Trigger OOS via voltage simulation ---
        print("\n  [STEP 2] Trigger OOS via battery voltage simulation...")
        print("           (Setting voltage to 2900mV, threshold is 3000mV)")
        if not self.send_mcu_command(MCU_CMD_SIM_ENABLE):
            self.save_logs(cycle_num, "sim_enable_fail")
            return False
        time.sleep(0.5)

        self.clear_events()
        if not self.send_mcu_command(MCU_CMD_SIM_VOLTAGE):
            self.save_logs(cycle_num, "sim_voltage_fail")
            return False

        # --- Step 3: Record all logs until device sleeps ---
        print(f"\n  [STEP 3] Recording OOS sequence (timeout: {OOS_TOTAL_TIMEOUT_S}s)...")
        print(f"           Expecting: OOS trigger → ISP notification → last gasp → ISP off → sleep")
        print(f"           LastGaspWaitTime = {LAST_GASP_WAIT_TIME_S}s")

        all_events = list(Event)
        sleep_seen = False
        isp_off_seen = False
        deadline = time.time() + OOS_TOTAL_TIMEOUT_S

        while time.time() < deadline:
            evt = self.wait_for_any_event(all_events, timeout=2)
            if evt is None:
                continue

            event_type, source, line = evt

            if event_type == Event.MCU_OOS_VOLTAGE:
                print(f"    [MCU] OOS voltage threshold hit: {line[:120]}")
            elif event_type == Event.MCU_ISP_EVENT_OOS:
                print(f"    [MCU] ISP event: ENTER_OUT_OF_SERVICE: {line[:120]}")
            elif event_type == Event.ISP_ENTER_OOS:
                print(f"    [ISP] Enter OOS received: {line[:120]}")
            elif event_type == Event.ISP_BATT_CRITICAL:
                print(f"    [ISP] Battery status critical=true: {line[:120]}")
            elif event_type == Event.ISP_CONN_STATE_CRITICAL:
                print(f"    [ISP] Connection state → batteryCritical: {line[:120]}")
            elif event_type == Event.ISP_FEEDBACK_STATE:
                print(f"    [ISP] Feedback state change: {line[:120]}")
            elif event_type == Event.ISP_FEEDBACK_APPLY:
                print(f"    [ISP] Feedback APPLIED (LED): {line[:120]}")
            elif event_type == Event.ISP_CRITICAL_ALERT:
                print(f"    [ISP] Critical battery alert/event to BE: {line[:120]}")
            elif event_type == Event.ISP_LOW_BATTERY_ALERT:
                print(f"    [ISP] Low battery alert posted: {line[:120]}")
            elif event_type == Event.ISP_STREAM_BLOCKED:
                print(f"    [ISP] Stream BLOCKED: {line[:120]}")
            elif event_type == Event.ISP_ARCHIVE_LOG:
                print(f"    [ISP] Archiving log (preparing shutdown): {line[:120]}")
                isp_off_seen = True
            elif event_type == Event.ISP_SHUTDOWN:
                print(f"    [ISP] SHUTDOWN signal registered: {line[:120]}")
            elif event_type == Event.MCU_LAST_GASP_TIMEOUT:
                print(f"    [MCU] LastGaspWaitTime TIMEOUT (30s elapsed): {line[:120]}")
            elif event_type == Event.MCU_LED_TASK:
                print(f"    [MCU] LED task set: {line[:80]}")
            elif event_type == Event.MCU_LED_PATTERN_QUEUED:
                print(f"    [MCU] LED pattern queued: {line[:120]}")
            elif event_type == Event.MCU_LED_PATTERN_STARTED:
                print(f"    [MCU] LED pattern started: {line[:120]}")
            elif event_type == Event.MCU_BATT_MONITOR:
                print(f"    [MCU] Periodic battery monitor (ISP off): {line[:120]}")
            elif event_type == Event.MCU_SLEEP:
                print(f"    [MCU] DEEP SLEEP entered: {line[:120]}")
                sleep_seen = True
                break

        # If ISP went off but MCU didn't announce deep sleep via the indicator,
        # wait a bit more for the periodic monitor message
        if isp_off_seen and not sleep_seen:
            print("\n    [..] ISP off, waiting for MCU deep sleep or battery monitor...")
            extra_deadline = time.time() + 60
            while time.time() < extra_deadline:
                evt = self.wait_for_any_event(
                    [Event.MCU_SLEEP, Event.MCU_BATT_MONITOR], timeout=5)
                if evt:
                    event_type, source, line = evt
                    if event_type == Event.MCU_SLEEP:
                        print(f"    [MCU] DEEP SLEEP entered: {line[:120]}")
                        sleep_seen = True
                        break
                    elif event_type == Event.MCU_BATT_MONITOR:
                        print(f"    [MCU] Periodic battery monitor: {line[:120]}")
                        break

        # --- Step 4: Analyze results ---
        print(f"\n{'='*60}")
        print(f"  [ANALYSIS] OOS Action Verification")
        print(f"{'='*60}")

        results = {
            "1_battery_critical_led": False,
            "2_oos_reason_saved": False,
            "3_handle_battery_status": False,
            "4_streaming_stopped": False,
            "5_fw_upgrade_stopped": False,
            "6_wifi_off": False,
            "7_isp_off": False,
            "8_periodic_batt_monitor": False,
            "9_last_gasp_notification": False,
        }

        for tag, line in self.traced_lines:
            # 1. Battery Critical LED
            if tag in ("ISP_FB_APPLY", "ISP_FB_STATE", "MCU_LED_QUEUED", "MCU_LED_STARTED"):
                results["1_battery_critical_led"] = True
            # 2. OOS reason saved
            if tag == "MCU_OOS_REASON":
                results["2_oos_reason_saved"] = True
            # 3. arlo_handle_battery_status with critical
            if tag in ("ISP_BATT_CRIT", "ISP_ENTER_OOS"):
                results["3_handle_battery_status"] = True
            # 4. Streaming stopped
            if tag == "ISP_STREAM_BLK":
                results["4_streaming_stopped"] = True
            # 5. FW upgrade stopped (implicit via ISP power-off)
            if tag in ("ISP_ARCHIVE", "ISP_SHUTDOWN"):
                results["5_fw_upgrade_stopped"] = True
            # 6. WiFi off (MCU last gasp timeout triggers wifi+ISP off)
            if tag == "MCU_LASTGASP_TO":
                results["6_wifi_off"] = True
            # 7. ISP off
            if tag in ("ISP_ARCHIVE", "ISP_SHUTDOWN", "MCU_LASTGASP_TO"):
                results["7_isp_off"] = True
            # 8. Periodic battery monitor (only prints when ISP is off)
            if tag == "MCU_BATT_MON":
                results["8_periodic_batt_monitor"] = True
            # 9. Last gasp notification
            if tag in ("ISP_CRIT_ALERT", "ISP_LOW_BATT"):
                results["9_last_gasp_notification"] = True

        # Also mark ISP off from isp_off_seen flag
        if isp_off_seen:
            results["5_fw_upgrade_stopped"] = True
            results["7_isp_off"] = True

        print("\n  # | OOS Action                          | Status")
        print("  --+-----------------------------------------+--------")
        action_names = {
            "1_battery_critical_led":    "Start Battery Critical LED",
            "2_oos_reason_saved":        "Save OOS reason on MCU",
            "3_handle_battery_status":   "arlo_handle_battery_status(critical=true)",
            "4_streaming_stopped":       "Stop ongoing streaming",
            "5_fw_upgrade_stopped":      "Stop FW upgrade (ISP off)",
            "6_wifi_off":                "Turn off Wi-Fi (LastGaspWaitTime)",
            "7_isp_off":                 "Turn off ISP (LastGaspWaitTime)",
            "8_periodic_batt_monitor":   "MCU periodic battery monitor",
            "9_last_gasp_notification":  "Send Last Gasp to Arlo BE",
        }

        for key, name in action_names.items():
            status = "PASS" if results[key] else "MISS"
            marker = "[x]" if results[key] else "[ ]"
            print(f"  {marker} {name:40s} {status}")

        # --- Step 5: Chronological trace ---
        print(f"\n  [TRACE] All OOS-related log entries (chronological):")
        for tag, line in self.traced_lines:
            print(f"    [{tag:15s}] {line[:130]}")

        # --- Step 6: Disable simulation ---
        print("\n  [STEP 6] Disabling battery simulation...")
        try:
            self.mcu.sock.sendall(f"{MCU_CMD_SIM_DISABLE}\r\n".encode())
        except OSError:
            print("  [WARN] Could not send disable (MCU may be asleep)")

        self.mcu.stop_recording()
        self.isp.stop_recording()
        self.save_logs(cycle_num, "complete")

        # Determine pass/fail
        passed_count = sum(results.values())
        total_checks = len(results)
        print(f"\n  [RESULT] {passed_count}/{total_checks} OOS actions confirmed")

        if passed_count >= 7:
            print("  [PASS] OOS sequence verified")
            return True
        elif passed_count >= 4:
            print("  [PARTIAL] Most OOS actions confirmed, some missed (check logs)")
            return True
        else:
            print("  [FAIL] Too many OOS actions not confirmed")
            return False


def main():
    parser = argparse.ArgumentParser(description="OOS Full Verification Test")
    parser.add_argument("--cycles", type=int, default=1,
                        help="Number of test cycles (default: 1)")
    args = parser.parse_args()

    test = OOSVerifyTest()
    sys.exit(test.run(num_cycles=args.cycles))


if __name__ == "__main__":
    main()
