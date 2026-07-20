#!/usr/bin/env python3
"""Control digital outputs on the voodooboard.

Usage:
  voodoo_do_pulse.py <do_num> [duration]       Pulse DO for duration seconds (default 2s)
  voodoo_do_pulse.py --on <do_num>             Turn DO on indefinitely
  voodoo_do_pulse.py --off <do_num>            Turn DO off
  voodoo_do_pulse.py --read                    Read current DO register state
"""

import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import get_serial_mux_config

_cfg = get_serial_mux_config()

SLAVE_ADDR = 0xFF
DO_REGISTER = 1  # MBHR_DISCRETE_OUTPUTS_LOW
MODBUS_TCP_PORT = _cfg['voodoo_port']
DEFAULT_HOST = _cfg['voodoo_host']
DEFAULT_DURATION = 2.0


def write_do(sock, value, tid):
    pdu = struct.pack('>BHH', 0x06, DO_REGISTER, value)
    mbap = struct.pack('>HHHB', tid, 0x0000, len(pdu) + 1, SLAVE_ADDR)
    sock.sendall(mbap + pdu)
    sock.settimeout(2.0)
    try:
        resp = sock.recv(256)
    except socket.timeout:
        print(f"  [WARN] No Modbus response for tid={tid} (command likely still executed)", file=sys.stderr)
        return None
    if len(resp) < 9:
        raise RuntimeError(f"Short response ({len(resp)} bytes)")
    if resp[7] & 0x80:
        raise RuntimeError(f"Modbus exception: code {resp[8]}")
    return resp


def read_do(sock, tid):
    pdu = struct.pack('>BHH', 0x03, DO_REGISTER, 1)
    mbap = struct.pack('>HHHB', tid, 0x0000, len(pdu) + 1, SLAVE_ADDR)
    sock.sendall(mbap + pdu)
    sock.settimeout(2.0)
    try:
        resp = sock.recv(256)
    except socket.timeout:
        raise RuntimeError("No Modbus response (timeout)")
    if len(resp) < 9:
        raise RuntimeError(f"Short response ({len(resp)} bytes)")
    if resp[7] & 0x80:
        raise RuntimeError(f"Modbus exception: code {resp[8]}")
    byte_count = resp[8]
    if byte_count >= 2:
        value = struct.unpack('>H', resp[9:11])[0]
    else:
        value = resp[9]
    return value


def connect(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect((host, port))
    time.sleep(0.1)
    return sock


def main():
    parser = argparse.ArgumentParser(description="Voodooboard digital output control")
    parser.add_argument('--on', type=int, metavar='DO', help='Turn DO on indefinitely')
    parser.add_argument('--off', type=int, metavar='DO', help='Turn DO off')
    parser.add_argument('--read', action='store_true', help='Read current DO register state')
    parser.add_argument('--host', default=DEFAULT_HOST, help='Voodooboard IP')
    parser.add_argument('--port', type=int, default=MODBUS_TCP_PORT, help='Modbus TCP port')
    parser.add_argument('do_num', nargs='?', type=int, help='DO number for pulse mode')
    parser.add_argument('duration', nargs='?', type=float, default=DEFAULT_DURATION,
                        help='Pulse duration in seconds (default 2)')

    args = parser.parse_args()
    print(f"host = {args.host}")

    sock = connect(args.host, args.port)
    try:
        if args.read:
            value = read_do(sock, 1)
            print(f"DO register: 0x{value:04X} (binary: {value:08b})")
            for i in range(8):
                if value & (1 << i):
                    print(f"  DO{i}: ON")
            return

        if args.on is not None:
            cur = read_do(sock, 1)
            new_val = cur | (1 << args.on)
            write_do(sock, new_val, 2)
            print(f"DO{args.on} ON (register: 0x{new_val:04X})")
            return

        if args.off is not None:
            cur = read_do(sock, 1)
            new_val = cur & ~(1 << args.off)
            write_do(sock, new_val, 2)
            print(f"DO{args.off} OFF (register: 0x{new_val:04X})")
            return

        if args.do_num is None:
            parser.print_help()
            sys.exit(1)

        cur = read_do(sock, 1)
        new_val = cur | (1 << args.do_num)
        write_do(sock, new_val, 2)
        print(f"DO{args.do_num} ON")
        time.sleep(args.duration)
        restore = new_val & ~(1 << args.do_num)
        write_do(sock, restore, 3)
        print(f"DO{args.do_num} OFF (after {args.duration}s)")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
