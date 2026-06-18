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
import socket
import time
import threading
import subprocess
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcu_patterns import (
    SLEEP_INDICATOR, ISP_OFF_PATTERNS, ISP_WAKE_PATTERNS, SBU_PATTERNS,
    SLEEP_VOTE_PATTERNS, check_for_anomalies, check_mcu_line, check_isp_line,
    AnomalyType, ISP_CRASH_PATTERNS, is_crash_dump_line, save_crash_dump,
)

# ISP boot messages that are EXPECTED in this test (ISP waking = success)
ISP_BOOT_EXPECTED = [
    "Initramfs unpacking",
    "IPL ",
    "DRAM Size:",
    "HW Reset",
]
from console_utils import isp_init_console, get_serial_mux_config

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VOODOO_SCRIPT = os.path.join(SCRIPT_DIR, "voodoo_do_pulse.py")
_cfg = get_serial_mux_config()
MCU_HOST = _cfg['mcu_host']
MCU_PORT = _cfg['mcu_port']
ISP_HOST = _cfg['isp_host']
ISP_PORT = _cfg['isp_port']


class MCUReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = None
        self.running = False
        self.lines = []
        self.lock = threading.Lock()
        self.recording = False
        self.sleep_event = threading.Event()
        self.isp_off_event = threading.Event()
        self.anomaly_event = threading.Event()
        self.anomaly_info = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((MCU_HOST, MCU_PORT))
        self.sock.settimeout(0.5)
        self.running = True

    def disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def start_recording(self):
        with self.lock:
            self.lines = []
            self.recording = True
        self.sleep_event.clear()
        self.isp_off_event.clear()
        self.anomaly_event.clear()
        self.anomaly_info = None

    def stop_recording(self):
        with self.lock:
            self.recording = False

    def get_lines(self):
        with self.lock:
            return list(self.lines)

    def drain(self, duration=3.0):
        end = time.time() + duration
        while time.time() < end and self.running:
            try:
                self.sock.recv(8192)
            except (socket.timeout, BlockingIOError, OSError):
                pass
            time.sleep(0.05)

    def run(self):
        buf = ""
        while self.running:
            try:
                data = self.sock.recv(8192)
                if not data:
                    time.sleep(0.1)
                    continue
                buf += data.decode("utf-8", errors="replace").replace("\x00", "")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    with self.lock:
                        if self.recording:
                            self.lines.append(line)
                    if SLEEP_INDICATOR in line:
                        self.sleep_event.set()
                    if any(p in line for p in ISP_OFF_PATTERNS):
                        self.isp_off_event.set()
                    if is_crash_dump_line(line):
                        self.anomaly_info = (AnomalyType.CRASH, line)
                        self.anomaly_event.set()
                    else:
                        anomaly_type, _ = check_mcu_line(line)
                        if anomaly_type != AnomalyType.NONE:
                            self.anomaly_info = (anomaly_type, line)
                            self.anomaly_event.set()
                if len(buf) > 4096:
                    line = buf.strip()
                    buf = ""
                    if line:
                        with self.lock:
                            if self.recording:
                                self.lines.append(line)
            except socket.timeout:
                continue
            except (BlockingIOError, ConnectionResetError, BrokenPipeError, OSError):
                if self.running:
                    time.sleep(0.5)


class ISPReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = None
        self.running = False
        self.lines = []
        self.lock = threading.Lock()
        self.recording = False
        self.anomaly_event = threading.Event()
        self.anomaly_info = None
        self.expect_boot = False

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((ISP_HOST, ISP_PORT))
        self.sock.settimeout(0.5)
        self.running = True

    def disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def start_recording(self):
        with self.lock:
            self.lines = []
            self.recording = True
        self.anomaly_event.clear()
        self.anomaly_info = None

    def stop_recording(self):
        with self.lock:
            self.recording = False

    def get_lines(self):
        with self.lock:
            return list(self.lines)

    def drain(self, duration=3.0):
        end = time.time() + duration
        while time.time() < end and self.running:
            try:
                self.sock.recv(8192)
            except (socket.timeout, BlockingIOError, OSError):
                pass
            time.sleep(0.05)

    def init_console(self):
        """Login to ISP console if needed and start tail -f /var/log/messages."""
        isp_init_console(self.sock)

    def run(self):
        buf = ""
        while self.running:
            try:
                data = self.sock.recv(8192)
                if not data:
                    time.sleep(0.1)
                    continue
                buf += data.decode("utf-8", errors="replace").replace("\x00", "")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    with self.lock:
                        if self.recording:
                            self.lines.append(line)
                    if not any(p in line for p in ISP_BOOT_EXPECTED):
                        anomaly_type, _ = check_isp_line(line)
                        if anomaly_type != AnomalyType.NONE:
                            self.anomaly_info = (anomaly_type, line)
                            self.anomaly_event.set()
                if len(buf) > 4096:
                    line = buf.strip()
                    buf = ""
                    if line:
                        with self.lock:
                            if self.recording:
                                self.lines.append(line)
            except socket.timeout:
                continue
            except (BlockingIOError, ConnectionResetError, BrokenPipeError, OSError):
                if self.running:
                    time.sleep(0.5)


def voodoo(args):
    cmd = [sys.executable, VOODOO_SCRIPT] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print(f"  [ERR] voodoo: {result.stderr.strip()}")
    return result.returncode == 0


def check_result(lines):
    """Analyze MCU log lines after USB plug. Returns (pass, fail_type, detail_lines)."""
    # Check for anomalies first
    anomaly_type, anomaly_line = check_for_anomalies(lines)
    if anomaly_type != AnomalyType.NONE:
        return False, f"anomaly_{anomaly_type.name.lower()}", [anomaly_line] + lines[-5:]

    saw_sbu = any(any(p in l for p in SBU_PATTERNS) for l in lines)
    saw_isp_wake = any(any(p in l for p in ISP_WAKE_PATTERNS) for l in lines)
    # Sleep was rejected due to pending wakeup reasons — ISP stays on, this is a PASS
    saw_sleep_rejected = any("stay_awake_reasons pending - rejecting sleep" in l
                             or "stay_awake_reasons=0x" in l for l in lines)
    # MCU transitioned back from sleep to active — sleep was rejected
    saw_sleep_to_active = any("state:active" in l and "oldstate:sleep" in l for l in lines)
    # ISP still connected after USB plug means it never powered off — PASS
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


def check_anomaly_abort(mcu_reader, isp_reader=None):
    """Check if any reader detected an anomaly. Returns (should_abort, source, message) tuple."""
    if mcu_reader.anomaly_event.is_set():
        atype, line = mcu_reader.anomaly_info
        return True, "MCU", f"{atype.name}: {line}"
    if isp_reader and isp_reader.anomaly_event.is_set():
        atype, line = isp_reader.anomaly_info
        return True, "ISP", f"{atype.name}: {line}"
    return False, None, None


def handle_crash_dump(reader, isp_reader, cycle_num, test_name, output_dir):
    """Save crash dump, print message, reset device, and return.

    Call this when a crash/dump anomaly is detected. Collects remaining dump lines
    (waits briefly for dump to finish), saves to file, then resets.

    Returns:
        Path to saved dump file, or None.
    """
    time.sleep(3)
    lines = reader.get_lines()
    if isp_reader:
        lines += isp_reader.get_lines()

    dump_path = save_crash_dump(lines, output_dir, test_name, cycle_num, source="mcu")
    if dump_path:
        print(f"  [DUMP] Crash dump saved: {os.path.basename(dump_path)}")
    else:
        print(f"  [DUMP] Crash context saved (no hex dump lines)")
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = os.path.join(output_dir, f"crash_{test_name}_cycle{cycle_num}_{ts}.log")
        with open(fallback, "w") as f:
            f.write(f"# Crash context — {test_name} cycle {cycle_num}\n")
            f.write(f"# Time: {datetime.now().isoformat()}\n\n")
            for l in lines[-30:]:
                f.write(l + "\n")
        dump_path = fallback
        print(f"  [DUMP] Context saved: {os.path.basename(fallback)}")

    reset_device(reader, isp_reader)
    return dump_path


