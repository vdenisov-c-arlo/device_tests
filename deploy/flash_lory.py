#!/usr/bin/env python3
"""Flash Lory firmware via serial_mux TCP connection.

Requires serial_terminals.sh to be running (serial_mux on port 9001).

Wakes the device via voodoo board SYNC button, logs in to ISP console,
waits for network, issues fwupgrade, monitors progress, and verifies.

Usage:
    python3 $ARLO_CLAUDE_SETTINGS/utils/custom/flash_lory.py [fwupgrade_url]

If no URL is given, finds the latest .enc in output/lory-2k/images/.
"""

import configparser
import os
import re
import subprocess
import sys
import time
import socket as sock_mod

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INI_PATH = os.path.join(SCRIPT_DIR, '..', 'serial_mux', 'serial_mux.ini')

_cfg = configparser.ConfigParser()
_cfg.read(INI_PATH)

ISP_TCP_HOST = _cfg.get('isp', 'tcp_host', fallback='127.0.0.1')
ISP_TCP_PORT = _cfg.getint('isp', 'tcp_port', fallback=9001)
SERVER_IP = _cfg.get('server', 'host_ip', fallback='192.168.100.75')

VOODOO_SCRIPT = os.path.join(SCRIPT_DIR, '..', 'voodoo', 'voodoo_do_pulse.py')
IMAGES_DIR = os.path.join(os.getcwd(), 'output', 'lory-2k', 'images')


