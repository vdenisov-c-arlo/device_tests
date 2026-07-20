#!/usr/bin/env python3
"""Control digital outputs on the voodooboard.

Usage (CLI):
  voodoo_do_pulse.py <do_num> [duration]       Pulse DO for duration seconds (default 2s)
  voodoo_do_pulse.py --on <do_num>             Turn DO on indefinitely
  voodoo_do_pulse.py --off <do_num>            Turn DO off
  voodoo_do_pulse.py --read                    Read current DO register state

Usage (library):
  from voodoo.voodoo_do_pulse import VoodooBoard
  vb = VoodooBoard()
  vb.pulse(7, duration=2.0)
  vb.on(6)
  vb.off(6)
  state = vb.read()
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


class VoodooBoard:
    """Modbus TCP client for voodooboard digital output control.

    Maintains a persistent connection and uses read-modify-write for all
    operations to preserve the state of other DO channels.
    """

    def __init__(self, host=None, port=None):
        self.host = host or DEFAULT_HOST
        self.port = port or MODBUS_TCP_PORT
        self.sock = None
        self._tid = 0

    def connect(self):
        if self.sock:
            return
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect((self.host, self.port))
        time.sleep(0.1)

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _ensure_connected(self):
        if not self.sock:
            self.connect()

    def _next_tid(self):
        self._tid += 1
        return self._tid

    def _write_register(self, value):
        self._ensure_connected()
        tid = self._next_tid()
        pdu = struct.pack('>BHH', 0x06, DO_REGISTER, value)
        mbap = struct.pack('>HHHB', tid, 0x0000, len(pdu) + 1, SLAVE_ADDR)
        self.sock.sendall(mbap + pdu)
        self.sock.settimeout(2.0)
        try:
            resp = self.sock.recv(256)
        except socket.timeout:
            print(f"  [WARN] No Modbus response for tid={tid}", file=sys.stderr)
            return None
        if len(resp) < 9:
            raise RuntimeError(f"Short response ({len(resp)} bytes)")
        if resp[7] & 0x80:
            raise RuntimeError(f"Modbus exception: code {resp[8]}")
        return resp

    def _read_register(self):
        self._ensure_connected()
        tid = self._next_tid()
        pdu = struct.pack('>BHH', 0x03, DO_REGISTER, 1)
        mbap = struct.pack('>HHHB', tid, 0x0000, len(pdu) + 1, SLAVE_ADDR)
        self.sock.sendall(mbap + pdu)
        self.sock.settimeout(2.0)
        try:
            resp = self.sock.recv(256)
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

    def read(self):
        """Read current DO register state. Returns int (bitmask)."""
        return self._read_register()

    def on(self, do_num):
        """Turn a single DO on (read-modify-write)."""
        cur = self._read_register()
        new_val = cur | (1 << do_num)
        self._write_register(new_val)
        return new_val

    def off(self, do_num):
        """Turn a single DO off (read-modify-write)."""
        cur = self._read_register()
        new_val = cur & ~(1 << do_num)
        self._write_register(new_val)
        return new_val

    def pulse(self, do_num, duration=DEFAULT_DURATION):
        """Pulse a DO on then off (read-modify-write)."""
        cur = self._read_register()
        new_val = cur | (1 << do_num)
        self._write_register(new_val)
        time.sleep(duration)
        restore = new_val & ~(1 << do_num)
        self._write_register(restore)
        return restore

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()


# --- Legacy function API (used by ble_onboard.py) ---

def connect(host=None, port=None):
    """Legacy: connect and return raw socket."""
    host = host or DEFAULT_HOST
    port = port or MODBUS_TCP_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect((host, port))
    time.sleep(0.1)
    return sock


def write_do(sock, value, tid):
    """Legacy: write DO register on an existing socket."""
    pdu = struct.pack('>BHH', 0x06, DO_REGISTER, value)
    mbap = struct.pack('>HHHB', tid, 0x0000, len(pdu) + 1, SLAVE_ADDR)
    sock.sendall(mbap + pdu)
    sock.settimeout(2.0)
    try:
        resp = sock.recv(256)
    except socket.timeout:
        print(f"  [WARN] No Modbus response for tid={tid}", file=sys.stderr)
        return None
    if len(resp) < 9:
        raise RuntimeError(f"Short response ({len(resp)} bytes)")
    if resp[7] & 0x80:
        raise RuntimeError(f"Modbus exception: code {resp[8]}")
    return resp


def read_do(sock, tid):
    """Legacy: read DO register on an existing socket."""
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

    with VoodooBoard(args.host, args.port) as vb:
        if args.read:
            value = vb.read()
            print(f"DO register: 0x{value:04X} (binary: {value:08b})")
            for i in range(8):
                if value & (1 << i):
                    print(f"  DO{i}: ON")
            return

        if args.on is not None:
            new_val = vb.on(args.on)
            print(f"DO{args.on} ON (register: 0x{new_val:04X})")
            return

        if args.off is not None:
            new_val = vb.off(args.off)
            print(f"DO{args.off} OFF (register: 0x{new_val:04X})")
            return

        if args.do_num is None:
            parser.print_help()
            sys.exit(1)

        restore = vb.pulse(args.do_num, args.duration)
        print(f"DO{args.do_num} pulsed for {args.duration}s (register: 0x{restore:04X})")


if __name__ == "__main__":
    main()
