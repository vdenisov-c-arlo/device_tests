#!/usr/bin/env python3
"""PEGA-1695: MCU coredump in xagent_erpc_client_request_complete() during USB unplug.

Tests the race between xagent erpc_request_worker_func calling the generated eRPC
client stub and arlo_erpc_deinit() NULLing the client during ISP power-off triggered
by USB unplug.

The test:
  1. Ensures USB is plugged and device is in always-on mode (ISP active, xagent running)
  2. Generates xagent eRPC traffic via ISP console (arlocmd)
  3. Immediately unplugs USB to trigger ISP sleep vote + eRPC teardown
  4. Monitors MCU console for crash/coredump
  5. Waits for device to recover (ISP powers back on or device sleeps+wakes)
  6. Repeats N cycles

The bug has ~1% hit rate, so run with high cycle count (e.g., -n 200).

Usage:
    python3 erpc_usb_unplug_crash_test.py -n 200
    python3 erpc_usb_unplug_crash_test.py -n 50 --no-isp

Prerequisites:
    - Device claimed, in always-on mode (100% or battery >95% with USB)
    - serial_mux running (MCU 9002, ISP 9001)
    - testbot4 reachable (DO6 = USB plug)
"""

import argparse
import os
import sys
import time
from datetime import datetime
from enum import Enum, auto

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase, SerialMuxReader, isp_init_console, _recv_all, _drain_sock
from lib.mcu_patterns import (
    SLEEP_INDICATOR, ISP_OFF_PATTERNS, SBU_PATTERNS,
    check_mcu_line, AnomalyType, is_crash_dump_line, save_crash_dump,
)

sys.stdout.reconfigure(line_buffering=True)

from testbot4.testbot4_channels import DO_USB as USB_DO_CHANNEL, DO_RESET as RESET_DO_CHANNEL

CRASH_TARGET_PATTERNS = [
    "Core dump:",
    "core dump",
    "xagent_erpc_client_request_complete",
]

ARLOCMD_DIAGNOSTICS = 'arlocmd \'{"action":"get","resource":"diagnostics/logLevel","responseUrl":"http://127.0.0.1:4447/test"}\''
ARLOCMD_BATTERY = 'arlocmd \'{"action":"get","resource":"battery","responseUrl":"http://127.0.0.1:4447/test"}\''


class Event(Enum):
    SLEEP = auto()
    ISP_OFF = auto()
    SBU_REMOVE = auto()
    SBU_INSERT = auto()
    CRASH = auto()
    ERPC_DEINIT = auto()
    ISP_READY = auto()


class State(Enum):
    ENSURE_PLUGGED = auto()
    WAIT_ISP_READY = auto()
    GENERATE_TRAFFIC = auto()
    UNPLUG_USB = auto()
    OBSERVE_CRASH = auto()
    REPLUG_RECOVER = auto()
    WAIT_RECOVERY = auto()
    PASS = auto()
    FAIL_CRASH = auto()
    FAIL_NO_RECOVER = auto()


TERMINAL_STATES = {State.PASS, State.FAIL_CRASH, State.FAIL_NO_RECOVER}


def _isp_login(sock):
    """Blind login to ISP console — break stale commands, send root/arlo unconditionally."""
    # Break any running command (tail -f, etc.)
    for _ in range(3):
        sock.sendall(b"\x03")
        time.sleep(0.2)
    time.sleep(1)
    # Blind login: works whether at login prompt or already at shell
    sock.sendall(b"root\r\n")
    time.sleep(1)
    sock.sendall(b"arlo\r\n")
    time.sleep(1)
    return True