def wait_for_epds(reader, timeout=120):
    """Wait for EPDS — either catch message or probe console."""
    reader.start_recording()
    reader.sleep_event.clear()
    got_sleep = reader.sleep_event.wait(timeout=30)

    if got_sleep:
        print("  [*] Got 'Network Stack Suspended'")
        time.sleep(10)
        return True

    # May already be in EPDS — probe console
    print("  [*] No sleep message — probing MCU console...")
    reader.start_recording()
    try:
        reader.sock.sendall(b"\r\n\r\n\r\n")
    except OSError:
        pass
    time.sleep(3)
    probe_lines = reader.get_lines()
    mcu_responded = any(">" in l or "$" in l or "#" in l or "mcu:" in l.lower()
                        for l in probe_lines[-5:])
    if not mcu_responded:
        print("  [*] MCU unresponsive — already in EPDS")
        return True

    # MCU still awake, wait longer
    print(f"  [*] MCU still awake, waiting up to {timeout}s...")
    reader.sleep_event.clear()
    got_sleep = reader.sleep_event.wait(timeout=timeout)
    if got_sleep:
        print("  [*] Got 'Network Stack Suspended'")
        time.sleep(10)
        return True

    return False


# ---------------------------------------------------------------------------
# Scenario A: Plug USB while device is in full EPDS
# ---------------------------------------------------------------------------

def run_scenario_a(reader, isp_reader, num_cycles, output_dir=None):
    print(f"=== PEGA-1455 Scenario A: USB Wake from EPDS ===")
    print(f"Cycles: {num_cycles}")
    print(f"Race: WiFi resume sets wakeup_reason before VBUS ISR")
    print()

    results = {"pass": 0, "fail_no_isp_wake": 0, "fail_timeout": 0, "fail_crash": 0}
    crash_dumps = []

    for cycle in range(1, num_cycles + 1):
        print(f"\n--- Cycle {cycle}/{num_cycles} [{datetime.now().strftime('%H:%M:%S')}] ---")

        # Ensure USB unplugged
        print("  [1] Unplug USB (DO6 OFF)")
        voodoo(["--off", "6"])

        # Wait for EPDS
        print("  [2] Waiting for EPDS...")
        if not wait_for_epds(reader):
            print("  [!] TIMEOUT — device didn't sleep")
            results["fail_timeout"] += 1
            voodoo(["--on", "6"])
            time.sleep(5)
            continue

        # Plug USB
        reader.start_recording()
        if isp_reader:
            isp_reader.start_recording()
        print("  [3] Plug USB (DO6 ON)")
        voodoo(["--on", "6"])

        # Observe
        print("  [4] Observing logs for 15s...")
        time.sleep(15)
        reader.stop_recording()
        if isp_reader:
            isp_reader.stop_recording()

        # Check for anomaly (crash/hang) on either console
        abort, source, msg = check_anomaly_abort(reader, isp_reader)
        if abort:
            print(f"  [!] {source} anomaly (unrelated crash): {msg}")
            results["fail_crash"] += 1
            dump_dir = output_dir or os.path.join(SCRIPT_DIR, "crash_dumps")
            path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_a", dump_dir)
            if path:
                crash_dumps.append(path)
            continue

        passed, fail_type, detail = check_result(reader.get_lines())
        if passed:
            print("  [PASS] ISP wake detected")
            results["pass"] += 1
        else:
            if fail_type.startswith("anomaly_"):
                print(f"  [!] {fail_type} (unrelated crash)")
                results["fail_crash"] += 1
                dump_dir = output_dir or os.path.join(SCRIPT_DIR, "crash_dumps")
                path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_a", dump_dir)
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


# ---------------------------------------------------------------------------
# Scenario B: Plug USB during sleep-transition window
# ---------------------------------------------------------------------------

def reset_device(reader, isp_reader):
    """Hardware-reset the DUT and wait 60s for it to come back."""
    print("  [RESET] Pressing reset button...")
    voodoo(["2", "1"])
    print("  [RESET] Waiting 60s for device to boot...")
    reader.start_recording()
    if isp_reader:
        isp_reader.start_recording()
    time.sleep(60)
    reader.drain(2.0)


