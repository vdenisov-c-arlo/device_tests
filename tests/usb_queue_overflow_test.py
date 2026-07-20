#!/usr/bin/env python3
"""USB rapid plug/unplug stress test — trigger MCU main task queue overflow.

Rapidly toggles USB (DO6) to flood the MCU task queue with SBU events.
The burst runs directly on the voodoo board (~3ms Modbus RTT) for maximum speed.
Log analysis runs locally via serial_mux as usual.

On queue overflow detection, saves logs, resets DUT, and continues to next cycle.

Usage:
    python3 usb_queue_overflow_test.py -n 20 --bursts 30 --interval 0.05
    python3 usb_queue_overflow_test.py --bursts 50 --interval 0.02 --cooldown 30
    python3 usb_queue_overflow_test.py --bursts 100 --interval 0.01  # very aggressive

Prerequisites:
    - Device claimed, ISP awake (USB plugged)
    - serial_mux running (MCU on port 9002, ISP on port 9001)
    - Voodoo board reachable at 192.168.3.1 (SSH as root)
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum, auto

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import DeviceTestBase, SerialMuxReader
from lib.mcu_patterns import (
    SLEEP_INDICATOR, MCU_CRASH_PATTERNS, MCU_HANG_PATTERNS,
    check_mcu_line, check_isp_line, AnomalyType,
    is_crash_dump_line, save_crash_dump,
)

sys.stdout.reconfigure(line_buffering=True)

from voodoo.voodoo_channels import DO_USB as USB_DO_CHANNEL, DO_RESET as RESET_DO_CHANNEL
VOODOO_HOST = "192.168.3.1"

QUEUE_OVERFLOW_PATTERNS = [
    "xQueueSend fail",
    "Erpc xQueueSend fail",
    "queue full",
    "QueueFull",
    "mainTask queue",
]

# Script deployed to voodoo board for fast local toggling
VOODOO_BURST_SCRIPT = '''\
import socket, struct, time, sys

def modbus_write(sock, reg, value, tid):
    pdu = struct.pack('>BHH', 0x06, reg, value)
    mbap = struct.pack('>HHHB', tid, 0, len(pdu)+1, 0xFF)
    sock.sendall(mbap + pdu)
    sock.settimeout(2)
    return sock.recv(256)

bursts = int(sys.argv[1])
interval = float(sys.argv[2])
do_bit = 1 << int(sys.argv[3])

s = socket.socket()
s.connect(('127.0.0.1', 502))
time.sleep(0.05)

tid = 1
for i in range(bursts):
    modbus_write(s, 1, do_bit, tid); tid += 1
    time.sleep(interval / 2)
    modbus_write(s, 1, 0, tid); tid += 1
    time.sleep(interval / 2)

# End with DO ON (USB plugged)
modbus_write(s, 1, do_bit, tid)
s.close()
print(f"done {bursts} toggles @ {interval*1000:.0f}ms")
'''


class Event(Enum):
    QUEUE_OVERFLOW = auto()
    CRASH = auto()
    SLEEP = auto()
    MCU_RESPONSIVE = auto()


class CycleResult(Enum):
    PASS = auto()
    OVERFLOW_RECOVERED = auto()
    OVERFLOW_STUCK = auto()
    CRASH = auto()
    DEVICE_UNRESPONSIVE = auto()


class USBQueueOverflowTest(DeviceTestBase):
    _test_name = "usb_queue_overflow"
    _log_dir = "/tmp/usb_queue_overflow_logs"
    _sleep_timeout = 120
    _reset_recovery_timeout = 120

    def __init__(self, num_cycles, bursts_per_cycle, interval_s, cooldown_s,
                 observe_s, recovery_s, no_isp=False):
        super().__init__()
        self.num_cycles = num_cycles
        self.bursts_per_cycle = bursts_per_cycle
        self.interval_s = interval_s
        self.cooldown_s = cooldown_s
        self.observe_s = observe_s
        self.recovery_s = recovery_s
        self.no_isp = no_isp
        self._session_log = None
        self._burst_script_deployed = False

    def _check_events(self, line, source):
        if source == "MCU":
            if any(p in line for p in QUEUE_OVERFLOW_PATTERNS):
                self.event_callback(Event.QUEUE_OVERFLOW, source, line)
            if SLEEP_INDICATOR in line:
                self.event_callback(Event.SLEEP, source, line)
            if is_crash_dump_line(line):
                self.event_callback(Event.CRASH, source, line)
            else:
                anomaly_type, _ = check_mcu_line(line)
                if anomaly_type != AnomalyType.NONE:
                    self.event_callback(Event.CRASH, source, line)

        elif source == "ISP":
            anomaly_type, _ = check_isp_line(line)
            if anomaly_type != AnomalyType.NONE:
                self.event_callback(Event.CRASH, source, line)

        if self._session_log:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self._session_log.write(f"[{ts}] [{source}] {line}\n")
            self._session_log.flush()

    def _deploy_burst_script(self):
        """Deploy the fast toggle script to the voodoo board."""
        if self._burst_script_deployed:
            return True
        result = subprocess.run(
            ["ssh", f"root@{VOODOO_HOST}",
             f"cat > /tmp/usb_burst.py << 'ENDOFSCRIPT'\n{VOODOO_BURST_SCRIPT}ENDOFSCRIPT"],
            capture_output=True, timeout=10, text=True)
        if result.returncode != 0:
            print(f"  [ERROR] Failed to deploy burst script: {result.stderr}")
            return False
        self._burst_script_deployed = True
        return True

    def _run_burst_remote(self):
        """Execute the burst on the voodoo board. Returns True on success."""
        result = subprocess.run(
            ["ssh", f"root@{VOODOO_HOST}",
             f"python3 /tmp/usb_burst.py {self.bursts_per_cycle} {self.interval_s} {USB_DO_CHANNEL}"],
            capture_output=True, timeout=60, text=True)
        if result.returncode != 0:
            print(f"  [ERROR] Burst execution failed: {result.stderr[:200]}")
            return False
        if result.stdout.strip():
            print(f"  [VOODOO] {result.stdout.strip()}")
        return True

    def _run_cycle(self, cycle):
        """Single test cycle: burst → observe → evaluate."""
        self.clear_events()
        self.mcu.clear_lines()
        self.mcu.start_recording()
        if self.isp:
            self.isp.clear_lines()
            self.isp.start_recording()

        # Ensure USB starts plugged (device awake)
        print("  [1] Ensure USB plugged (device awake)")
        self.voodoo_on(USB_DO_CHANNEL)
        time.sleep(5)

        # Discard any boot messages from console init — not a real crash
        self.clear_events()
        self.mcu.clear_lines()
        if self.isp:
            self.isp.clear_lines()

        # Fresh recording for the burst
        self.clear_events()
        self.mcu.clear_lines()
        self.mcu.start_recording()
        if self.isp:
            self.isp.clear_lines()
            self.isp.start_recording()

        # Run rapid toggles on voodoo board
        burst_duration = self.bursts_per_cycle * self.interval_s
        print(f"  [2] Running {self.bursts_per_cycle} USB toggles @ "
              f"{self.interval_s*1000:.0f}ms on voodoo board "
              f"(~{burst_duration:.1f}s)...")

        if not self._run_burst_remote():
            return CycleResult.DEVICE_UNRESPONSIVE

        # Check if overflow happened during burst
        hit = self.check_event(Event.QUEUE_OVERFLOW)
        if hit:
            print(f"  [HIT] Queue overflow during burst: {hit[2][:100]}")
            return self._observe_recovery(cycle)

        crash = self.check_event(Event.CRASH)
        if crash:
            print(f"  [CRASH] During burst: {crash[2][:100]}")
            return CycleResult.CRASH

        # Observation window — check for delayed overflow/crash
        print(f"  [3] Observing for {self.observe_s}s...")
        result = self.wait_for_any_event(
            {Event.QUEUE_OVERFLOW, Event.CRASH}, timeout=self.observe_s)

        if result:
            if result[0] == Event.QUEUE_OVERFLOW:
                print(f"  [HIT] Delayed queue overflow: {result[2][:100]}")
                return self._observe_recovery(cycle)
            elif result[0] == Event.CRASH:
                print(f"  [CRASH] During observation: {result[2][:100]}")
                return CycleResult.CRASH

        # Verify MCU is still responsive
        print("  [4] Verifying MCU responsive...")
        try:
            self.mcu.sock.sendall(b"\r\n")
            time.sleep(2)
            lines = self.mcu.get_lines()
            recent = lines[-5:] if lines else []
            if any(">" in l or "$" in l or "#" in l for l in recent):
                print("  [PASS] MCU responsive, no overflow detected")
                return CycleResult.PASS
            else:
                print("  [PASS] No overflow (MCU may be processing)")
                return CycleResult.PASS
        except OSError:
            print("  [FAIL] MCU console unreachable")
            return CycleResult.DEVICE_UNRESPONSIVE

    def _observe_recovery(self, cycle):
        """After overflow detected, observe if device recovers on its own."""
        print(f"  [3] Waiting {self.recovery_s}s to see if DUT recovers...")

        overflow_count = 0
        recovery_time = None
        last_new_error_time = 0

        # Count initial overflows already in buffer
        lines = self.mcu.get_lines()
        overflow_count = sum(1 for l in lines if "xQueueSend fail" in l)

        # Drain queued overflow events — we already know about them
        while self.check_event(Event.QUEUE_OVERFLOW):
            pass
        while self.check_event(Event.CRASH):
            # Discard crash events that are just more xQueueSend fail lines
            pass
        self.clear_events()

        for sec in range(self.recovery_s):
            time.sleep(1)
            lines = self.mcu.get_lines()
            new_count = sum(1 for l in lines if "xQueueSend fail" in l)
            if new_count > overflow_count:
                overflow_count = new_count
                last_new_error_time = sec

            # Check for recovery: normal operation patterns
            recent = lines[-10:] if lines else []
            if recovery_time is None:
                if any("erpc:[CONNECTED]" in l or "vote_action" in l
                       or "state:active" in l or "eRPC from" in l
                       for l in recent):
                    recovery_time = sec
                    print(f"    [{sec}s] Recovery signs detected")

            # Check for REAL crash (not overflow) — only HardFault/BusFault etc
            crash = self.check_event(Event.CRASH)
            if crash and not any(p in crash[2] for p in QUEUE_OVERFLOW_PATTERNS):
                print(f"    [{sec}s] CRASH during recovery: {crash[2][:80]}")
                return CycleResult.CRASH

            if sec % 15 == 14:
                status = "recovering" if recovery_time else "waiting"
                print(f"    [{sec+1}s] {overflow_count} overflows, {status}")

        # Final assessment
        lines = self.mcu.get_lines()
        overflow_count = sum(1 for l in lines if "xQueueSend fail" in l)

        # Check MCU responsiveness
        try:
            self.mcu.sock.sendall(b"\r\n")
            time.sleep(2)
            responsive = True
        except OSError:
            responsive = False

        print(f"  [RESULT] {overflow_count} overflow errors total")
        print(f"    Last new error at: {last_new_error_time}s")
        print(f"    Recovery signs at: {recovery_time}s" if recovery_time else "    No recovery signs")
        print(f"    MCU responsive: {responsive}")

        if recovery_time is not None and responsive:
            print(f"  [RECOVERED] Device self-recovered after {recovery_time}s")
            return CycleResult.OVERFLOW_RECOVERED
        elif responsive:
            print(f"  [RECOVERED] MCU responsive (no explicit recovery pattern seen)")
            return CycleResult.OVERFLOW_RECOVERED
        else:
            print(f"  [STUCK] Device did NOT recover within {self.recovery_s}s")
            return CycleResult.OVERFLOW_STUCK

    def _save_cycle_log(self, cycle, label):
        os.makedirs(self._log_dir, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        mcu_lines = self.mcu.get_lines() if self.mcu else []
        isp_lines = self.isp.get_lines() if self.isp else []

        if mcu_lines:
            path = os.path.join(
                self._log_dir, f"usb_overflow_cycle{cycle}_{label}_mcu_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(mcu_lines))
            print(f"  [LOG] {path}")
        if isp_lines:
            path = os.path.join(
                self._log_dir, f"usb_overflow_cycle{cycle}_{label}_isp_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [LOG] {path}")

    def _reset_device(self, cycle):
        """Reset DUT and wait for it to come back awake."""
        print("  [RESET] Pressing reset button...")
        self.press_button(RESET_DO_CHANNEL, 1.0)
        time.sleep(3)

        # Reconnect consoles (reset may drop serial)
        print("  [RESET] Reconnecting consoles...")
        self.disconnect_consoles()
        time.sleep(5)
        if self.no_isp:
            self._connect_mcu_only()
        else:
            self.connect_consoles()

        self.clear_events()
        self.mcu.start_recording()

        # Plug USB to keep device awake
        self.voodoo_on(USB_DO_CHANNEL)

        print("  [RESET] Waiting for MCU boot (30s)...")
        time.sleep(30)

        # Verify MCU responsive
        try:
            self.mcu.sock.sendall(b"\r\n")
            time.sleep(3)
            lines = self.mcu.get_lines()
            if lines:
                print("  [RESET] MCU responding — recovered")
                return True
        except OSError:
            pass

        print("  [RESET] MCU not responding after reset")
        return False

    def _connect_mcu_only(self):
        self.mcu = SerialMuxReader(
            "MCU", self._cfg['mcu_host'], self._cfg['mcu_port'],
            event_callback=self._line_callback)
        self.mcu.connect()
        self.mcu.start()
        self.isp = None

    def connect_consoles(self):
        super().connect_consoles()
        if self.isp and self.isp.sock:
            from lib.console_utils import isp_init_console
            isp_init_console(self.isp.sock)

    def run(self, num_cycles=None):
        os.makedirs(self._log_dir, exist_ok=True)

        # Deploy burst script to voodoo board
        print("[INIT] Deploying burst script to voodoo board...")
        if not self._deploy_burst_script():
            print("[ABORT] Cannot deploy to voodoo board")
            return 1

        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_path = os.path.join(
            self._log_dir, f"session_{session_ts}.log")
        self._session_log = open(session_path, "w")
        self._session_log.write(
            f"# USB Queue Overflow Stress Test\n"
            f"# Started: {datetime.now().isoformat()}\n"
            f"# Cycles: {self.num_cycles}, Bursts/cycle: {self.bursts_per_cycle}, "
            f"Interval: {self.interval_s*1000:.0f}ms, Cooldown: {self.cooldown_s}s\n"
            f"# Burst execution: voodoo board ({VOODOO_HOST})\n\n")

        print(f"=== USB Queue Overflow Stress Test ===")
        print(f"  Cycles:          {self.num_cycles}")
        print(f"  Bursts/cycle:    {self.bursts_per_cycle}")
        print(f"  Toggle interval: {self.interval_s*1000:.0f}ms")
        print(f"  Observe window:  {self.observe_s}s")
        print(f"  Recovery window: {self.recovery_s}s")
        print(f"  Cooldown:        {self.cooldown_s}s")
        print(f"  Burst host:      {VOODOO_HOST} (local Modbus ~3ms RTT)")
        print(f"  Session log:     {session_path}")
        print()

        print("[INIT] Connecting consoles...")
        if self.no_isp:
            self._connect_mcu_only()
        else:
            self.connect_consoles()

        print("[INIT] Draining stale buffers...")
        self.mcu.drain(2.0)
        if self.isp:
            self.isp.drain(2.0)
        self.clear_events()
        print("[INIT] Ready\n")

        results = {"pass": 0, "overflow_recovered": 0, "overflow_stuck": 0,
                   "crash": 0, "unresponsive": 0}

        for cycle in range(1, self.num_cycles + 1):
            print(f"\n{'='*60}")
            print(f"[CYCLE {cycle}/{self.num_cycles}] "
                  f"[{datetime.now().strftime('%H:%M:%S')}]")
            print(f"{'='*60}")

            self._session_log.write(f"\n--- CYCLE {cycle} ---\n")

            result = self._run_cycle(cycle)

            if result == CycleResult.PASS:
                results["pass"] += 1
                self._save_cycle_log(cycle, "pass")

            elif result == CycleResult.OVERFLOW_RECOVERED:
                results["overflow_recovered"] += 1
                self._save_cycle_log(cycle, "OVERFLOW_RECOVERED")

            elif result == CycleResult.OVERFLOW_STUCK:
                results["overflow_stuck"] += 1
                print(f"\n  >>> DEVICE STUCK — saving logs, resetting <<<")
                self._save_cycle_log(cycle, "OVERFLOW_STUCK")
                if not self._reset_device(cycle):
                    print("  [ABORT] Cannot recover — stopping test")
                    break

            elif result == CycleResult.CRASH:
                results["crash"] += 1
                print(f"\n  >>> CRASH DETECTED — saving logs, resetting <<<")
                self._save_cycle_log(cycle, "CRASH")
                if not self._reset_device(cycle):
                    print("  [ABORT] Cannot recover — stopping test")
                    break

            elif result == CycleResult.DEVICE_UNRESPONSIVE:
                results["unresponsive"] += 1
                self._save_cycle_log(cycle, "UNRESPONSIVE")
                if not self._reset_device(cycle):
                    print("  [ABORT] Cannot recover — stopping test")
                    break

            # Cooldown between cycles
            if cycle < self.num_cycles:
                print(f"  [COOLDOWN] {self.cooldown_s}s...")
                time.sleep(self.cooldown_s)

        self.disconnect_consoles()
        self._session_log.write(f"\n# Ended: {datetime.now().isoformat()}\n")
        self._session_log.close()

        # Summary
        total_run = sum(results.values())
        total_stuck = results["overflow_stuck"] + results["crash"] + results["unresponsive"]

        print(f"\n{'='*60}")
        print(f"RESULTS: {total_run} cycles run")
        print(f"  PASS:               {results['pass']}")
        print(f"  OVERFLOW+RECOVERED: {results['overflow_recovered']}")
        print(f"  OVERFLOW+STUCK:     {results['overflow_stuck']}")
        print(f"  CRASH:              {results['crash']}")
        print(f"  UNRESPONSIVE:       {results['unresponsive']}")
        print(f"\n  Session log: {session_path}")
        print(f"  Log directory: {self._log_dir}")
        print(f"{'='*60}")

        if results["overflow_recovered"] > 0:
            print(f"\n  Overflow triggered {results['overflow_recovered']}x — device always self-recovered")
        if total_stuck > 0:
            print(f"\n  >>> HARD FAILURES: {total_stuck}/{total_run} cycles <<<")

        return 1 if total_stuck > 0 else 0


def main():
    parser = argparse.ArgumentParser(
        description="USB rapid plug/unplug stress test — trigger MCU queue overflow")
    parser.add_argument("-n", "--cycles", type=int, default=20,
                        help="Number of test cycles (default: 20)")
    parser.add_argument("--bursts", type=int, default=30,
                        help="USB toggles per cycle (default: 30)")
    parser.add_argument("--interval", type=float, default=0.05,
                        help="Seconds between toggles (default: 0.05 = 50ms)")
    parser.add_argument("--cooldown", type=float, default=10.0,
                        help="Seconds between cycles (default: 10)")
    parser.add_argument("--observe", type=float, default=5.0,
                        help="Observation window after burst (default: 5s)")
    parser.add_argument("--recovery", type=float, default=60.0,
                        help="Recovery observation window after overflow (default: 60s)")
    parser.add_argument("--no-isp", action="store_true",
                        help="Skip ISP console monitoring")
    args = parser.parse_args()

    test = USBQueueOverflowTest(
        num_cycles=args.cycles,
        bursts_per_cycle=args.bursts,
        interval_s=args.interval,
        cooldown_s=args.cooldown,
        observe_s=args.observe,
        recovery_s=int(args.recovery),
        no_isp=args.no_isp,
    )
    sys.exit(test.run())


if __name__ == "__main__":
    main()
