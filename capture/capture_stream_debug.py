#!/usr/bin/env python3
"""
Stream Stop Debug — 3-channel capture (ISP + MCU + Android logcat).

Connects to ISP serial, MCU serial, and adb logcat simultaneously.
Reports READY, then passively records all 3 channels until the stream ends.
Analyzes logs post-capture to determine who/why the stream stopped.

Usage:
    python3 utils/custom/device_tests/capture_stream_debug.py [--timeout 900]
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import get_serial_mux_config, isp_init_console, SerialMuxReader

LOG_DIR = "/tmp/stream_stop_debug"

STREAM_START_PATTERNS = [
    "USER_STREAM_START",
    "LSM_EVT_USER_STREAM_START",
    "_start_user_stream()",
    "set_user_stream_active() => 1",
]

STREAM_END_PATTERNS = [
    "USER_STREAM_END",
    "LSM_EVT_USER_STREAM_END",
    "All clients disconnected",
]

INTERESTING_ISP_PATTERNS = [
    "stream", "STREAM", "disconnect", "timeout", "error", "ERROR",
    "sleep", "SLEEP", "vote", "lsm_", "LSM_EVT", "ir_cut",
    "night_vision", "day_night", "rtsp", "RTSP", "teardown",
    "mediaserver", "crash", "panic", "Oops", "segfault",
]

INTERESTING_MCU_PATTERNS = [
    "eMode", "sleep", "SLEEP", "wake", "WAKE", "vote", "erpc",
    "disconnect", "error", "crash", "assert", "xQueue", "IRCut",
]


class LogcatCapture(threading.Thread):
    """Captures adb logcat output to a list and file."""

    def __init__(self, log_file):
        super().__init__(daemon=True)
        self.log_file = log_file
        self.lines = []
        self.lock = threading.Lock()
        self.running = False
        self.proc = None

    def start_capture(self):
        self.running = True
        self.start()

    def stop_capture(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()

    def get_lines(self):
        with self.lock:
            return list(self.lines)

    def run(self):
        try:
            self.proc = subprocess.Popen(
                ["adb", "logcat", "-v", "threadtime"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print("  [ERROR] adb not found")
            return

        with open(self.log_file, "w") as f:
            while self.running:
                line = self.proc.stdout.readline()
                if not line:
                    if self.proc.poll() is not None:
                        break
                    continue
                line = line.rstrip("\n")
                f.write(line + "\n")
                f.flush()
                with self.lock:
                    self.lines.append(line)


class StreamDebugCapture:
    """3-channel capture: ISP + MCU + logcat."""

    def __init__(self, timeout=900):
        self.timeout = timeout
        self.cfg = get_serial_mux_config()
        self.isp = None
        self.mcu = None
        self.logcat = None
        self.isp_lines = []
        self.mcu_lines = []
        self.isp_lock = threading.Lock()
        self.mcu_lock = threading.Lock()
        self.stream_started = threading.Event()
        self.stream_ended = threading.Event()
        self.stream_start_time = None
        self.stream_end_time = None
        self.stream_end_reason = None
        self._end_timer = None
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _isp_callback(self, line, source):
        with self.isp_lock:
            self.isp_lines.append((time.time(), line))
        for pat in STREAM_START_PATTERNS:
            if pat in line:
                self.stream_start_time = time.time()
                self.stream_started.set()
                self._cancel_end_timer()
                break
        for pat in STREAM_END_PATTERNS:
            if pat in line:
                self.stream_end_time = time.time()
                self.stream_end_reason = line
                self._schedule_end_confirm(line)
                break

    def _schedule_end_confirm(self, reason):
        """Delay stream-end confirmation by 10s to filter backend renegotiation."""
        self._cancel_end_timer()
        self._end_timer = threading.Timer(10.0, self._confirm_end, args=[reason])
        self._end_timer.daemon = True
        self._end_timer.start()

    def _confirm_end(self, reason):
        """If no new stream start arrived within 10s, confirm the end."""
        self.stream_end_reason = reason
        self.stream_ended.set()

    def _cancel_end_timer(self):
        if hasattr(self, '_end_timer') and self._end_timer:
            self._end_timer.cancel()
            self._end_timer = None

    def _mcu_callback(self, line, source):
        with self.mcu_lock:
            self.mcu_lines.append((time.time(), line))

    def setup(self):
        os.makedirs(LOG_DIR, exist_ok=True)

        print("[1/3] Connecting ISP console (192.168.7.100:9001)...")
        self.isp = SerialMuxReader(
            "ISP", self.cfg['isp_host'], self.cfg['isp_port'],
            event_callback=self._isp_callback)
        self.isp.connect()
        self.isp.start()
        isp_init_console(self.isp.sock)
        time.sleep(2)
        print("  [OK] ISP connected, tail -f running")

        print("[2/3] Connecting MCU console (192.168.7.100:9002)...")
        self.mcu = SerialMuxReader(
            "MCU", self.cfg['mcu_host'], self.cfg['mcu_port'],
            event_callback=self._mcu_callback)
        self.mcu.connect()
        self.mcu.start()
        self.mcu.drain(2.0)
        print("  [OK] MCU connected, stale data drained")

        print("[3/3] Starting adb logcat...")
        subprocess.run(["adb", "logcat", "-c"], capture_output=True, timeout=10)
        logcat_file = os.path.join(LOG_DIR, f"logcat_{self.timestamp}.log")
        self.logcat = LogcatCapture(logcat_file)
        self.logcat.start_capture()
        time.sleep(1)
        if self.logcat.proc and self.logcat.proc.poll() is None:
            print(f"  [OK] logcat capturing to {logcat_file}")
        else:
            print("  [WARN] logcat may not be running")

    def wait_for_stream(self):
        print()
        print("=" * 60)
        print("READY — Start live stream from Arlo app now")
        print("=" * 60)
        print()

        # Check if stream is already active (started before we connected)
        time.sleep(3)
        with self.isp_lock:
            for _, line in self.isp_lines:
                if "set_user_stream_active() => 1" in line or \
                   "_start_user_stream()" in line:
                    print(f"  [OK] Stream already active (detected in log history)")
                    self.stream_start_time = time.time()
                    self.stream_started.set()
                    return True

        if self.stream_started.is_set():
            print(f"  [OK] Stream started at {datetime.now().strftime('%H:%M:%S')}")
            return True

        print("Waiting for stream start (USER_STREAM_START in ISP log)...")

        if self.stream_started.wait(timeout=120):
            print(f"  [OK] Stream started at {datetime.now().strftime('%H:%M:%S')}")
            return True
        else:
            print("  [TIMEOUT] No stream start detected in 120s")
            return False

    def capture_until_end(self):
        print("Recording all channels... (waiting for stream end or timeout)")
        print(f"  Max capture time: {self.timeout}s")
        print()

        start = time.time()
        last_status = start
        while not self.stream_ended.is_set():
            elapsed = time.time() - start
            if elapsed > self.timeout:
                print(f"\n  [TIMEOUT] Max capture time ({self.timeout}s) reached")
                self.stream_end_reason = "CAPTURE_TIMEOUT"
                break
            if time.time() - last_status > 30:
                with self.isp_lock:
                    isp_count = len(self.isp_lines)
                with self.mcu_lock:
                    mcu_count = len(self.mcu_lines)
                logcat_count = len(self.logcat.get_lines()) if self.logcat else 0
                dur = int(elapsed)
                print(f"  [{dur}s] ISP:{isp_count} MCU:{mcu_count} Logcat:{logcat_count} lines")
                last_status = time.time()
            time.sleep(0.5)

        if self.stream_ended.is_set():
            elapsed = time.time() - start
            print(f"\n  [DETECTED] Stream ended after {elapsed:.1f}s")
            print(f"  Reason line: {self.stream_end_reason}")

        print("  Capturing 10s of follow-up events...")
        time.sleep(10)

    def save_logs(self):
        isp_file = os.path.join(LOG_DIR, f"isp_{self.timestamp}.log")
        mcu_file = os.path.join(LOG_DIR, f"mcu_{self.timestamp}.log")

        with self.isp_lock:
            isp_copy = list(self.isp_lines)
        with self.mcu_lock:
            mcu_copy = list(self.mcu_lines)

        with open(isp_file, "w") as f:
            for ts, line in isp_copy:
                f.write(f"{ts:.3f} {line}\n")
        with open(mcu_file, "w") as f:
            for ts, line in mcu_copy:
                f.write(f"{ts:.3f} {line}\n")

        print(f"\nLogs saved:")
        print(f"  ISP: {isp_file} ({len(isp_copy)} lines)")
        print(f"  MCU: {mcu_file} ({len(mcu_copy)} lines)")
        logcat_lines = self.logcat.get_lines() if self.logcat else []
        logcat_file = os.path.join(LOG_DIR, f"logcat_{self.timestamp}.log")
        print(f"  Logcat: {logcat_file} ({len(logcat_lines)} lines)")

        return isp_copy, mcu_copy, logcat_lines

    def analyze(self, isp_lines, mcu_lines, logcat_lines):
        print("\n" + "=" * 60)
        print("ANALYSIS")
        print("=" * 60)

        # Stream duration
        if self.stream_start_time and self.stream_end_time:
            duration = self.stream_end_time - self.stream_start_time
            print(f"\nStream duration: {duration:.1f}s ({duration/60:.1f} min)")
            if duration < 330:
                print("  >>> ABNORMAL: ended before typical 5-min idle timeout")
            else:
                print("  Normal: likely idle timeout (~5 min)")
        elif self.stream_start_time:
            print("\nStream started but no end detected (timeout or manual stop)")

        # ISP events around stream end
        print("\n--- ISP: Last 20 interesting lines before stream end ---")
        interesting_isp = []
        for ts, line in isp_lines:
            if any(p.lower() in line.lower() for p in INTERESTING_ISP_PATTERNS):
                interesting_isp.append((ts, line))
        for ts, line in interesting_isp[-20:]:
            t = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [{t}] {line[:120]}")

        # MCU events around stream end
        print("\n--- MCU: Last 20 interesting lines before stream end ---")
        interesting_mcu = []
        for ts, line in mcu_lines:
            if any(p.lower() in line.lower() for p in INTERESTING_MCU_PATTERNS):
                interesting_mcu.append((ts, line))
        for ts, line in interesting_mcu[-20:]:
            t = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [{t}] {line[:120]}")

        # Logcat: Arlo-related events around stream end
        print("\n--- Logcat: Arlo/stream-related lines (last 30) ---")
        arlo_logcat = []
        for line in logcat_lines:
            ll = line.lower()
            if any(k in ll for k in [
                "arlo", "stream", "rtsp", "live", "disconnect",
                "timeout", "onpause", "onstop", "ondestroy",
                "media", "video", "codec", "socket", "error",
                "exception", "anr", "crash",
            ]):
                arlo_logcat.append(line)
        for line in arlo_logcat[-30:]:
            print(f"  {line[:140]}")

        # Determine initiator
        print("\n--- Verdict ---")
        if self.stream_end_reason and "All clients disconnected" in self.stream_end_reason:
            print("  Initiator: APP (mediaserver received disconnect from all clients)")
            print("  Check logcat for app lifecycle / network events before disconnect")
        elif self.stream_end_reason and "USER_STREAM_END" in self.stream_end_reason:
            print("  Initiator: DEVICE (arlod ended the stream internally)")
            print("  Check ISP log for error/sleep/vote before stream end")
        elif self.stream_end_reason == "CAPTURE_TIMEOUT":
            print("  Stream did not end within capture window")
        else:
            print(f"  Initiator: UNKNOWN — end reason: {self.stream_end_reason}")

        # Save summary
        summary_file = os.path.join(LOG_DIR, f"summary_{self.timestamp}.txt")
        with open(summary_file, "w") as f:
            f.write(f"Capture: {self.timestamp}\n")
            if self.stream_start_time and self.stream_end_time:
                f.write(f"Duration: {self.stream_end_time - self.stream_start_time:.1f}s\n")
            f.write(f"End reason: {self.stream_end_reason}\n")
            f.write(f"ISP lines: {len(isp_lines)}\n")
            f.write(f"MCU lines: {len(mcu_lines)}\n")
            f.write(f"Logcat lines: {len(logcat_lines)}\n")
        print(f"\n  Summary: {summary_file}")

    def teardown(self):
        if self.logcat:
            self.logcat.stop_capture()
        if self.isp:
            self.isp.disconnect()
        if self.mcu:
            self.mcu.disconnect()

    def run(self):
        try:
            self.setup()
            if not self.wait_for_stream():
                print("Aborting — no stream detected")
                return 1
            self.capture_until_end()
            isp, mcu, logcat = self.save_logs()
            self.analyze(isp, mcu, logcat)
            return 0
        except KeyboardInterrupt:
            print("\n\n[Ctrl+C] Stopping capture...")
            isp, mcu, logcat = self.save_logs()
            self.analyze(isp, mcu, logcat)
            return 0
        finally:
            self.teardown()


def main():
    parser = argparse.ArgumentParser(description="Stream Stop Debug — 3-channel capture")
    parser.add_argument('--timeout', type=int, default=900,
                        help='Max capture time in seconds (default 900 = 15min)')
    args = parser.parse_args()

    print("=" * 60)
    print("Stream Stop Debug — ISP + MCU + Android Logcat Capture")
    print("=" * 60)
    print(f"  Output: {LOG_DIR}/")
    print(f"  Timeout: {args.timeout}s")
    print()

    capture = StreamDebugCapture(timeout=args.timeout)
    sys.exit(capture.run())


if __name__ == "__main__":
    main()
