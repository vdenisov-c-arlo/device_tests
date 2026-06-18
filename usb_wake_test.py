#!/usr/bin/env python3
"""PEGA-1455: USB plug/unplug doesn't wake ISP from deepsleep.

Scenario A (--scenario a): Race during EPDS wake.
    Device fully asleep → plug USB → WiFi resume beats VBUS ISR → no ISP wake.

Scenario B (--scenario b): Race during sleep-transition window.
    Device awake → unplug USB (triggers ISP off + sleep entry) → re-plug USB
    during the transition → stale wakeup_reason not yet cleared → no ISP wake.

Usage:
    python3 usb_wake_test.py --scenario a -n 50
    python3 usb_wake_test.py --scenario b -n 50

Prerequisites:
    - Device claimed, in Standby mode
    - serial_mux running (MCU on port 9002)
    - Voodoo board reachable (DO6 = USB plug)
"""

import argparse
import time
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import DeviceTestBase, SerialMuxReader
from mcu_patterns import (
    SLEEP_INDICATOR, ISP_OFF_PATTERNS, ISP_WAKE_PATTERNS, SBU_PATTERNS,
    SLEEP_VOTE_PATTERNS, check_for_anomalies, check_mcu_line, check_isp_line,
    AnomalyType, is_crash_dump_line, save_crash_dump,
)

ISP_BOOT_EXPECTED = [
    "Initramfs unpacking",
    "IPL ",
    "DRAM Size:",
    "HW Reset",
]

sys.stdout.reconfigure(line_buffering=True)

USB_DO_CHANNEL = 6
RESET_DO_CHANNEL = 2


def check_result(lines):
    """Analyze MCU log lines after USB plug. Returns (pass, fail_type, detail_lines)."""
    anomaly_type, anomaly_line = check_for_anomalies(lines)
    if anomaly_type != AnomalyType.NONE:
        return False, f"anomaly_{anomaly_type.name.lower()}", [anomaly_line] + lines[-5:]

    saw_sbu = any(any(p in l for p in SBU_PATTERNS) for l in lines)
    saw_isp_wake = any(any(p in l for p in ISP_WAKE_PATTERNS) for l in lines)
    saw_sleep_rejected = any("stay_awake_reasons pending - rejecting sleep" in l
                             or "stay_awake_reasons=0x" in l for l in lines)
    saw_sleep_to_active = any("state:active" in l and "oldstate:sleep" in l for l in lines)
    saw_isp_still_on = any("erpc:[CONNECTED]" in l for l in lines)
    saw_isp_off = any(any(p in l for p in ISP_OFF_PATTERNS) for l in lines)

    relevant = [l for l in lines if any(p in l for p in
                SBU_PATTERNS + ISP_WAKE_PATTERNS + SLEEP_VOTE_PATTERNS +
                ["wakeup", "ISP", "SBU", "reason", "stay_awake"])]

    if saw_isp_wake:
        return True, None, relevant
    elif saw_sleep_rejected and not saw_isp_off:
        return True, None, relevant
    elif saw_sleep_to_active and not saw_isp_off:
        return True, None, relevant
    elif saw_sbu and saw_isp_still_on and not saw_isp_off:
        return True, None, relevant
    elif saw_sbu:
        return False, "no_isp_wake", relevant
    else:
        return False, "no_response", lines[-10:]


