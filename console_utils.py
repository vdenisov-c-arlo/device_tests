"""Shared console connection utilities for test sequences.

Usage:
    from console_utils import (
        isp_init_console, get_serial_mux_config,
        SerialMuxReader, MCUReader, ISPReader,
        DeviceTestBase,
    )
"""

import configparser
import os
import socket
import subprocess
import sys
import threading
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_INI_PATH = os.environ.get(
    'SERIAL_MUX_INI',
    os.path.join(_SCRIPT_DIR, '..', 'serial_mux', 'serial_mux.ini'),
)


def get_serial_mux_config():
    """Read serial_mux.ini and return a dict with ISP/MCU/voodoo connection settings."""
    cfg = configparser.ConfigParser()
    cfg.read(_INI_PATH)
    return {
        'isp_host': cfg.get('isp', 'tcp_host', fallback='192.168.3.1'),
        'isp_port': cfg.getint('isp', 'tcp_port', fallback=9001),
        'mcu_host': cfg.get('mcu', 'tcp_host', fallback='192.168.3.1'),
        'mcu_port': cfg.getint('mcu', 'tcp_port', fallback=9002),
        'voodoo_host': cfg.get('voodoo', 'host', fallback='192.168.3.1'),
        'voodoo_port': cfg.getint('voodoo', 'modbus_port', fallback=502),
        'server_ip': cfg.get('server', 'host_ip', fallback='192.168.3.1'),
    }


def isp_init_console(sock, login="root", password="arlo", max_attempts=10):
    """Initialize ISP console: keep probing until login prompt or shell, then start tail -f.

    Sends CR/LF repeatedly until it sees a recognizable prompt (login: or #),
    performs login if needed, then starts tail -f /var/log/messages.
    """
    sock.sendall(b"\x03\r\n")
    time.sleep(0.5)
    _drain_sock(sock, 0.5)

    # Poll until we get a login prompt or shell prompt
    logged_in = False
    for attempt in range(max_attempts):
        sock.sendall(b"\r\n")
        time.sleep(2)
        response = _recv_all(sock, timeout=2.0)

        if "login:" in response:
            sock.sendall(f"{login}\r\n".encode())
            time.sleep(2)
            resp2 = _recv_all(sock, timeout=2.0)
            if "assword:" in resp2:
                sock.sendall(f"{password}\r\n".encode())
                time.sleep(2)
                _recv_all(sock, timeout=1.0)
            logged_in = True
            break
        elif "assword:" in response:
            sock.sendall(f"{password}\r\n".encode())
            time.sleep(2)
            _recv_all(sock, timeout=1.0)
            logged_in = True
            break
        elif response.rstrip().endswith("#"):
            logged_in = True
            break

    if not logged_in:
        return

    # Break any running command, dump full log then follow
    sock.sendall(b"\x03\r\n")
    time.sleep(0.5)
    _drain_sock(sock, 0.5)
    sock.sendall(b"cat /var/log/messages; tail -f /var/log/messages\r\n")
    time.sleep(0.5)


def _recv_all(sock, timeout=1.0):
    """Read whatever is available on the socket within timeout."""
    old_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, BlockingIOError, OSError):
        pass
    finally:
        sock.settimeout(old_timeout)
    return data.decode("utf-8", errors="replace")


def _drain_sock(sock, duration=0.5):
    """Discard any pending data on the socket."""
    end = time.time() + duration
    old_timeout = sock.gettimeout()
    sock.settimeout(0.1)
    try:
        while time.time() < end:
            try:
                sock.recv(4096)
            except (socket.timeout, BlockingIOError, OSError):
                break
    finally:
        sock.settimeout(old_timeout)


# ---------------------------------------------------------------------------
# SerialMuxReader — shared base class for console readers
# ---------------------------------------------------------------------------

