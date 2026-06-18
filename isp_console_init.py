#!/usr/bin/env python3
"""ISP console initialization: break stale commands, login if needed, start log tail.

Usage:
    python3 isp_console_init.py [--no-tail] [--command CMD]

Returns the connected socket on stdout (for piping), or prints log lines.
When imported, call isp_console_connect() to get an initialized socket.
"""

import os
import socket
import time
import select
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import get_serial_mux_config

_cfg = get_serial_mux_config()
ISP_HOST = _cfg['isp_host']
ISP_PORT = _cfg['isp_port']
LOGIN_USER = "root"
LOGIN_PASS = "arlo"


def read_available(sock, timeout=1.0):
    """Read all available data from a non-blocking socket."""
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        ready, _, _ = select.select([sock], [], [], 0.2)
        if not ready:
            if buf:
                break
            continue
        try:
            data = sock.recv(4096)
            if data:
                buf += data
            else:
                break
        except (BlockingIOError, socket.error):
            break
    return buf


def isp_console_connect(start_tail=True, command=None):
    """Connect to ISP console, handle login, optionally start tail.

    Returns (socket, initial_output_str).
    Socket is left in non-blocking mode.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((ISP_HOST, ISP_PORT))
    sock.setblocking(False)

    # Step 1: Send Ctrl+C to break any running command
    for _ in range(4):
        sock.sendall(b"\x03")
        time.sleep(0.1)

    time.sleep(0.3)
    sock.sendall(b"\r\n")
    time.sleep(0.8)

    response = read_available(sock, timeout=1.0).decode("utf-8", errors="replace")

    # Step 2: Handle login if needed
    if "login:" in response.lower():
        sock.sendall(f"{LOGIN_USER}\r\n".encode())
        time.sleep(1.0)
        resp2 = read_available(sock, timeout=1.5).decode("utf-8", errors="replace")
        if "assword" in resp2:
            sock.sendall(f"{LOGIN_PASS}\r\n".encode())
            time.sleep(1.0)
            resp3 = read_available(sock, timeout=1.5).decode("utf-8", errors="replace")
            response = resp3
        else:
            response = resp2
    elif not response:
        # Maybe device is asleep or slow, send another enter
        sock.sendall(b"\r\n")
        time.sleep(1.0)
        response = read_available(sock, timeout=1.5).decode("utf-8", errors="replace")
        if "login:" in response.lower():
            sock.sendall(f"{LOGIN_USER}\r\n".encode())
            time.sleep(1.0)
            resp2 = read_available(sock, timeout=1.5).decode("utf-8", errors="replace")
            if "assword" in resp2:
                sock.sendall(f"{LOGIN_PASS}\r\n".encode())
                time.sleep(1.0)
                response = read_available(sock, timeout=1.5).decode("utf-8", errors="replace")

    # Step 3: Start tail or custom command
    if command:
        sock.sendall(f"{command}\r\n".encode())
    elif start_tail:
        sock.sendall(b"tail -f /var/log/messages\r\n")

    time.sleep(1.0)
    initial = read_available(sock, timeout=1.5).decode("utf-8", errors="replace")

    return sock, initial


def main():
    no_tail = "--no-tail" in sys.argv
    command = None
    if "--command" in sys.argv:
        idx = sys.argv.index("--command")
        if idx + 1 < len(sys.argv):
            command = sys.argv[idx + 1]

    print(f"[ISP] Connecting to {ISP_HOST}:{ISP_PORT}...")
    sock, initial = isp_console_connect(start_tail=not no_tail and not command, command=command)

    if initial.strip():
        print(f"[ISP] Initial output:")
        for line in initial.strip().split("\n")[-15:]:
            print(f"  {line.rstrip()[:140]}")

    if no_tail and not command:
        print("[ISP] Connected, no tail started.")
        sock.close()
        return

    print("[ISP] Monitoring (Ctrl+C to stop)...")
    try:
        while True:
            ready, _, _ = select.select([sock], [], [], 1.0)
            if ready:
                data = sock.recv(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                for line in text.split("\n"):
                    if line.strip():
                        print(line.rstrip()[:160])
    except KeyboardInterrupt:
        print("\n[ISP] Stopped.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
