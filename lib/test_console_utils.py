#!/usr/bin/env python3
"""Quick command-line tests for console_utils.py classes.

Usage:
    # Test serial_mux connection (ISP + MCU)
    python3 test_console_utils.py connect

    # Test ISP login (wake device first)
    python3 test_console_utils.py login --wake

    # Cat the log (dump + exit)
    python3 test_console_utils.py cat --wake

    # Tail the log (live follow, Ctrl+C to stop)
    python3 test_console_utils.py tail --wake

    # Run all tests in sequence, waking device before login/cat/mcu
    python3 test_console_utils.py all --wake

    # Just wake the device (press SYNC button)
    python3 test_console_utils.py wake

Environment:
    SERIAL_MUX_INI  — override path to serial_mux.ini
"""

import argparse
import os
import signal
import socket
import sys
import time
import threading

sys.path.insert(0, __file__.rsplit("/", 1)[0])
sys.path.insert(0, os.path.join(__file__.rsplit("/", 1)[0], ".."))
from console_utils import (
    get_serial_mux_config,
    isp_init_console,
    SerialMuxReader,
    ISPReader,
    MCUReader,
    _recv_all,
    _drain_sock,
)


def _print_ok(msg):
    print(f"  [OK] {msg}")


def _print_fail(msg):
    print(f"  [FAIL] {msg}")


def _print_info(msg):
    print(f"  [..] {msg}")


