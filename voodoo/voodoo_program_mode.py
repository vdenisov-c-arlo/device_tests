#!/usr/bin/env python3
"""Put device into USB ROM boot mode by holding Program button while pressing Reset."""

import configparser
import os
import socket
import struct
import time
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_ini = configparser.ConfigParser()
_ini.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'serial_mux', 'serial_mux.ini'))

SLAVE_ADDR = 0xFF
DO_REG = 1
MODBUS_TCP_PORT = _ini.getint('voodoo', 'modbus_port', fallback=502)
DEFAULT_HOST = _ini.get('voodoo', 'host', fallback='192.168.3.1')

from voodoo.voodoo_channels import DO_PROGRAM, DO_RESET
DO_PROGRAM_MASK = 1 << DO_PROGRAM  # 0x08
DO_RESET_MASK = 1 << DO_RESET      # 0x04


def write_do(sock, value, tid):
    pdu = struct.pack('>BHH', 0x06, DO_REG, value)
    mbap = struct.pack('>HHHB', tid, 0x0000, len(pdu) + 1, SLAVE_ADDR)
    sock.sendall(mbap + pdu)
    sock.settimeout(2.0)
    try:
        sock.recv(256)
    except socket.timeout:
        print(f"  [WARN] No Modbus response for tid={tid}", file=sys.stderr)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else MODBUS_TCP_PORT

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect((host, port))
    time.sleep(0.1)

    try:
        # Hold Program
        write_do(sock, DO_PROGRAM_MASK, 1)
        print("Program button DOWN")
        time.sleep(0.3)

        # Press Reset while holding Program
        write_do(sock, DO_PROGRAM_MASK | DO_RESET_MASK, 2)
        print("Reset button DOWN (Program still held)")
        time.sleep(2.0)

        # Release Reset, keep Program held
        write_do(sock, DO_PROGRAM_MASK, 3)
        print("Reset button UP (Program still held)")
        time.sleep(1.0)

        # Release Program
        write_do(sock, 0x0000, 4)
        print("Program button UP — device should be in ROM boot mode")
    finally:
        write_do(sock, 0x0000, 5)
        sock.close()


if __name__ == "__main__":
    main()