class SocketSerial:
    """Adapter that talks through serial_mux TCP socket,
    mimicking the pyserial interface used by flash_lory."""

    def __init__(self, host, port):
        self.sock = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
        self.sock.connect((host, port))
        self.sock.setblocking(False)
        self._buf = b''

    @property
    def in_waiting(self):
        try:
            data = self.sock.recv(4096)
            if data:
                self._buf += data
        except BlockingIOError:
            pass
        return len(self._buf)

    def read(self, size=1):
        # Drain from socket into buffer
        try:
            while True:
                data = self.sock.recv(4096)
                if data:
                    self._buf += data
                else:
                    break
        except BlockingIOError:
            pass
        result = self._buf[:size]
        self._buf = self._buf[size:]
        return result

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.sock.sendall(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    break
        except BlockingIOError:
            pass
        self._buf = b''

    def close(self):
        self.sock.close()


def open_connection():
    """Connect to serial_mux TCP server."""
    try:
        s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
        s.settimeout(2)
        s.connect((ISP_TCP_HOST, ISP_TCP_PORT))
        s.close()
    except (ConnectionRefusedError, OSError):
        print(f'ERROR: serial_mux not running on {ISP_TCP_HOST}:{ISP_TCP_PORT}')
        print(f'Please run: {os.path.join(SCRIPT_DIR, "..", "serial_mux", "serial_terminals.sh")}')
        sys.exit(1)

    try:
        conn = SocketSerial(ISP_TCP_HOST, ISP_TCP_PORT)
        print(f'Connected to serial_mux ({ISP_TCP_HOST}:{ISP_TCP_PORT})')
        return conn, 'socket'
    except Exception as e:
        print(f'ERROR: Connect failed: {e}')
        sys.exit(1)


def send(ser, cmd):
    ser.write((cmd + '\n').encode())
    ser.flush()


def read_until(ser, pattern, timeout=30):
    start = time.time()
    buf = b''
    while time.time() - start < timeout:
        n = ser.in_waiting
        if n:
            data = ser.read(n)
        else:
            data = ser.read(1)
        if data:
            buf += data
            text = buf.decode('utf-8', errors='replace')
            if re.search(pattern, text):
                return text
        else:
            time.sleep(0.05)
    return buf.decode('utf-8', errors='replace')


def find_latest_enc():
    """Find the most recent .enc file in the build output directory."""
    import glob
    pattern = os.path.join(IMAGES_DIR, 'AVD6001-*.enc')
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def get_fwupgrade_url():
    if len(sys.argv) > 1:
        url = sys.argv[1]
        if not url.startswith('fwupgrade'):
            url = 'fwupgrade ' + url
        return url

    enc_file = find_latest_enc()
    if not enc_file:
        print(f'ERROR: No .enc file found in {IMAGES_DIR}')
        sys.exit(1)

    filename = os.path.basename(enc_file)
    url = f'fwupgrade http://{SERVER_IP}/lory-2k/bin/{filename}'
    return url


def wake_device():
    print('=== Waking device (3x SYNC button press) ===')
    for i in range(3):
        result = subprocess.run(
            ['python3', VOODOO_SCRIPT, '0', '1'],
            capture_output=True, text=True
        )
        print(f'  press {i+1}: {result.stdout.strip()}')
        if result.returncode != 0:
            print(f'ERROR: voodoo pulse failed: {result.stderr}')
            sys.exit(1)
        time.sleep(1)
    time.sleep(5)


def login(ser):
    send(ser, '')
    time.sleep(0.5)
    send(ser, '')
    output = read_until(ser, r'login:|#', timeout=5)

    if not re.search(r'login:|#', output):
        wake_device()
        time.sleep(3)
        send(ser, '')
        output = read_until(ser, r'login:|#', timeout=15)

    if 'login:' in output:
        print('=== Logging in ===')
        send(ser, 'root')
        output = read_until(ser, r'Password:|#', timeout=5)
        if 'Password:' in output:
            send(ser, 'arlo')
            output = read_until(ser, r'#', timeout=5)

    if '#' not in output:
        send(ser, 'echo OK')
        output = read_until(ser, r'OK|#', timeout=5)
        if 'OK' not in output and '#' not in output:
            print('ERROR: Could not get shell prompt')
            print(f'[UART] {output[-300:]}')
            ser.close()
            sys.exit(1)

    return ser


def wait_for_network(ser):
    print('=== Waiting for network (iot0) ===')
    for attempt in range(10):
        send(ser, 'ifconfig iot0 2>&1')
        output = read_until(ser, r'inet addr:|No such device|Device not found|#', timeout=5)
        if 'inet addr:' in output:
            ip_match = re.search(r'inet addr:(\S+)', output)
            if ip_match:
                print(f'iot0 IP: {ip_match.group(1)}')
                return True
        if 'No such device' in output or 'Device not found' in output:
            if attempt >= 3:
                print('ERROR: iot0 interface does not exist')
                return False
        time.sleep(3)
    print('ERROR: iot0 did not get an IP within 30s')
    return False


def ping_server(ser):
    print(f'=== Pinging update server {SERVER_IP} ===')
    send(ser, f'ping -c 1 -W 3 {SERVER_IP}')
    output = read_until(ser, r'bytes from|100% packet loss|#', timeout=10)
    if 'bytes from' in output:
        print('Server reachable')
        return True
    print('WARNING: ping may have failed, trying fwupgrade anyway')
    return False


FLASH_FAIL_PATTERNS = [
    r'Failure while performing upgrade',
    r'Install \S+ failed',
]


def flash(ser, fwupgrade_cmd):
    print(f'=== Flashing: {fwupgrade_cmd} ===')
    send(ser, fwupgrade_cmd)

    print('=== Monitoring upgrade progress ===')
    fail_re = '|'.join(FLASH_FAIL_PATTERNS)
    combined_pattern = rf'restarting system|Rebooting|reboot|{fail_re}'
    output = read_until(ser, combined_pattern, timeout=240)
    lines = output.split('\n')
    for line in lines[-20:]:
        if line.strip():
            print(f'  {line.strip()}')

    for pat in FLASH_FAIL_PATTERNS:
        if re.search(pat, output):
            print(f'ERROR: Flash failed — matched: {pat}')
            print(f'[UART last 500 chars] {output[-500:]}')
            return False

    if not re.search(r'restarting system|Rebooting|reboot', output):
        print('ERROR: Did not detect reboot within timeout')
        print(f'[UART last 500 chars] {output[-500:]}')
        return False
    return True


def verify(ser, expected_version):
    print('=== Reboot detected, waiting for device to come back ===')
    output = read_until(ser, r'login:', timeout=90)
    print('Device booted, logging in...')
    send(ser, 'root')
    output = read_until(ser, r'Password:', timeout=5)
    send(ser, 'arlo')
    output = read_until(ser, r'#', timeout=5)

    print('=== Verifying firmware version ===')
    send(ser, 'cat /etc/os-release')
    output = read_until(ser, r'#', timeout=5)

    version_match = re.search(r'VERSION=(\S+)', output)
    if version_match:
        version = version_match.group(1)
        print(f'\n=== SUCCESS: Firmware version {version} ===')
        if expected_version in version:
            print('Version matches build!')
            return True
        else:
            print(f'WARNING: Expected {expected_version}, got {version}')
            return False
    else:
        print('Could not extract version. Raw output:')
        print(output[-300:])
        return False


def main():
    fwupgrade_cmd = get_fwupgrade_url()
    version_match = re.search(r'AVD6001-(\S+)\.enc', fwupgrade_cmd)
    expected_version = version_match.group(1) if version_match else ''

    print(f'Target: {fwupgrade_cmd}')
    print(f'Expected version: {expected_version}')

    print('=== Connecting to ISP UART ===')
    ser, mode = open_connection()

    ser = login(ser)

    if not wait_for_network(ser):
        ser.close()
        sys.exit(1)

    ping_server(ser)

    if not flash(ser, fwupgrade_cmd):
        ser.close()
        sys.exit(1)

    success = verify(ser, expected_version)
    ser.close()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
