#!/usr/bin/env python3
"""PEGA-1555: Rapid USB plug/unplug to flood MCU main task queue.

Goal: Reproduce xQueueSend fail flood by rapid USB cycling while the main task
is busy processing ISP power-on/off sequences. Each plug/unplug posts 2+ events
to the 16-slot main task queue. If cycling is faster than the main task can drain,
the queue fills and the vote timer (1/sec) produces the infinite error flood.

Usage:
    python3 usb_queue_flood_test.py -n 20 --interval 0.5
    python3 usb_queue_flood_test.py -n 50 --interval 0.3 --burst 5

Prerequisites:
    - Device claimed, battery mode
    - serial_mux running (MCU on port 9002)
    - testbot4 reachable (DO6 = USB plug)
"""

import argparse
import socket
import time
import threading
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.mcu_patterns import (
    SLEEP_INDICATOR, ISP_OFF_PATTERNS, ISP_WAKE_PATTERNS, SBU_PATTERNS,
    SLEEP_VOTE_PATTERNS, check_for_anomalies, check_mcu_line,
    AnomalyType, is_crash_dump_line, save_crash_dump,
)
from lib.console_utils import get_serial_mux_config
from testbot4.testbot4_do_pulse import Testbot4

sys.stdout.reconfigure(line_buffering=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_cfg = get_serial_mux_config()
MCU_HOST = _cfg['mcu_host']
MCU_PORT = _cfg['mcu_port']

QUEUE_FLOOD_PATTERN = "xQueueSend fail"


class MCUReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = None
        self.running = False
        self.lines = []
        self.lock = threading.Lock()
        self.recording = False
        self.sleep_event = threading.Event()
        self.flood_event = threading.Event()
        self.flood_count = 0

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
        self.flood_event.clear()
        self.flood_count = 0

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
                    if QUEUE_FLOOD_PATTERN in line:
                        self.flood_count += 1
                        if self.flood_count >= 3:
                            self.flood_event.set()
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


_vb = None

def _get_testbot4():
    global _vb
    if _vb is None:
        _vb = Testbot4()
        _vb.connect()
    return _vb

def testbot4_cmd(args):
    try:
        vb = _get_testbot4()
        if args[0] == "--on":
            vb.on(int(args[1]))
        elif args[0] == "--off":
            vb.off(int(args[1]))
        elif args[0] == "--read":
            vb.read()
        else:
            do_num = int(args[0])
            duration = float(args[1]) if len(args) > 1 else 2.0
            vb.pulse(do_num, duration)
        return True
    except (OSError, RuntimeError) as e:
        global _vb
        print(f"  [ERR] testbot4: {e}")
        _vb = None
        return False


def wait_for_sleep(reader, timeout=120):
    """Wait for device to enter deep sleep."""
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

    print(f"  [*] MCU still awake, waiting up to {timeout}s...")
    reader.sleep_event.clear()
    got_sleep = reader.sleep_event.wait(timeout=timeout)
    if got_sleep:
        print("  [*] Got 'Network Stack Suspended'")
        time.sleep(10)
        return True

    return False


def reset_device(reader):
    """Hardware-reset via testbot4 button press."""
    print("  [RESET] Pressing reset button...")
    testbot4_cmd(["2", "1"])
    print("  [RESET] Waiting 60s for device to boot...")
    reader.start_recording()
    time.sleep(60)
    reader.drain(2.0)


def run_flood_test(reader, num_cycles, interval_s, burst_count, observe_time):
    """Rapid USB plug/unplug to flood the main task queue.

    Strategy:
        1. Wait for device to be in deep sleep (queue consumer idle)
        2. Fire a rapid burst of USB plug/unplug toggles
        3. Each toggle posts 2+ events to the 16-slot queue
        4. If the main task can't drain fast enough, queue fills
        5. Vote timer (1/sec) then produces infinite xQueueSend fail flood
    """
    print(f"=== PEGA-1555: USB Queue Flood Test ===")
    print(f"Cycles: {num_cycles}")
    print(f"Burst: {burst_count} toggles per cycle, {interval_s:.2f}s apart")
    print(f"Observe: {observe_time}s after burst")
    print()

    results = {"pass": 0, "flood_reproduced": 0, "fail_no_sleep": 0, "fail_crash": 0}
    crash_dumps = []
    dump_dir = os.path.join(SCRIPT_DIR, "crash_dumps")

    for cycle in range(1, num_cycles + 1):
        print(f"\n--- Cycle {cycle}/{num_cycles} [{datetime.now().strftime('%H:%M:%S')}] ---")

        # Step 1: Ensure USB unplugged, wait for deep sleep
        print("  [1] Unplug USB, waiting for deep sleep...")
        testbot4_cmd(["--off", "6"])
        reader.start_recording()

        if not wait_for_sleep(reader):
            print("  [!] Device didn't enter sleep — skipping cycle")
            results["fail_no_sleep"] += 1
            testbot4_cmd(["--on", "6"])
            time.sleep(5)
            continue

        # Step 2: Rapid USB burst
        reader.start_recording()
        print(f"  [2] Firing {burst_count} USB toggles ({interval_s}s interval)...")

        for i in range(burst_count):
            testbot4_cmd(["--on", "6"])
            time.sleep(interval_s)
            testbot4_cmd(["--off", "6"])
            time.sleep(interval_s)

            # Check if we already triggered the flood mid-burst
            if reader.flood_event.is_set():
                print(f"  [!] Queue flood detected at toggle {i+1}!")
                break

        # Step 3: Observe for queue flood
        print(f"  [3] Observing for {observe_time}s...")
        flood_detected = False
        observe_end = time.time() + observe_time

        while time.time() < observe_end:
            if reader.flood_event.is_set():
                flood_detected = True
                break
            time.sleep(0.5)

        reader.stop_recording()
        lines = reader.get_lines()

        # Count flood messages
        flood_lines = [l for l in lines if QUEUE_FLOOD_PATTERN in l]

        if flood_detected or len(flood_lines) >= 3:
            print(f"  [BUG REPRODUCED] xQueueSend fail flood! ({len(flood_lines)} messages)")
            results["flood_reproduced"] += 1

            # Save the log
            os.makedirs(dump_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(dump_dir, f"queue_flood_cycle{cycle}_{ts}.log")
            with open(log_path, "w") as f:
                f.write(f"# PEGA-1555 queue flood — cycle {cycle}\n")
                f.write(f"# Time: {datetime.now().isoformat()}\n")
                f.write(f"# Burst: {burst_count} toggles, {interval_s}s interval\n")
                f.write(f"# Flood messages: {len(flood_lines)}\n\n")
                for l in lines:
                    f.write(l + "\n")
            print(f"  [DUMP] Log saved: {os.path.basename(log_path)}")
            crash_dumps.append(log_path)

            # Device is hung — reset it
            reset_device(reader)
            continue

        # Check for other anomalies (crash, hang)
        has_crash = any(is_crash_dump_line(l) for l in lines)
        if has_crash:
            print("  [!] Crash detected (may be related)")
            results["fail_crash"] += 1
            path = save_crash_dump(lines, dump_dir, "queue_flood", cycle, source="mcu")
            if path:
                crash_dumps.append(path)
                print(f"  [DUMP] Crash saved: {os.path.basename(path)}")
            reset_device(reader)
            continue

        # No flood — device survived
        print(f"  [PASS] No queue flood (saw {len(flood_lines)} xQueueSend fail)")
        results["pass"] += 1

        # Leave USB unplugged for next cycle's sleep
        testbot4_cmd(["--off", "6"])
        time.sleep(5)

    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS: {num_cycles} cycles")
    print(f"  PASS (no flood):        {results['pass']}")
    print(f"  BUG REPRODUCED (flood): {results['flood_reproduced']}")
    print(f"  FAIL (no sleep):        {results['fail_no_sleep']}")
    print(f"  FAIL (crash):           {results['fail_crash']}")
    if results['flood_reproduced'] > 0:
        print(f"\n  >>> PEGA-1555 REPRODUCED in {results['flood_reproduced']}/{num_cycles} cycles <<<")
    if crash_dumps:
        print(f"\n  Logs/dumps saved ({len(crash_dumps)}):")
        for p in crash_dumps:
            print(f"    {p}")
    print()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="PEGA-1555: Rapid USB cycling to flood MCU main task queue")
    parser.add_argument("-n", "--cycles", type=int, default=20,
                        help="Number of test cycles (default: 20)")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Seconds between each plug/unplug toggle (default: 0.5)")
    parser.add_argument("--burst", type=int, default=10,
                        help="Number of USB plug/unplug toggles per cycle (default: 10)")
    parser.add_argument("--observe", type=int, default=30,
                        help="Seconds to observe after burst (default: 30)")
    args = parser.parse_args()

    print(f"[*] Connecting to MCU console at {MCU_HOST}:{MCU_PORT}...")
    reader = MCUReader()
    try:
        reader.connect()
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        print(f"[FATAL] Cannot connect to MCU console: {e}")
        sys.exit(1)
    reader.start()
    reader.drain(2.0)
    print("[*] Connected.")

    try:
        run_flood_test(reader, args.cycles, args.interval, args.burst, args.observe)
    except KeyboardInterrupt:
        print("\n[*] Interrupted by user")
    finally:
        reader.disconnect()


if __name__ == "__main__":
    main()
