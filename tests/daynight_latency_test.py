#!/usr/bin/env python3
"""
Daynight shutter latency measurement test.

Cycles the testbot4 ALS shutter (DO5) between day and night and measures
the time from relay actuation to MCU detecting the ALS change and switching
mode (logged as "DayNightStateSet(eMode=N)").

Usage:
    python3 daynight_latency_test.py [-n CYCLES] [--settle SECONDS]

Output: per-transition latency and summary statistics (min/max/avg/median).
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase
from testbot4.testbot4_channels import DO_AMBLIGHT
from lib.mcu_patterns import MCU_CRASH_PATTERNS, AnomalyType


EVT_MCU_DAY = "mcu_day"
EVT_MCU_NIGHT = "mcu_night"
EVT_CRASH = "crash"


class DaynightLatencyTest(DeviceTestBase):
    _test_name = "daynight_latency"
    _log_dir = "/tmp/daynight_latency_logs"
    _mode_timeout = 30

    def __init__(self):
        super().__init__()
        self.switch_time = None

    def testbot4_read_do(self):
        """Read current DO register value. Returns int or None on failure."""
        return self.testbot4_read()

    def _check_events(self, line, source):
        if source != "MCU":
            return
        if "DayNightStateSet" in line:
            if "eMode = 2" in line or "eMode=2" in line:
                self.event_callback(EVT_MCU_NIGHT, source, line)
            elif "eMode = 1" in line or "eMode=1" in line:
                self.event_callback(EVT_MCU_DAY, source, line)
        for pat in MCU_CRASH_PATTERNS:
            if pat in line:
                self.event_callback(EVT_CRASH, source, line)

    def run(self, num_cycles=5, settle_time=3.0):
        os.makedirs(self._log_dir, exist_ok=True)
        print(f"=== Daynight Shutter Latency Test ({num_cycles} cycles) ===")
        print(f"  Settle time between transitions: {settle_time}s")
        print(f"  Timeout per transition: {self._mode_timeout}s")
        print()

        self.connect_consoles()
        time.sleep(1)

        # Drain stale data
        self.mcu.clear_lines()
        self.clear_events()

        # Read current shutter state and ensure DAY
        do_reg = self.testbot4_read_do()
        if do_reg is None:
            print("[ERROR] Cannot read testbot4 DO register")
            self.disconnect_consoles()
            return 1
        shutter_is_night = bool(do_reg & (1 << DO_AMBLIGHT))
        print(f"[INIT] Current DO register: 0x{do_reg:04X} — "
              f"shutter is {'NIGHT (closed)' if shutter_is_night else 'DAY (open)'}")
        if shutter_is_night:
            print("[INIT] Switching to DAY (DO5 OFF)...")
            self.testbot4_off(DO_AMBLIGHT)
            time.sleep(settle_time)
        else:
            print("[INIT] Already in DAY, good.")
        self.clear_events()

        to_night_latencies = []
        to_day_latencies = []

        for cycle in range(1, num_cycles + 1):
            print(f"\n--- Cycle {cycle}/{num_cycles} ---")

            # Check for crash
            crash = self.check_event(EVT_CRASH)
            if crash:
                print(f"  [ABORT] Crash detected: {crash[2][:100]}")
                self.save_logs(cycle, "crash")
                break

            # Transition: DAY -> NIGHT
            self.clear_events()
            print("  [DAY->NIGHT] Closing shutter (DO5 ON)...")
            self.testbot4_on(DO_AMBLIGHT)
            t_switch = time.time()

            evt = self.wait_for_event(EVT_MCU_NIGHT, self._mode_timeout)
            if evt:
                latency = time.time() - t_switch
                to_night_latencies.append(latency)
                print(f"  [OK] MCU night detected — latency: {latency:.3f}s")
            else:
                print(f"  [FAIL] MCU did not switch to NIGHT within {self._mode_timeout}s")
                self.save_logs(cycle, "no_night")
                break

            time.sleep(settle_time)

            # Transition: NIGHT -> DAY
            self.clear_events()
            print("  [NIGHT->DAY] Opening shutter (DO5 OFF)...")
            self.testbot4_off(DO_AMBLIGHT)
            t_switch = time.time()

            evt = self.wait_for_event(EVT_MCU_DAY, self._mode_timeout)
            if evt:
                latency = time.time() - t_switch
                to_day_latencies.append(latency)
                print(f"  [OK] MCU day detected — latency: {latency:.3f}s")
            else:
                print(f"  [FAIL] MCU did not switch to DAY within {self._mode_timeout}s")
                self.save_logs(cycle, "no_day")
                break

            time.sleep(settle_time)

        # Summary
        print("\n" + "=" * 60)
        print("RESULTS")
        print("=" * 60)

        if to_night_latencies:
            self._print_stats("DAY -> NIGHT", to_night_latencies)
        else:
            print("  DAY -> NIGHT: no successful measurements")

        if to_day_latencies:
            self._print_stats("NIGHT -> DAY", to_day_latencies)
        else:
            print("  NIGHT -> DAY: no successful measurements")

        all_latencies = to_night_latencies + to_day_latencies
        if all_latencies:
            self._print_stats("ALL transitions", all_latencies)

        print()
        self.disconnect_consoles()
        return 0 if all_latencies else 1

    def _print_stats(self, label, values):
        values_sorted = sorted(values)
        avg = sum(values) / len(values)
        median = values_sorted[len(values) // 2]
        print(f"\n  {label} (n={len(values)}):")
        print(f"    Min:    {values_sorted[0]:.3f}s")
        print(f"    Max:    {values_sorted[-1]:.3f}s")
        print(f"    Avg:    {avg:.3f}s")
        print(f"    Median: {median:.3f}s")
        print(f"    All:    {', '.join(f'{v:.3f}' for v in values)}s")


def main():
    parser = argparse.ArgumentParser(description="Measure daynight shutter-to-MCU latency")
    parser.add_argument("-n", "--cycles", type=int, default=5,
                        help="Number of day/night cycles (default: 5)")
    parser.add_argument("--settle", type=float, default=3.0,
                        help="Settle time between transitions in seconds (default: 3.0)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Max wait for MCU mode switch (default: 30.0)")
    args = parser.parse_args()

    test = DaynightLatencyTest()
    test._mode_timeout = args.timeout
    sys.exit(test.run(num_cycles=args.cycles, settle_time=args.settle))


if __name__ == "__main__":
    main()