class ErpcUsbUnplugCrashTest(DeviceTestBase):
    _test_name = "erpc_usb_unplug_crash"
    _log_dir = "/tmp/erpc_usb_crash_logs"
    _sleep_timeout = 60
    _reset_recovery_timeout = 120

    def __init__(self, num_cycles, no_isp=False, traffic_burst=3,
                 unplug_delay_ms=100, output_dir=None):
        super().__init__()
        self.num_cycles = num_cycles
        self.no_isp = no_isp
        self.traffic_burst = traffic_burst
        self.unplug_delay_ms = unplug_delay_ms
        self.output_dir = output_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "crash_dumps")
        self._session_log = None
        self._cycle_num = 0

    def _check_events(self, line, source):
        if source == "MCU":
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP, source, line)
            if any(p in line for p in ISP_OFF_PATTERNS):
                self.event_callback(Event.ISP_OFF, source, line)
            if "USB plugged out" in line or "SBU_Remove" in line:
                self.event_callback(Event.SBU_REMOVE, source, line)
            if "USB plugged in" in line or "SBU_Insert" in line:
                self.event_callback(Event.SBU_INSERT, source, line)
            if any(p in line for p in CRASH_TARGET_PATTERNS):
                self.event_callback(Event.CRASH, source, line)
            elif is_crash_dump_line(line):
                self.event_callback(Event.CRASH, source, line)
            else:
                anomaly_type, _ = check_mcu_line(line)
                if anomaly_type != AnomalyType.NONE:
                    self.event_callback(Event.CRASH, source, line)
            if "eRPC service deinitialized" in line:
                self.event_callback(Event.ERPC_DEINIT, source, line)
            if "pga_erpc_notify_isp_ready" in line or "isIspReady = true" in line:
                self.event_callback(Event.ISP_READY, source, line)

        elif source == "ISP":
            if "Core dump" in line or "core dump" in line:
                self.event_callback(Event.CRASH, source, line)

        if self._session_log:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self._session_log.write(f"[{ts}] [{source}] {line}\n")
            self._session_log.flush()

    def _send_isp_command(self, cmd):
        """Send a command to ISP console (requires ISP to be logged in)."""
        if not self.isp or not self.isp.sock:
            return False
        try:
            self.isp.sock.sendall(f"{cmd}\r\n".encode())
            return True
        except OSError:
            return False

    def _run_state_machine(self, cycle):
        state = State.ENSURE_PLUGGED

        while state not in TERMINAL_STATES:
            state = self._transition(state, cycle)

        return state

    def _transition(self, state, cycle):
        if state == State.ENSURE_PLUGGED:
            print("  [1] Ensure USB plugged (DO6 ON)")
            self.testbot4_on(USB_DO_CHANNEL)
            self.clear_events()
            self.mcu.start_recording()
            if self.isp:
                self.isp.start_recording()
            time.sleep(3)

            crash = self.check_event(Event.CRASH)
            if crash:
                print(f"    [CRASH] {crash[2][:120]}")
                return State.FAIL_CRASH
            return State.WAIT_ISP_READY

        elif state == State.WAIT_ISP_READY:
            print("  [2] Checking ISP ready...")

            # Active probe: login to ISP shell (no tail -f)
            if self.isp and self.isp.sock:
                try:
                    if _isp_login(self.isp.sock):
                        print("    ISP logged in")
                        return State.GENERATE_TRAFFIC
                except OSError:
                    pass

            # Fallback: wait for MCU notification (fresh boot scenario)
            result = self.wait_for_any_event(
                {Event.ISP_READY, Event.CRASH}, timeout=30)

            if result and result[0] == Event.CRASH:
                print(f"    [CRASH] {result[2][:120]}")
                return State.FAIL_CRASH
            elif result and result[0] == Event.ISP_READY:
                print("    ISP ready (MCU notification)")
                if self.isp and self.isp.sock:
                    _isp_login(self.isp.sock)
                time.sleep(2)
                return State.GENERATE_TRAFFIC

            print("    ISP state unknown — proceeding")
            return State.GENERATE_TRAFFIC

        elif state == State.GENERATE_TRAFFIC:
            print(f"  [3] Generating xagent traffic (burst={self.traffic_burst})...")
            if self.isp and self.isp.sock:
                for i in range(self.traffic_burst):
                    cmd = ARLOCMD_DIAGNOSTICS if i % 2 == 0 else ARLOCMD_BATTERY
                    self._send_isp_command(cmd)
                    time.sleep(0.05)
            else:
                print("    [WARN] No ISP console — skipping traffic gen")
                time.sleep(1)

            time.sleep(self.unplug_delay_ms / 1000.0)
            return State.UNPLUG_USB

        elif state == State.UNPLUG_USB:
            self.clear_events()
            self.mcu.clear_lines()
            self.mcu.start_recording()
            if self.isp:
                self.isp.clear_lines()
                self.isp.start_recording()

            print("  [4] Unplug USB (DO6 OFF)")
            self.testbot4_off(USB_DO_CHANNEL)
            return State.OBSERVE_CRASH

        elif state == State.OBSERVE_CRASH:
            print("  [5] Observing for crash (15s)...")
            result = self.wait_for_any_event(
                {Event.CRASH, Event.ISP_OFF, Event.SLEEP}, timeout=15)

            if result and result[0] == Event.CRASH:
                print(f"    [CRASH] {result[2][:120]}")
                return State.FAIL_CRASH

            # Check all collected lines for crash evidence
            lines = self.mcu.get_lines()
            for line in lines:
                if any(p in line for p in CRASH_TARGET_PATTERNS):
                    print(f"    [CRASH] {line[:120]}")
                    return State.FAIL_CRASH
                if is_crash_dump_line(line):
                    print(f"    [CRASH] Crash dump detected")
                    return State.FAIL_CRASH

            # Report what we saw
            saw_isp_off = any(any(p in l for p in ISP_OFF_PATTERNS) for l in lines)
            saw_sleep = any(SLEEP_INDICATOR in l for l in lines)
            saw_erpc_deinit = any("eRPC service deinitialized" in l for l in lines)

            status_parts = []
            if saw_erpc_deinit:
                status_parts.append("eRPC deinit")
            if saw_isp_off:
                status_parts.append("ISP off")
            if saw_sleep:
                status_parts.append("EPDS")
            if status_parts:
                print(f"    No crash. Saw: {', '.join(status_parts)}")
            else:
                print("    No crash, no sleep transition observed")

            return State.REPLUG_RECOVER

        elif state == State.REPLUG_RECOVER:
            print("  [6] Replug USB (DO6 ON) — recovering")
            self.clear_events()
            self.mcu.clear_lines()
            self.mcu.start_recording()
            self.testbot4_on(USB_DO_CHANNEL)
            return State.WAIT_RECOVERY

        elif state == State.WAIT_RECOVERY:
            print("  [7] Waiting for device to recover (45s)...")
            result = self.wait_for_any_event(
                {Event.ISP_READY, Event.CRASH, Event.SBU_INSERT}, timeout=45)

            if result and result[0] == Event.CRASH:
                print(f"    [CRASH during recovery] {result[2][:120]}")
                return State.FAIL_CRASH

            # Give extra time for ISP to boot and xagent to reconnect
            time.sleep(10)

            # Check for late crash
            crash = self.check_event(Event.CRASH)
            if crash:
                print(f"    [LATE CRASH] {crash[2][:120]}")
                return State.FAIL_CRASH

            print("    Recovered")
            return State.PASS

        return state

    def _handle_crash(self, cycle):
        print("  [COREDUMP] Waiting for dump to complete (10s)...")
        time.sleep(10)
        lines = self.mcu.get_lines() if self.mcu else []

        dump_path = save_crash_dump(
            lines, self.output_dir, self._test_name, cycle, source="mcu")
        if dump_path:
            print(f"  [DUMP] Saved: {os.path.basename(dump_path)}")
        else:
            os.makedirs(self.output_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback = os.path.join(
                self.output_dir, f"crash_{self._test_name}_cycle{cycle}_{ts}.log")
            with open(fallback, "w") as f:
                f.write(f"# Crash context — {self._test_name} cycle {cycle}\n")
                f.write(f"# Time: {datetime.now().isoformat()}\n\n")
                for l in lines[-50:]:
                    f.write(l + "\n")
            dump_path = fallback
            print(f"  [DUMP] Context saved: {os.path.basename(fallback)}")

        return dump_path

    def _reset_device(self):
        print("  [RESET] Pressing reset button...")
        self.press_button(RESET_DO_CHANNEL, 1.0)
        print("  [RESET] Waiting for boot (15s)...")
        time.sleep(15)
        self.clear_events()
        self.mcu.clear_lines()
        self.mcu.start_recording()

        # Ensure USB is plugged for always-on mode
        self.testbot4_on(USB_DO_CHANNEL)
        print("  [RESET] USB plugged, waiting for ISP ready (90s)...")
        result = self.wait_for_any_event(
            {Event.ISP_READY, Event.CRASH}, timeout=90)
        if result and result[0] == Event.ISP_READY:
            print("  [RESET] Device recovered — ISP ready")
            time.sleep(5)
            return True
        elif result and result[0] == Event.CRASH:
            print("  [RESET] Crash during recovery!")
            return False

        # Check if ISP is already up
        lines = self.mcu.get_lines()
        if any("isIspReady" in l or "ISP_POWER_IS_ON" in l for l in lines):
            print("  [RESET] ISP appears ready")
            time.sleep(5)
            return True

        print("  [RESET] Timeout — ISP not ready")
        return False

    def _connect_mcu_only(self):
        self.mcu = SerialMuxReader(
            "MCU", self._cfg['mcu_host'], self._cfg['mcu_port'],
            event_callback=self._line_callback)
        self.mcu.connect()
        self.mcu.start()
        self.isp = None

    def run(self, num_cycles=None):
        os.makedirs(self._log_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_path = os.path.join(
            self._log_dir, f"session_{session_ts}.log")
        self._session_log = open(session_path, "w")
        self._session_log.write(
            f"# PEGA-1695 eRPC USB Unplug Crash Test\n"
            f"# Started: {datetime.now().isoformat()}\n"
            f"# Cycles: {self.num_cycles}\n"
            f"# Traffic burst: {self.traffic_burst}\n"
            f"# Unplug delay: {self.unplug_delay_ms}ms\n\n")

        print(f"=== PEGA-1695 eRPC USB Unplug Crash Test ===")
        print(f"Cycles: {self.num_cycles}")
        print(f"Traffic burst: {self.traffic_burst} commands")
        print(f"Unplug delay: {self.unplug_delay_ms}ms after traffic")
        print(f"Session log: {session_path}")
        print()

        print("[INIT] Connecting consoles...")
        if self.no_isp:
            self._connect_mcu_only()
            print("  MCU only (no ISP console)")
        else:
            self.connect_consoles()
            print("  MCU + ISP connected")
            print("[INIT] Logging into ISP console...")
            _isp_login(self.isp.sock)
            time.sleep(1)

        print("[INIT] Draining stale buffers...")
        self.mcu.drain(2.0)
        if self.isp:
            self.isp.drain(2.0)
        self.clear_events()
        print("[INIT] Ready\n")

        results = {"pass": 0, "crash": 0}
        crash_dumps = []

        for cycle in range(1, self.num_cycles + 1):
            self._cycle_num = cycle
            print(f"\n{'='*60}")
            print(f"[CYCLE {cycle}/{self.num_cycles}] "
                  f"[{datetime.now().strftime('%H:%M:%S')}]  "
                  f"pass={results['pass']} crash={results['crash']}")
            print(f"{'='*60}")

            self._session_log.write(f"\n--- CYCLE {cycle} ---\n")

            final_state = self._run_state_machine(cycle)

            if final_state == State.PASS:
                results["pass"] += 1

            elif final_state == State.FAIL_CRASH:
                results["crash"] += 1
                path = self._handle_crash(cycle)
                if path:
                    crash_dumps.append(path)
                self.save_logs(cycle, "crash")
                if not self._reset_device():
                    print("  [ABORT] Cannot recover — stopping test")
                    break

            elif final_state == State.FAIL_NO_RECOVER:
                results["crash"] += 1
                self.save_logs(cycle, "no_recover")
                if not self._reset_device():
                    print("  [ABORT] Cannot recover — stopping test")
                    break

        self.disconnect_consoles()
        self._session_log.write(f"\n# Ended: {datetime.now().isoformat()}\n")
        self._session_log.close()

        total_run = sum(results.values())
        print(f"\n{'='*60}")
        print(f"RESULTS: {total_run} cycles run")
        print(f"  PASS:   {results['pass']}")
        print(f"  CRASH:  {results['crash']}")
        if crash_dumps:
            print(f"\n  Crash dumps ({len(crash_dumps)}):")
            for p in crash_dumps:
                print(f"    {p}")
        print(f"\n  Session log: {session_path}")
        print(f"{'='*60}")

        if results["crash"] > 0:
            crash_pct = results["crash"] / total_run * 100
            print(f"\n  >>> CRASHES: {results['crash']}/{total_run} "
                  f"({crash_pct:.1f}%) — BUG CONFIRMED <<<")

        return 1 if results["crash"] > 0 else 0


def main():
    parser = argparse.ArgumentParser(
        description="PEGA-1695: eRPC crash during USB unplug stress test")
    parser.add_argument("-n", "--cycles", type=int, default=100,
                        help="Number of test cycles (default 100)")
    parser.add_argument("--no-isp", action="store_true",
                        help="Skip ISP console (MCU monitoring only, no traffic gen)")
    parser.add_argument("--burst", type=int, default=3,
                        help="Number of arlocmd commands per cycle (default 3)")
    parser.add_argument("--delay", type=int, default=100,
                        help="Delay in ms between traffic and unplug (default 100)")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Directory for crash dump files")
    args = parser.parse_args()

    test = ErpcUsbUnplugCrashTest(
        num_cycles=args.cycles,
        no_isp=args.no_isp,
        traffic_burst=args.burst,
        unplug_delay_ms=args.delay,
        output_dir=args.output_dir,
    )
    sys.exit(test.run())


if __name__ == "__main__":
    main()
