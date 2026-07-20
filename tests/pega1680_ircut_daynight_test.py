#!/usr/bin/env python3
"""
PEGA-1680: IR cut day/night transition test during active stream.

Prerequisites:
- Device streaming (live view active from app, or D2AP mode)
- Voodoo board accessible
- serial_mux running

Test:
1. Confirm stream is active (user-initiated)
2. Force NIGHT via DO5=1 (daynight shutter closed)
3. Wait for MCU night mode + ISP lsm_enter_night_state
4. Force DAY via DO5=0 (daynight shutter open)
5. Check if ISP calls lsm_enter_day_state / ir_cut_filter
6. Report PASS/FAIL
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase
from voodoo.voodoo_channels import DO_AMBLIGHT

# Events fired by _check_events
EVT_MCU_NIGHT = "mcu_night"
EVT_MCU_DAY = "mcu_day"
EVT_ISP_NIGHT_ENTER = "isp_night_enter"
EVT_ISP_DAY_ENTER = "isp_day_enter"
EVT_ISP_IRCUT_DAY = "isp_ircut_day"
EVT_ISP_IRCUT_NIGHT = "isp_ircut_night"
EVT_ISP_DAY_EVENT = "isp_day_event"
EVT_ISP_NIGHT_EVENT = "isp_night_event"
EVT_CRASH = "crash"


class PEGA1680Test(DeviceTestBase):
    _test_name = "pega1680_ircut_daynight"
    _log_dir = "/tmp/pega1680_logs"
    _night_timeout = 15
    _day_timeout = 15

    def run(self, num_cycles=1):
        os.makedirs(self._log_dir, exist_ok=True)
        print(f"=== {self._test_name} ({num_cycles} cycles) ===")
        print(f"  Log directory: {self._log_dir}")
        print()

        print("[INIT] Connecting consoles...")
        self.connect_consoles()

        print("[INIT] Initializing ISP console (login + tail -f)...")
        self.init_isp_console()
        time.sleep(1)

        print("[INIT] Draining stale buffers...")
        self.mcu.clear_lines()
        self.isp.clear_lines()
        self.clear_events()
        print("[INIT] Ready")

        for cycle in range(1, num_cycles + 1):
            passed = self.run_cycle(cycle)
            self.results.append(passed)
            if not passed:
                if not self.recovery(cycle):
                    print("  [RECOVERY] Aborting remaining cycles")
                    break

        self.disconnect_consoles()

        total = len(self.results)
        passed_count = sum(self.results)
        print(f"\n{'='*60}")
        print(f"TEST COMPLETE: {passed_count}/{total} cycles passed")
        print(f"{'='*60}")
        for i, r in enumerate(self.results, 1):
            print(f"  Cycle {i}: {'PASS' if r else 'FAIL'}")
        if all(self.results) and total == num_cycles:
            print(f"\nRESULT: PASS")
            return 0
        else:
            print(f"\nRESULT: FAIL ({self.results.count(False)} failures)")
            print(f"Logs saved to: {self._log_dir}")
            return 1

    def _check_events(self, line, source):
        if source == "MCU":
            if "eMode = 2" in line or "NV_MODE_NIGHT" in line:
                self.event_callback(EVT_MCU_NIGHT, source, line)
            elif "eMode = 1" in line or "NV_MODE_DAY" in line:
                self.event_callback(EVT_MCU_DAY, source, line)
            elif "IRCut_ModeSet: NIGHT" in line:
                self.event_callback(EVT_ISP_IRCUT_NIGHT, source, line)
            elif "IRCut_ModeSet: DAY" in line:
                self.event_callback(EVT_ISP_IRCUT_DAY, source, line)
        elif source == "ISP":
            if "lsm_enter_night_state" in line:
                self.event_callback(EVT_ISP_NIGHT_ENTER, source, line)
            elif "lsm_enter_day_state" in line or "exit_night_state" in line:
                self.event_callback(EVT_ISP_DAY_ENTER, source, line)
            elif "ir_cut_filter: 1" in line or "set_ir_cut_filter(1" in line:
                self.event_callback(EVT_ISP_IRCUT_DAY, source, line)
            elif "ir_cut_filter: 0" in line or "set_ir_cut_filter(0" in line:
                self.event_callback(EVT_ISP_IRCUT_NIGHT, source, line)
            elif "LSM_EVT_DAY_MODE_SWITCH" in line:
                self.event_callback(EVT_ISP_DAY_EVENT, source, line)
            elif "LSM_EVT_NIGHT_MODE_SWITCH" in line:
                self.event_callback(EVT_ISP_NIGHT_EVENT, source, line)
            elif "Segfault" in line or "panic" in line or "Oops" in line:
                self.event_callback(EVT_CRASH, source, line)

    def run_cycle(self, cycle_num):
        print(f"\n--- Cycle {cycle_num} ---")

        self.clear_events()
        self.mcu.start_recording()
        self.isp.start_recording()

        # Step 1: Force NIGHT
        print("[1] Forcing NIGHT (DO5 ON, shutter closed)...")
        if not self.voodoo_on(DO_AMBLIGHT):
            print("  [ERROR] Failed to set DO5 ON")
            return False

        # Wait for MCU to detect night
        print("  Waiting for MCU night mode...")
        evt = self.wait_for_event(EVT_MCU_NIGHT, self._night_timeout)
        if evt:
            print(f"  [OK] MCU night: {evt[2][:80]}")
        else:
            print("  [WARN] MCU night not confirmed (may already be in night)")

        # Wait for ISP night state
        print("  Waiting for ISP lsm_enter_night_state...")
        evt = self.wait_for_any_event(
            [EVT_ISP_NIGHT_ENTER, EVT_ISP_IRCUT_NIGHT, EVT_ISP_NIGHT_EVENT],
            self._night_timeout)
        if evt:
            print(f"  [OK] ISP night: {evt[0]} — {evt[2][:80]}")
        else:
            print("  [WARN] ISP night not confirmed in logs")
            isp_lines = self.isp.get_lines()
            print("  Last 5 ISP lines:")
            for l in isp_lines[-5:]:
                print(f"    {l[:100]}")

        time.sleep(1)

        # Step 2: Force DAY
        print("\n[2] Forcing DAY (DO5 OFF, shutter open)...")
        self.clear_events()
        if not self.voodoo_off(DO_AMBLIGHT):
            print("  [ERROR] Failed to set DO5 OFF")
            return False

        # Wait for MCU to detect day
        print("  Waiting for MCU day mode...")
        evt = self.wait_for_event(EVT_MCU_DAY, self._day_timeout)
        if evt:
            print(f"  [OK] MCU day: {evt[2][:80]}")
        else:
            print("  [FAIL] MCU did not switch to day mode")
            mcu_lines = self.mcu.get_lines()
            print("  Last 10 MCU lines:")
            for l in mcu_lines[-10:]:
                print(f"    {l[:100]}")
            self.save_logs(cycle_num, "mcu_no_day")
            return False

        # Critical check: Wait for ISP to actuate IR cut back to DAY
        print("  Waiting for ISP lsm_enter_day_state / ir_cut_filter(DAY)...")
        evt = self.wait_for_any_event(
            [EVT_ISP_DAY_ENTER, EVT_ISP_IRCUT_DAY],
            self._day_timeout)

        if evt:
            print(f"  [OK] ISP day actuation: {evt[0]} — {evt[2][:80]}")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return True

        # Check if we at least got the event but no actuation
        evt_received = self.check_event(EVT_ISP_DAY_EVENT)
        if evt_received:
            print(f"  [PARTIAL] Event received but NO actuation: {evt_received[2][:80]}")
            print("  >>> PEGA-1680 CONFIRMED: DAY event received but IR cut NOT switched")
        else:
            print("  [FAIL] No ISP day event or actuation detected")
            print("  >>> PEGA-1680 CONFIRMED: IR cut stuck in NIGHT")

        isp_lines = self.isp.get_lines()
        print("\n  Last 15 ISP lines:")
        for l in isp_lines[-15:]:
            print(f"    {l[:120]}")

        self.save_logs(cycle_num, "ircut_stuck")
        self.mcu.stop_recording()
        self.isp.stop_recording()
        return False


def main():
    parser = argparse.ArgumentParser(description="PEGA-1680: IR cut day/night test")
    parser.add_argument('-n', '--cycles', type=int, default=3,
                        help='Number of night→day cycles (default 3)')
    parser.add_argument('--no-prompt', action='store_true',
                        help='Skip interactive prompt (assume stream is active)')
    args = parser.parse_args()

    print("=" * 60)
    print("PEGA-1680: IR Cut Day/Night Test During Active Stream")
    print("=" * 60)
    print()
    if not args.no_prompt:
        print("Ensure live view is active from the Arlo app (or device in D2AP mode).")
        input("Press Enter when streaming is active... ")
    else:
        print("Assuming stream is active (--no-prompt)")

    test = PEGA1680Test()
    sys.exit(test.run(num_cycles=args.cycles))


if __name__ == "__main__":
    main()