def run_scenario_b(reader, isp_reader, num_cycles, output_dir=None):
    print(f"=== PEGA-1455 Scenario B: USB Plug/Unplug/Replug/Sleep ===")
    print(f"Cycles: {num_cycles}")
    print(f"Sequence: plug→unplug→replug(verify wake)→unplug(verify sleep)")
    print()

    results = {"pass": 0, "fail_no_wake": 0, "fail_no_sleep": 0, "fail_crash": 0}
    crash_dumps = []
    dump_dir = output_dir or os.path.join(SCRIPT_DIR, "crash_dumps")

    for cycle in range(1, num_cycles + 1):
        print(f"\n--- Cycle {cycle}/{num_cycles} [{datetime.now().strftime('%H:%M:%S')}] ---")

        # Step 1: Start from USB plugged, ISP/MCU awake
        print("  [1] Ensure USB plugged — device awake")
        voodoo(["--on", "6"])
        reader.start_recording()
        if isp_reader:
            isp_reader.start_recording()
        time.sleep(5)

        # Poke MCU to verify it's responsive
        try:
            reader.sock.sendall(b"\r\n")
        except OSError:
            pass
        time.sleep(2)

        abort, source, msg = check_anomaly_abort(reader, isp_reader)
        if abort:
            print(f"  [!] {source} anomaly at start (unrelated crash): {msg}")
            results["fail_crash"] += 1
            path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
            if path:
                crash_dumps.append(path)
            continue

        # Step 2: Unplug USB — observe sleep flow starting
        reader.start_recording()
        if isp_reader:
            isp_reader.start_recording()
        reader.sleep_event.clear()
        reader.isp_off_event.clear()
        print("  [2] Unplug USB — observing sleep flow...")
        voodoo(["--off", "6"])

        # Wait for sleep vote or ISP-off indicator (up to 30s)
        sleep_starting = False
        deadline = time.time() + 30
        while time.time() < deadline:
            if reader.isp_off_event.is_set():
                print("  [2] ISP off detected — sleep flow active")
                sleep_starting = True
                break
            lines = reader.get_lines()
            if any(any(p in l for p in SLEEP_VOTE_PATTERNS) for l in lines[-3:]):
                print("  [2] Sleep vote detected — sleep flow active")
                sleep_starting = True
                break
            if reader.sleep_event.is_set():
                print("  [2] Already in EPDS")
                sleep_starting = True
                break
            abort, source, msg = check_anomaly_abort(reader, isp_reader)
            if abort:
                break
            time.sleep(0.2)

        if abort:
            print(f"  [!] {source} anomaly during unplug (unrelated crash): {msg}")
            results["fail_crash"] += 1
            path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
            if path:
                crash_dumps.append(path)
            continue

        if not sleep_starting:
            # Check if the reason is a crash dump in the buffer
            lines = reader.get_lines()
            if any(is_crash_dump_line(l) for l in lines):
                print("  [!] Crash dump detected (unrelated) — resetting")
                results["fail_crash"] += 1
                path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
                if path:
                    crash_dumps.append(path)
                continue
            print("  [!] No sleep flow detected within 30s — resetting")
            results["fail_no_sleep"] += 1
            reset_device(reader, isp_reader)
            continue

        # Step 3: Quickly plug back — verify ISP remains ON or wakes
        reader.start_recording()
        if isp_reader:
            isp_reader.start_recording()
        print("  [3] Replug USB — verifying ISP stays on or wakes...")
        voodoo(["--on", "6"])

        obs_deadline = time.time() + 60
        while time.time() < obs_deadline:
            time.sleep(5)
            try:
                reader.sock.sendall(b"\r\n")
            except OSError:
                pass
            lines = reader.get_lines()
            if any(any(p in l for p in ISP_WAKE_PATTERNS) for l in lines):
                break
            abort, source, msg = check_anomaly_abort(reader, isp_reader)
            if abort:
                break

        if abort:
            print(f"  [!] {source} anomaly after replug (unrelated crash): {msg}")
            results["fail_crash"] += 1
            path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
            if path:
                crash_dumps.append(path)
            continue

        passed, fail_type, detail = check_result(reader.get_lines())
        if not passed:
            print(f"  [FAIL] ISP did not wake/stay on after replug ({fail_type})")
            results["fail_no_wake"] += 1
            for line in (detail or [])[:10]:
                print(f"        {line}")
            reset_device(reader, isp_reader)
            continue

        print("  [3] PASS — ISP on after replug")

        # Step 4: Wait 15 seconds (let system stabilize)
        print("  [4] Waiting 15s...")
        time.sleep(15)

        abort, source, msg = check_anomaly_abort(reader, isp_reader)
        if abort:
            print(f"  [!] {source} anomaly during stabilize (unrelated crash): {msg}")
            results["fail_crash"] += 1
            path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
            if path:
                crash_dumps.append(path)
            continue

        # Step 5: Unplug again — verify full go-to-sleep within 60s
        reader.start_recording()
        if isp_reader:
            isp_reader.start_recording()
        reader.sleep_event.clear()
        print("  [5] Unplug USB again — expecting full sleep within 60s...")
        voodoo(["--off", "6"])

        slept = False
        sleep_deadline = time.time() + 60
        while time.time() < sleep_deadline:
            if reader.sleep_event.is_set():
                slept = True
                break
            abort, source, msg = check_anomaly_abort(reader, isp_reader)
            if abort:
                break
            time.sleep(1)

        if abort:
            print(f"  [!] {source} anomaly during final sleep (unrelated crash): {msg}")
            results["fail_crash"] += 1
            path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
            if path:
                crash_dumps.append(path)
            continue

        if slept:
            print("  [5] Device entered deep sleep — PASS")
            results["pass"] += 1
        else:
            # Check if failure is due to crash dump in buffer
            lines = reader.get_lines()
            if any(is_crash_dump_line(l) for l in lines):
                print("  [!] Crash dump in output (unrelated) — resetting")
                results["fail_crash"] += 1
                path = handle_crash_dump(reader, isp_reader, cycle, "usb_wake_b", dump_dir)
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    reader = MCUReader()
    try:
        reader.connect()
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        print(f"[FATAL] Cannot connect to MCU console at {MCU_HOST}:{MCU_PORT}: {e}")
        sys.exit(1)
    reader.start()
    reader.drain(2.0)

    isp_reader = None
    if not args.no_isp:
        isp_reader = ISPReader()
        try:
            isp_reader.connect()
            isp_reader.start()
            isp_reader.init_console()
            isp_reader.drain(2.0)
            print(f"[*] ISP console connected ({ISP_HOST}:{ISP_PORT})")
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            print(f"[WARN] Cannot connect to ISP console at {ISP_HOST}:{ISP_PORT}: {e}")
            print("[WARN] Continuing without ISP monitoring")
            isp_reader = None

    if args.scenario == "a":
        results = run_scenario_a(reader, isp_reader, args.cycles, output_dir=args.output_dir)
    else:
        results = run_scenario_b(reader, isp_reader, args.cycles, output_dir=args.output_dir)

    reader.disconnect()
    if isp_reader:
        isp_reader.disconnect()

    print(f"\n{'='*50}")
    print(f"RESULTS: {args.cycles} cycles (Scenario {args.scenario.upper()})")
    print(f"  PASS:              {results.get('pass', 0)}")
    print(f"  FAIL (no wake):    {results.get('fail_no_isp_wake', 0) + results.get('fail_no_wake', 0)}")
    print(f"  FAIL (no sleep):   {results.get('fail_timeout', 0) + results.get('fail_no_sleep', 0)}")
    print(f"  FAIL (crash):      {results.get('fail_crash', 0)}")
    total_fail = sum(v for k, v in results.items() if k != "pass")
    if total_fail > 0:
        print(f"\n  >>> FAILURES: {total_fail}/{args.cycles} cycles <<<")
    print()


if __name__ == "__main__":
    main()