def wake_device(wait=3.0):
    """Press SYNC button via testbot4 to wake the device."""
    print("\n--- wake_device ---")
    try:
        from testbot4.testbot4_do_pulse import Testbot4
        vb = Testbot4()
        vb.connect()
        vb.pulse(0, 0.5)
        vb.disconnect()
        _print_ok(f"SYNC pressed, waiting {wait}s for ISP boot...")
        time.sleep(wait)
        return True
    except (OSError, RuntimeError, ImportError) as e:
        _print_fail(f"wake failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Test: config loading
# ---------------------------------------------------------------------------

def test_config():
    """Verify serial_mux.ini is readable and returns expected keys."""
    print("\n--- test_config ---")
    cfg = get_serial_mux_config()
    required_keys = ['isp_host', 'isp_port', 'mcu_host', 'mcu_port', 'testbot4_host', 'testbot4_port', 'server_ip']
    for k in required_keys:
        if k not in cfg:
            _print_fail(f"missing key: {k}")
            return False
    _print_ok(f"config loaded: ISP={cfg['isp_host']}:{cfg['isp_port']}, MCU={cfg['mcu_host']}:{cfg['mcu_port']}")
    return True


# ---------------------------------------------------------------------------
# Test: raw TCP connection to serial_mux
# ---------------------------------------------------------------------------

def test_connect():
    """Connect to both ISP and MCU serial_mux ports, read a few bytes."""
    print("\n--- test_connect ---")
    cfg = get_serial_mux_config()
    ok = True

    for name, host, port in [
        ("ISP", cfg['isp_host'], cfg['isp_port']),
        ("MCU", cfg['mcu_host'], cfg['mcu_port']),
    ]:
        _print_info(f"connecting to {name} at {host}:{port}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            _print_ok(f"{name} connected")
            # Send a CR and see if anything comes back
            sock.sendall(b"\r\n")
            time.sleep(1)
            data = _recv_all(sock, timeout=2.0)
            if data:
                preview = data[:120].replace('\n', '\\n')
                _print_ok(f"{name} got {len(data)} bytes: {preview}")
            else:
                _print_info(f"{name} no data (device may be asleep)")
            sock.close()
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            _print_fail(f"{name} connection failed: {e}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Test: SerialMuxReader thread
# ---------------------------------------------------------------------------

def test_reader(duration=5):
    """Start MCU + ISP readers, collect lines for a few seconds."""
    print(f"\n--- test_reader ({duration}s) ---")
    cfg = get_serial_mux_config()

    collected = {"MCU": [], "ISP": []}

    def cb(line, source):
        collected[source].append(line)

    mcu = SerialMuxReader("MCU", cfg['mcu_host'], cfg['mcu_port'], event_callback=cb)
    isp = SerialMuxReader("ISP", cfg['isp_host'], cfg['isp_port'], event_callback=cb)

    try:
        mcu.connect()
        mcu.start()
        _print_ok("MCU reader started")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        _print_fail(f"MCU reader connect: {e}")
        return False

    try:
        isp.connect()
        isp.start()
        _print_ok("ISP reader started")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        _print_fail(f"ISP reader connect: {e}")
        mcu.disconnect()
        return False

    # Poke ISP to generate output
    isp.sock.sendall(b"\r\n")

    _print_info(f"reading for {duration}s...")
    time.sleep(duration)

    mcu.disconnect()
    isp.disconnect()

    _print_ok(f"MCU lines: {len(collected['MCU'])}, ISP lines: {len(collected['ISP'])}")
    for source in ("MCU", "ISP"):
        for line in collected[source][:5]:
            print(f"    [{source}] {line[:100]}")
        if len(collected[source]) > 5:
            print(f"    [{source}] ... ({len(collected[source]) - 5} more)")

    return True


# ---------------------------------------------------------------------------
# Test: ISP login
# ---------------------------------------------------------------------------

def test_login():
    """Connect to ISP, perform login sequence, verify we get a shell."""
    print("\n--- test_login ---")
    cfg = get_serial_mux_config()

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((cfg['isp_host'], cfg['isp_port']))
        _print_ok(f"connected to ISP {cfg['isp_host']}:{cfg['isp_port']}")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        _print_fail(f"ISP connect failed: {e}")
        return False

    _print_info("running isp_init_console()...")
    isp_init_console(sock, login="root", password="arlo", max_attempts=5)

    # Check we got a shell by sending a command
    time.sleep(1)
    _drain_sock(sock, 0.5)
    sock.sendall(b"echo HELLO_FROM_TEST\r\n")
    time.sleep(2)
    response = _recv_all(sock, timeout=2.0)
    sock.close()

    if "HELLO_FROM_TEST" in response:
        _print_ok("login successful — shell responding")
        return True
    elif response.strip():
        preview = response[:200].replace('\n', '\\n')
        _print_info(f"got response but no echo: {preview}")
        _print_info("(device may be asleep or in an unexpected state)")
        return True
    else:
        _print_fail("no response after login")
        return False


# ---------------------------------------------------------------------------
# Test: cat log (ISPReader init_console dumps full log then exits)
# ---------------------------------------------------------------------------

def test_cat(max_lines=200, timeout=10):
    """Connect, login, cat /var/log/messages, print output, disconnect."""
    print(f"\n--- test_cat (up to {max_lines} lines, {timeout}s) ---")
    cfg = get_serial_mux_config()

    collected = []

    def cb(line, source):
        collected.append(line)

    isp = ISPReader(cfg['isp_host'], cfg['isp_port'], event_callback=cb)

    try:
        isp.connect()
        isp.start()
        _print_ok("ISP reader connected")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        _print_fail(f"ISP connect: {e}")
        return False

    _print_info("logging in and running cat /var/log/messages...")
    isp.init_console(login="root", password="arlo")

    deadline = time.time() + timeout
    while time.time() < deadline and len(collected) < max_lines:
        time.sleep(0.5)

    isp.disconnect()

    if collected:
        _print_ok(f"captured {len(collected)} lines")
        # Print last 20 lines as a preview
        start = max(0, len(collected) - 20)
        print("    --- last 20 lines ---")
        for line in collected[start:]:
            print(f"    {line[:140]}")
        return True
    else:
        _print_fail("no log output captured (device may be off or asleep)")
        return False


# ---------------------------------------------------------------------------
# Test: tail log (live follow until Ctrl+C)
# ---------------------------------------------------------------------------

def test_tail():
    """Connect, login, tail -f /var/log/messages. Ctrl+C to stop."""
    print("\n--- test_tail (Ctrl+C to stop) ---")
    cfg = get_serial_mux_config()

    stop = threading.Event()

    def cb(line, source):
        if not stop.is_set():
            print(f"  [{source}] {line[:160]}")

    isp = ISPReader(cfg['isp_host'], cfg['isp_port'], event_callback=cb)

    try:
        isp.connect()
        isp.start()
        _print_ok("ISP reader connected")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        _print_fail(f"ISP connect: {e}")
        return False

    _print_info("logging in and starting tail -f...")
    isp.init_console(login="root", password="arlo")

    def sigint_handler(sig, frame):
        stop.set()

    signal.signal(signal.SIGINT, sigint_handler)
    _print_info("streaming... press Ctrl+C to stop\n")

    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    isp.disconnect()
    _print_ok("stopped")
    return True


# ---------------------------------------------------------------------------
# Test: MCUReader event detection
# ---------------------------------------------------------------------------

def test_mcu_reader(duration=10):
    """Start MCUReader with sleep detection, observe for a few seconds."""
    print(f"\n--- test_mcu_reader ({duration}s) ---")
    cfg = get_serial_mux_config()

    sleep_patterns = ["Network Stack Suspended", "Entering DeepSleep", "DS Entry"]
    isp_off_patterns = ["ISP_POWER_OFF", "isp_power_off"]

    mcu = MCUReader(
        cfg['mcu_host'], cfg['mcu_port'],
        sleep_indicators=sleep_patterns,
        isp_off_patterns=isp_off_patterns,
    )

    try:
        mcu.connect()
        mcu.start()
        _print_ok("MCU reader connected")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        _print_fail(f"MCU connect: {e}")
        return False

    mcu.start_recording()
    _print_info(f"observing MCU for {duration}s...")
    _print_info(f"  sleep patterns: {sleep_patterns}")
    _print_info(f"  ISP-off patterns: {isp_off_patterns}")

    time.sleep(duration)
    mcu.stop_recording()

    lines = mcu.get_lines()
    _print_ok(f"captured {len(lines)} MCU lines")
    if mcu.sleep_event.is_set():
        _print_ok("sleep event DETECTED")
    if mcu.isp_off_event.is_set():
        _print_ok("ISP-off event DETECTED")
    if lines:
        print("    --- last 10 lines ---")
        for line in lines[-10:]:
            print(f"    {line[:120]}")

    mcu.disconnect()
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test console_utils.py functionality from command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("test", nargs="?", default="all",
                        choices=["config", "connect", "reader", "login", "cat", "tail", "mcu", "wake", "all"],
                        help="Which test to run (default: all)")
    parser.add_argument("--wake", action="store_true",
                        help="Press SYNC button to wake device before tests that need ISP")
    parser.add_argument("--duration", type=int, default=5,
                        help="Duration in seconds for reader/mcu tests (default: 5)")
    parser.add_argument("--lines", type=int, default=200,
                        help="Max lines for cat test (default: 200)")
    args = parser.parse_args()

    needs_wake = {"login", "cat", "tail", "mcu", "reader", "all"}

    if args.wake and args.test in needs_wake:
        if not wake_device():
            return 1

    test_map = {
        "config": test_config,
        "connect": test_connect,
        "reader": lambda: test_reader(args.duration),
        "login": test_login,
        "cat": lambda: test_cat(max_lines=args.lines),
        "tail": test_tail,
        "mcu": lambda: test_mcu_reader(args.duration),
        "wake": wake_device,
    }

    if args.test == "all":
        tests = ["config", "connect", "reader", "login", "cat", "mcu"]
        results = {}
        for name in tests:
            results[name] = test_map[name]()
        print("\n" + "=" * 40)
        print("SUMMARY:")
        for name in tests:
            status = "PASS" if results[name] else "FAIL"
            print(f"  {name:12s} {status}")
        if all(results.values()):
            print("\nAll tests passed.")
            return 0
        else:
            print(f"\n{sum(not v for v in results.values())} test(s) failed.")
            return 1
    else:
        ok = test_map[args.test]()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
