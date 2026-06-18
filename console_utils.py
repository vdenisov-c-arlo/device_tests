"""Shared console connection utilities for test sequences.

Usage:
    from console_utils import isp_init_console, get_serial_mux_config
"""

import configparser
import os
import socket
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_INI_PATH = os.path.join(_SCRIPT_DIR, 'serial_mux', 'serial_mux.ini')


def get_serial_mux_config():
    """Read serial_mux.ini and return a dict with ISP/MCU/voodoo connection settings."""
    cfg = configparser.ConfigParser()
    cfg.read(_INI_PATH)
    return {
        'isp_host': cfg.get('isp', 'tcp_host', fallback='10.9.8.8'),
        'isp_port': cfg.getint('isp', 'tcp_port', fallback=9001),
        'mcu_host': cfg.get('mcu', 'tcp_host', fallback='10.9.8.8'),
        'mcu_port': cfg.getint('mcu', 'tcp_port', fallback=9002),
        'voodoo_host': cfg.get('voodoo', 'host', fallback='10.9.8.8'),
        'voodoo_port': cfg.getint('voodoo', 'modbus_port', fallback=502),
        'server_ip': cfg.get('server', 'host_ip', fallback='10.9.8.7'),
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