class USBWakeTest(DeviceTestBase):
    _test_name = "usb_wake"
    _log_dir = "/tmp/usb_wake_test_logs"
    _sleep_timeout = 120
    _reset_recovery_timeout = 120

    def __init__(self, scenario, num_cycles, no_isp=False, output_dir=None):
        super().__init__()
        self.scenario = scenario
        self.num_cycles = num_cycles
        self.no_isp = no_isp
        self.output_dir = output_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "crash_dumps")
        self._mcu_sleep_event = None
        self._mcu_isp_off_event = None
        self._anomaly_info = None
        self._anomaly_detected = False

    def _check_events(self, line, source):
        """Track sleep, ISP-off, and anomaly events from MCU/ISP lines."""
        if source == "MCU":
            if SLEEP_INDICATOR in line:
                if self._mcu_sleep_event:
                    self._mcu_sleep_event.set()
            if any(p in line for p in ISP_OFF_PATTERNS):
                if self._mcu_isp_off_event:
                    self._mcu_isp_off_event.set()
            if is_crash_dump_line(line):
                self._anomaly_info = (AnomalyType.CRASH, line)
                self._anomaly_detected = True
            else:
                anomaly_type, _ = check_mcu_line(line)
                if anomaly_type != AnomalyType.NONE:
                    self._anomaly_info = (anomaly_type, line)
                    self._anomaly_detected = True

        elif source == "ISP":
            if not any(p in line for p in ISP_BOOT_EXPECTED):
                anomaly_type, _ = check_isp_line(line)
                if anomaly_type != AnomalyType.NONE:
                    self._anomaly_info = (anomaly_type, line)
                    self._anomaly_detected = True

    def _reset_tracking(self):
        """Reset per-cycle event tracking."""
        import threading
        self._mcu_sleep_event = threading.Event()
        self._mcu_isp_off_event = threading.Event()
        self._anomaly_info = None
        self._anomaly_detected = False

    def _check_anomaly(self):
        """Returns (should_abort, source, message)."""
        if self._anomaly_detected:
            atype, line = self._anomaly_info
            return True, "MCU/ISP", f"{atype.name}: {line}"
        return False, None, None

    def _handle_crash_dump(self, cycle):
        """Save crash dump, reset device."""
        time.sleep(3)
        lines = self.mcu.get_lines()
        if self.isp:
            lines += self.isp.get_lines()

        dump_path = save_crash_dump(lines, self.output_dir, self._test_name, cycle, source="mcu")
        if dump_path:
            print(f"  [DUMP] Crash dump saved: {os.path.basename(dump_path)}")
        else:
            os.makedirs(self.output_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback = os.path.join(self.output_dir,
                                    f"crash_{self._test_name}_cycle{cycle}_{ts}.log")
            with open(fallback, "w") as f:
                f.write(f"# Crash context — {self._test_name} cycle {cycle}\n")
                f.write(f"# Time: {datetime.now().isoformat()}\n\n")
                for l in lines[-30:]:
                    f.write(l + "\n")
            dump_path = fallback
            print(f"  [DUMP] Context saved: {os.path.basename(fallback)}")

        self._reset_device()
        return dump_path

    def _reset_device(self):
        """Hardware-reset the DUT and wait for boot."""
        print("  [RESET] Pressing reset button...")
        self.press_button(RESET_DO_CHANNEL, 1.0)
        print("  [RESET] Waiting 60s for device to boot...")
        self.mcu.start_recording()
        if self.isp:
            self.isp.start_recording()
        time.sleep(60)
        self.mcu.drain(2.0)

    def _wait_for_epds(self, timeout=120):
        """Wait for EPDS — either catch message or probe console."""
        self._reset_tracking()
        self.mcu.start_recording()
        self._mcu_sleep_event.clear()
        got_sleep = self._mcu_sleep_event.wait(timeout=30)

        if got_sleep:
            print("  [*] Got 'Network Stack Suspended'")
            time.sleep(10)
            return True

        # May already be in EPDS — probe console
        print("  [*] No sleep message — probing MCU console...")
        self.mcu.start_recording()
        try:
            self.mcu.sock.sendall(b"\r\n\r\n\r\n")
        except OSError:
            pass
        time.sleep(3)
        probe_lines = self.mcu.get_lines()
        mcu_responded = any(">" in l or "$" in l or "#" in l or "mcu:" in l.lower()
                            for l in probe_lines[-5:])
        if not mcu_responded:
            print("  [*] MCU unresponsive — already in EPDS")
            return True

        # MCU still awake, wait longer
        print(f"  [*] MCU still awake, waiting up to {timeout}s...")
        self._mcu_sleep_event.clear()
        got_sleep = self._mcu_sleep_event.wait(timeout=timeout)
        if got_sleep:
            print("  [*] Got 'Network Stack Suspended'")
            time.sleep(10)
            return True

        return False

    # --- Scenario A ---

    def _run_scenario_a(self):
        print(f"=== PEGA-1455 Scenario A: USB Wake from EPDS ===")
        print(f"Cycles: {self.num_cycles}")
        print(f"Race: WiFi resume sets wakeup_reason before VBUS ISR")
        print()

        results = {"pass": 0, "fail_no_isp_wake": 0, "fail_timeout": 0, "fail_crash": 0}
        crash_dumps = []

        for cycle in range(1, self.num_cycles + 1):
            print(f"\n--- Cycle {cycle}/{self.num_cycles} [{datetime.now().strftime('%H:%M:%S')}] ---")

            # Ensure USB unplugged
            print("  [1] Unplug USB (DO6 OFF)")
            self.voodoo_off(USB_DO_CHANNEL)

            # Wait for EPDS
            print("  [2] Waiting for EPDS...")
            if not self._wait_for_epds():
                print("  [!] TIMEOUT — device didn't sleep")
                results["fail_timeout"] += 1
                self.voodoo_on(USB_DO_CHANNEL)
                time.sleep(5)
                continue

            # Plug USB
            self._reset_tracking()
            self.mcu.start_recording()
            if self.isp:
                self.isp.start_recording()
            print("  [3] Plug USB (DO6 ON)")
            self.voodoo_on(USB_DO_CHANNEL)

            # Observe
            print("  [4] Observing logs for 15s...")
            time.sleep(15)
            self.mcu.stop_recording()
            if self.isp:
                self.isp.stop_recording()

            # Check for anomaly
            abort, source, msg = self._check_anomaly()
            if abort:
                print(f"  [!] {source} anomaly (unrelated crash): {msg}")
                results["fail_crash"] += 1
                path = self._handle_crash_dump(cycle)
                if path:
                    crash_dumps.append(path)
                continue

            passed, fail_type, detail = check_result(self.mcu.get_lines())
            if passed:
                print("  [PASS] ISP wake detected")
                results["pass"] += 1
            else:
                if fail_type and fail_type.startswith("anomaly_"):
                    print(f"  [!] {fail_type} (unrelated crash)")
                    results["fail_crash"] += 1
                    path = self._handle_crash_dump(cycle)
                    if path:
                        crash_dumps.append(path)
                    continue
                elif fail_type == "no_isp_wake":
                    print("  [FAIL] SBU seen but NO ISP wake — BUG REPRODUCED!")
                else:
                    print("  [FAIL] No response at all — device may not have woken")
                results["fail_no_isp_wake"] += 1
                for line in (detail or [])[:10]:
                    print(f"        {line}")

            time.sleep(5)

        if crash_dumps:
            print(f"\n  Crash dumps saved ({len(crash_dumps)}):")
            for p in crash_dumps:
                print(f"    {p}")

        return results

    # --- Scenario B ---

    def _run_scenario_b(self):
        print(f"=== PEGA-1455 Scenario B: USB Plug/Unplug/Replug/Sleep ===")
        print(f"Cycles: {self.num_cycles}")
        print(f"Sequence: plug→unplug→replug(verify wake)→unplug(verify sleep)")
        print()

        results = {"pass": 0, "fail_no_wake": 0, "fail_no_sleep": 0, "fail_crash": 0}
        crash_dumps = []

        for cycle in range(1, self.num_cycles + 1):
            print(f"\n--- Cycle {cycle}/{self.num_cycles} [{datetime.now().strftime('%H:%M:%S')}] ---")

            # Step 1: Start from USB plugged, ISP/MCU awake
            print("  [1] Ensure USB plugged — device awake")
            self.voodoo_on(USB_DO_CHANNEL)
            self._reset_tracking()
            self.mcu.start_recording()
            if self.isp:
                self.isp.start_recording()
            time.sleep(5)

            try:
                self.mcu.sock.sendall(b"\r\n")
            except OSError:
                pass
            time.sleep(2)

            abort, source, msg = self._check_anomaly()
            if abort:
                print(f"  [!] {source} anomaly at start (unrelated crash): {msg}")
                results["fail_crash"] += 1
                path = self._handle_crash_dump(cycle)
                if path:
                    crash_dumps.append(path)
                continue

            # Step 2: Unplug USB — observe sleep flow starting
            self._reset_tracking()
            self.mcu.start_recording()
            if self.isp:
                self.isp.start_recording()
            print("  [2] Unplug USB — observing sleep flow...")
            self.voodoo_off(USB_DO_CHANNEL)

            sleep_starting = False
            deadline = time.time() + 30
            while time.time() < deadline:
                if self._mcu_isp_off_event and self._mcu_isp_off_event.is_set():
                    print("  [2] ISP off detected — sleep flow active")
                    sleep_starting = True
                    break
                lines = self.mcu.get_lines()
                if any(any(p in l for p in SLEEP_VOTE_PATTERNS) for l in lines[-3:]):
                    print("  [2] Sleep vote detected — sleep flow active")
                    sleep_starting = True
                    break
                if self._mcu_sleep_event and self._mcu_sleep_event.is_set():
                    print("  [2] Already in EPDS")
                    sleep_starting = True
                    break
                abort, source, msg = self._check_anomaly()
                if abort:
                    break
                time.sleep(0.2)

            if abort:
                print(f"  [!] {source} anomaly during unplug (unrelated crash): {msg}")
                results["fail_crash"] += 1
                path = self._handle_crash_dump(cycle)
                if path:
                    crash_dumps.append(path)
                continue

            if not sleep_starting:
                lines = self.mcu.get_lines()
                if any(is_crash_dump_line(l) for l in lines):
                    print("  [!] Crash dump detected (unrelated) — resetting")
                    results["fail_crash"] += 1
                    path = self._handle_crash_dump(cycle)
                    if path:
                        crash_dumps.append(path)
                    continue
                print("  [!] No sleep flow detected within 30s — resetting")
                results["fail_no_sleep"] += 1
                self._reset_device()
                continue

            # Step 3: Quickly plug back — verify ISP remains ON or wakes
            self._reset_tracking()
            self.mcu.start_recording()
            if self.isp:
                self.isp.start_recording()
            print("  [3] Replug USB — verifying ISP stays on or wakes...")
            self.voodoo_on(USB_DO_CHANNEL)

            obs_deadline = time.time() + 60
            abort = False
            while time.time() < obs_deadline:
                time.sleep(5)
                try:
                    self.mcu.sock.sendall(b"\r\n")
                except OSError:
                    pass
                lines = self.mcu.get_lines()
                if any(any(p in l for p in ISP_WAKE_PATTERNS) for l in lines):
                    break
                abort, source, msg = self._check_anomaly()
                if abort:
                    break

            if abort:
                print(f"  [!] {source} anomaly after replug (unrelated crash): {msg}")
                results["fail_crash"] += 1
                path = self._handle_crash_dump(cycle)
                if path:
                    crash_dumps.append(path)
                continue

            passed, fail_type, detail = check_result(self.mcu.get_lines())
            if not passed:
                print(f"  [FAIL] ISP did not wake/stay on after replug ({fail_type})")
                results["fail_no_wake"] += 1
                for line in (detail or [])[:10]:
                    print(f"        {line}")
                self._reset_device()
                continue

            print("  [3] PASS — ISP on after replug")

            # Step 4: Stabilize
            print("  [4] Waiting 15s...")
            time.sleep(15)

            abort, source, msg = self._check_anomaly()
            if abort:
                print(f"  [!] {source} anomaly during stabilize (unrelated crash): {msg}")
                results["fail_crash"] += 1
                path = self._handle_crash_dump(cycle)
                if path:
                    crash_dumps.append(path)
                continue

            # Step 5: Unplug again — verify full sleep
            self._reset_tracking()
            self.mcu.start_recording()
            if self.isp:
                self.isp.start_recording()
            print("  [5] Unplug USB again — expecting full sleep within 60s...")
            self.voodoo_off(USB_DO_CHANNEL)

            slept = False
            sleep_deadline = time.time() + 60
            abort = False
            while time.time() < sleep_deadline:
                if self._mcu_sleep_event and self._mcu_sleep_event.is_set():
                    slept = True
                    break
                abort, source, msg = self._check_anomaly()
                if abort:
                    break
                time.sleep(1)

            if abort:
                print(f"  [!] {source} anomaly during final sleep (unrelated crash): {msg}")
                results["fail_crash"] += 1
                path = self._handle_crash_dump(cycle)
                if path:
                    crash_dumps.append(path)
                continue

            if slept:
                print("  [5] Device entered deep sleep — PASS")
                results["pass"] += 1
            else:
                lines = self.mcu.get_lines()
                if any(is_crash_dump_line(l) for l in lines):
                    print("  [!] Crash dump in output (unrelated) — resetting")
                    results["fail_crash"] += 1
                    path = self._handle_crash_dump(cycle)
                    if path:
                        crash_dumps.append(path)
                    continue
                print("  [5] FAIL — device did not sleep within 60s")
                results["fail_no_sleep"] += 1
                for line in lines[-10:]:
                    print(f"        {line}")

        if crash_dumps:
            print(f"\n  Crash dumps saved ({len(crash_dumps)}):")
            for p in crash_dumps:
                print(f"    {p}")

        return results

    # --- Overrides ---

    def run_cycle(self, cycle_num):
        # Not used — we override run() entirely
        pass

    def run(self, num_cycles=None):
        """Override base run to use scenario-specific flow."""
        os.makedirs(self._log_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        print("[INIT] Connecting MCU...")
        self.connect_consoles() if not self.no_isp else self._connect_mcu_only()

        print("[INIT] Draining stale buffers...")
        self.mcu.drain(2.0)
        if self.isp:
            self.isp.drain(2.0)
            self.isp.sock and self.isp.init_console() if hasattr(self.isp, 'init_console') else None

        if self.scenario == "a":
            results = self._run_scenario_a()
        else:
            results = self._run_scenario_b()

        self.disconnect_consoles()

        print(f"\n{'='*50}")
        print(f"RESULTS: {self.num_cycles} cycles (Scenario {self.scenario.upper()})")
        print(f"  PASS:              {results.get('pass', 0)}")
        print(f"  FAIL (no wake):    {results.get('fail_no_isp_wake', 0) + results.get('fail_no_wake', 0)}")
        print(f"  FAIL (no sleep):   {results.get('fail_timeout', 0) + results.get('fail_no_sleep', 0)}")
        print(f"  FAIL (crash):      {results.get('fail_crash', 0)}")
        total_fail = sum(v for k, v in results.items() if k != "pass")
        if total_fail > 0:
            print(f"\n  >>> FAILURES: {total_fail}/{self.num_cycles} cycles <<<")
        print()
        return 1 if total_fail > 0 else 0

    def _connect_mcu_only(self):
        """Connect MCU only (when --no-isp)."""
        self.mcu = SerialMuxReader(
            "MCU", self._cfg['mcu_host'], self._cfg['mcu_port'],
            event_callback=self._line_callback)
        self.mcu.connect()
        self.mcu.start()
        self.isp = None

    def connect_consoles(self):
        """Override to add ISP init_console call."""
        super().connect_consoles()
        if self.isp and self.isp.sock:
            from console_utils import isp_init_console
            isp_init_console(self.isp.sock)
            print(f"[*] ISP console connected ({self._cfg['isp_host']}:{self._cfg['isp_port']})")


def main():
    parser = argparse.ArgumentParser(description="PEGA-1455 USB wake race condition test")
    parser.add_argument("--scenario", "-s", choices=["a", "b"], default="a",
                        help="a=wake from EPDS, b=sleep-transition window")
    parser.add_argument("-n", "--cycles", type=int, default=50,
                        help="Number of test cycles (default 50)")
    parser.add_argument("--no-isp", action="store_true",
                        help="Skip ISP console monitoring (if port 9001 unavailable)")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Directory for crash dump files (default: ./crash_dumps)")
    args = parser.parse_args()

    test = USBWakeTest(
        scenario=args.scenario,
        num_cycles=args.cycles,
        no_isp=args.no_isp,
        output_dir=args.output_dir,
    )
    sys.exit(test.run())


if __name__ == "__main__":
    main()