class SerialMuxReader(threading.Thread):
    """TCP reader thread for serial_mux connections.

    Connects to a serial_mux TCP port, reads lines continuously, and dispatches
    them via either an event_callback or an overridable _process_line() method.

    Usage (callback style):
        reader = SerialMuxReader("MCU", host, port, event_callback=my_cb)

    Usage (subclass style):
        class MyReader(SerialMuxReader):
            def _process_line(self, line):
                ...
    """

    def __init__(self, name, host, port, event_callback=None):
        super().__init__(daemon=True)
        self.console_name = name
        self.host = host
        self.port = port
        self.event_callback = event_callback
        self.sock = None
        self.running = False
        self.lines = []
        self.lock = threading.Lock()
        self.recording = False

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((self.host, self.port))
        self.sock.settimeout(0.5)
        self.running = True

    def disconnect(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def reconnect(self):
        self.disconnect()
        time.sleep(1)
        self.connect()

    def start_recording(self):
        with self.lock:
            self.lines = []
            self.recording = True

    def stop_recording(self):
        with self.lock:
            self.recording = False

    def get_lines(self):
        with self.lock:
            return list(self.lines)

    def clear_lines(self):
        with self.lock:
            self.lines = []

    def drain(self, duration=2.0):
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
                    self._dispatch_line(line)
                if len(buf) > 4096:
                    line = buf.strip()
                    buf = ""
                    if line:
                        with self.lock:
                            if self.recording:
                                self.lines.append(line)
                        self._dispatch_line(line)
            except socket.timeout:
                continue
            except (BlockingIOError, ConnectionResetError, BrokenPipeError, OSError):
                if self.running:
                    time.sleep(0.5)

    def _dispatch_line(self, line):
        if self.event_callback:
            self.event_callback(line, self.console_name)
        else:
            self._process_line(line)

    def _process_line(self, line):
        """Override in subclass for direct event handling."""
        pass


# ---------------------------------------------------------------------------
# MCUReader / ISPReader — convenience subclasses with event tracking
# ---------------------------------------------------------------------------

class MCUReader(SerialMuxReader):
    """MCU console reader with sleep/anomaly event tracking.

    Fires threading.Event objects when specific patterns are detected.
    Tests can wait on these events or poll anomaly_info.

    Patterns are passed at construction so tests can customize detection
    without subclassing.
    """

    def __init__(self, host, port, *,
                 sleep_indicators=None,
                 isp_off_patterns=None,
                 anomaly_checker=None,
                 crash_line_checker=None,
                 event_callback=None):
        super().__init__("MCU", host, port, event_callback=event_callback)
        self.sleep_indicators = sleep_indicators or []
        self.isp_off_patterns = isp_off_patterns or []
        self.anomaly_checker = anomaly_checker
        self.crash_line_checker = crash_line_checker
        self.sleep_event = threading.Event()
        self.isp_off_event = threading.Event()
        self.anomaly_event = threading.Event()
        self.anomaly_info = None

    def start_recording(self):
        super().start_recording()
        self.sleep_event.clear()
        self.isp_off_event.clear()
        self.anomaly_event.clear()
        self.anomaly_info = None

    def _process_line(self, line):
        if self.sleep_indicators and any(p in line for p in self.sleep_indicators):
            self.sleep_event.set()
        if self.isp_off_patterns and any(p in line for p in self.isp_off_patterns):
            self.isp_off_event.set()
        if self.crash_line_checker and self.crash_line_checker(line):
            self.anomaly_info = ("CRASH", line)
            self.anomaly_event.set()
        elif self.anomaly_checker:
            anomaly_type, _ = self.anomaly_checker(line)
            if anomaly_type:
                self.anomaly_info = (anomaly_type, line)
                self.anomaly_event.set()


class ISPReader(SerialMuxReader):
    """ISP console reader with anomaly detection.

    Can perform console initialization (login + tail -f) via init_console().
    Anomaly detection skips lines matching expected_patterns (e.g. boot messages).
    """

    def __init__(self, host, port, *,
                 anomaly_checker=None,
                 expected_patterns=None,
                 event_callback=None):
        super().__init__("ISP", host, port, event_callback=event_callback)
        self.anomaly_checker = anomaly_checker
        self.expected_patterns = expected_patterns or []
        self.anomaly_event = threading.Event()
        self.anomaly_info = None

    def start_recording(self):
        super().start_recording()
        self.anomaly_event.clear()
        self.anomaly_info = None

    def init_console(self, login="root", password="arlo"):
        isp_init_console(self.sock, login=login, password=password)

    def _process_line(self, line):
        if self.expected_patterns and any(p in line for p in self.expected_patterns):
            return
        if self.anomaly_checker:
            anomaly_type, _ = self.anomaly_checker(line)
            if anomaly_type:
                self.anomaly_info = (anomaly_type, line)
                self.anomaly_event.set()


# ---------------------------------------------------------------------------
# DeviceTestBase — event-driven test state machine base class
# ---------------------------------------------------------------------------

class DeviceTestBase:
    """Base class for event-driven device test state machines.

    Provides:
      - MCU/ISP reader management (connect, disconnect, reconnect)
      - Event queue with wait/check/clear operations
      - Voodoo board button/DO control with retry
      - ISP console initialization
      - MCU sleep verification
      - Log saving
      - Recovery (hardware reset + wait for sleep)
      - Standard run loop with cycle/summary/exit-code pattern

    Subclasses implement:
      - _check_events(line, source): classify lines into events
      - run_cycle(cycle_num): single test cycle logic, returns True/False
      - Optionally override: _test_name, _log_dir, _num_cycles, _parse_args
    """

    _test_name = "device_test"
    _log_dir = "/tmp/device_test_logs"
    _sleep_timeout = 60
    _reset_recovery_timeout = 120
    _coredump_capture_timeout = 60

    def __init__(self):
        self.mcu = None
        self.isp = None
        self.events = []
        self.event_lock = threading.Lock()
        self.event_signal = threading.Event()
        self.results = []
        self._cfg = get_serial_mux_config()
        self._voodoo_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "voodoo_do_pulse.py")

    # --- Event queue ---

    def event_callback(self, event, source, line):
        with self.event_lock:
            self.events.append((event, source, line))
        self.event_signal.set()

    def wait_for_event(self, target_event, timeout):
        """Wait for a specific event type. Returns (event, source, line) or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self.event_signal.wait(timeout=min(remaining, 0.5))
            self.event_signal.clear()
            with self.event_lock:
                for i, (evt, src, line) in enumerate(self.events):
                    if evt == target_event:
                        self.events.pop(i)
                        return (evt, src, line)
        return None

    def wait_for_any_event(self, target_events, timeout):
        """Wait for any of the given event types. Returns (event, source, line) or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self.event_signal.wait(timeout=min(remaining, 0.5))
            self.event_signal.clear()
            with self.event_lock:
                for i, (evt, src, line) in enumerate(self.events):
                    if evt in target_events:
                        self.events.pop(i)
                        return (evt, src, line)
        return None

    def check_event(self, target_event):
        """Non-blocking check. Returns (event, source, line) or None."""
        with self.event_lock:
            for i, (evt, src, line) in enumerate(self.events):
                if evt == target_event:
                    self.events.pop(i)
                    return (evt, src, line)
        return None

    def clear_events(self):
        with self.event_lock:
            self.events = []
        self.event_signal.clear()

    # --- Line dispatch (called by readers) ---

    def _line_callback(self, line, source):
        """Called by SerialMuxReader for each line. Routes to subclass _check_events."""
        self._check_events(line, source)

    def _check_events(self, line, source):
        """Override in subclass to classify lines into events via self.event_callback()."""
        pass

    # --- Console management ---

    def connect_consoles(self):
        """Connect MCU and ISP readers."""
        self.mcu = SerialMuxReader(
            "MCU", self._cfg['mcu_host'], self._cfg['mcu_port'],
            event_callback=self._line_callback)
        self.mcu.connect()
        self.mcu.start()

        self.isp = SerialMuxReader(
            "ISP", self._cfg['isp_host'], self._cfg['isp_port'],
            event_callback=self._line_callback)
        self.isp.connect()
        self.isp.start()

    def disconnect_consoles(self):
        if self.mcu:
            self.mcu.disconnect()
        if self.isp:
            self.isp.disconnect()

    def reconnect_consoles(self):
        """Disconnect and reconnect both readers (creates new threads)."""
        self.disconnect_consoles()
        time.sleep(2)
        self.connect_consoles()

    def init_isp_console(self):
        """Login to ISP console and start tail -f."""
        if not self.isp or not self.isp.sock:
            return
        isp_init_console(self.isp.sock)

    # --- MCU sleep verification ---

    def verify_sleep(self, probes=5, interval=0.1, wait_after=2.0):
        """Send CR/LF to MCU, return True if no response (asleep)."""
        if not self.mcu or not self.mcu.sock:
            return False
        time.sleep(0.5)
        try:
            for _ in range(probes):
                self.mcu.sock.sendall(b"\r\n")
                time.sleep(interval)
        except OSError:
            return False
        time.sleep(wait_after)
        lines_before = len(self.mcu.get_lines())
        time.sleep(1.0)
        lines_after = len(self.mcu.get_lines())
        return lines_after == lines_before

    # --- Battery / ALWAYS_ON detection ---

    def check_always_on(self):
        """Issue 'battery info' on MCU console, return True if bAlwaysOnMode = true.

        When battery is >95% and USB is plugged, the device enters ALWAYS_ON mode
        and will never sleep. Callers should unplug USB or skip waiting for sleep.
        """
        if not self.mcu or not self.mcu.sock:
            return False
        try:
            self.mcu.clear_lines()
            self.mcu.sock.sendall(b"battery info\r\n")
            time.sleep(3)
            lines = self.mcu.get_lines()
            for line in lines:
                if "bAlwaysOnMode = true" in line or "bAlwaysOnMode=true" in line:
                    return True
            return False
        except OSError:
            return False

    # --- Voodoo board control ---

    def press_button(self, channel, duration=0.3, retries=3):
        """Pulse voodoo DO channel. Returns True on success."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [sys.executable, self._voodoo_script, str(channel), str(duration)],
                    capture_output=True, timeout=15, text=True)
                if result.returncode == 0:
                    return True
                print(f"  [WARN] voodoo attempt {attempt+1}/{retries} failed: "
                      f"{result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"  [WARN] voodoo attempt {attempt+1}/{retries} timed out")
            time.sleep(1)
        print(f"  [ERROR] voodoo pulse DO{channel} failed after {retries} retries")
        return False

    def voodoo_on(self, channel):
        """Turn voodoo DO on indefinitely."""
        result = subprocess.run(
            [sys.executable, self._voodoo_script, "--on", str(channel)],
            capture_output=True, timeout=10, text=True)
        return result.returncode == 0

    def voodoo_off(self, channel):
        """Turn voodoo DO off."""
        result = subprocess.run(
            [sys.executable, self._voodoo_script, "--off", str(channel)],
            capture_output=True, timeout=10, text=True)
        return result.returncode == 0

    def voodoo_on_pair(self, channel_a, channel_b):
        """Turn two voodoo DOs on together."""
        ok_a = self.voodoo_on(channel_a)
        ok_b = self.voodoo_on(channel_b)
        return ok_a and ok_b

    def voodoo_off_pair(self, channel_a, channel_b):
        """Turn two voodoo DOs off together."""
        ok_a = self.voodoo_off(channel_a)
        ok_b = self.voodoo_off(channel_b)
        return ok_a and ok_b

    # --- Log saving ---

    def save_logs(self, cycle, label):
        """Save MCU and ISP logs to files."""
        from datetime import datetime
        os.makedirs(self._log_dir, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        mcu_lines = self.mcu.get_lines() if self.mcu else []
        isp_lines = self.isp.get_lines() if self.isp else []
        if mcu_lines:
            path = os.path.join(self._log_dir, f"{self._test_name}_cycle{cycle}_{label}_mcu_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(mcu_lines))
            print(f"  [SAVED] {path}")
        if isp_lines:
            path = os.path.join(self._log_dir, f"{self._test_name}_cycle{cycle}_{label}_isp_{ts}.log")
            with open(path, "w") as f:
                f.write("\n".join(isp_lines))
            print(f"  [SAVED] {path}")

    # --- Coredump capture ---

    def capture_coredump(self, cycle):
        """Wait for coredump to finish streaming, save to file."""
        print(f"  [COREDUMP] Capturing (up to {self._coredump_capture_timeout}s)...")
        start = time.time()
        lines_before = len(self.mcu.get_lines()) if self.mcu else 0
        idle_count = 0

        while time.time() - start < self._coredump_capture_timeout:
            time.sleep(2)
            lines_now = len(self.mcu.get_lines()) if self.mcu else 0
            if lines_now == lines_before:
                idle_count += 1
                if idle_count >= 3:
                    print("  [COREDUMP] Complete (no new data for 6s)")
                    break
            else:
                idle_count = 0
                lines_before = lines_now

        self.save_logs(cycle, "coredump")

    # --- Recovery ---

    def recovery(self, cycle, reset_channel=2, reset_duration=1.0):
        """Reset device and wait for it to sleep again. Returns True if recovered."""
        print(f"\n  [RECOVERY] Resetting device...")
        self.press_button(reset_channel, reset_duration)
        time.sleep(3)

        self.reconnect_consoles()
        self.clear_events()
        self.mcu.start_recording()

        print(f"  [RECOVERY] Waiting for sleep (up to {self._reset_recovery_timeout}s)...")
        # Subclass should fire a sleep event; we wait for it generically
        deadline = time.time() + self._reset_recovery_timeout
        while time.time() < deadline:
            time.sleep(1)
            # Check if any event indicates sleep — subclass-specific
            # Fallback: probe MCU console
        self.mcu.stop_recording()

        # Verify via probe
        if self.verify_sleep():
            print("  [RECOVERY] Device sleeping again")
            time.sleep(5)
            return True
        print("  [RECOVERY] Timeout — device didn't sleep")
        return False

    # --- Main run loop ---

    def run_cycle(self, cycle_num):
        """Override in subclass. Run one test cycle, return True for PASS."""
        raise NotImplementedError

    def run(self, num_cycles=1):
        """Standard test run loop with connect, cycle, summary, exit code."""
        os.makedirs(self._log_dir, exist_ok=True)

        print(f"=== {self._test_name} ({num_cycles} cycles) ===")
        print(f"  Log directory: {self._log_dir}")
        print()

        print("[INIT] Connecting consoles...")
        self.connect_consoles()

        print("[INIT] Draining stale buffers...")
        time.sleep(2)
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

        # Summary
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
            fail_count = self.results.count(False)
            print(f"\nRESULT: FAIL ({fail_count} failures)")
            print(f"Logs saved to: {self._log_dir}")
            return 1
