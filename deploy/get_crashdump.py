#!/usr/bin/env python3
"""Pull arlod crash dump from Lory DUT via SSH.

Reads DUT connection settings from utils/dut.ini.
Saves crash dump files to a specified destination directory.

Usage:
    python3 utils/get_crashdump.py [destination_dir]

If no destination is given, saves to /tmp/crashdump_<timestamp>/
"""

import configparser
import os
import subprocess
import sys
import time

import paramiko

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DUT_INI = os.path.join(SCRIPT_DIR, '..', 'serial_mux', 'dut.ini')
SERIAL_MUX_INI = os.path.join(SCRIPT_DIR, '..', 'serial_mux', 'serial_mux.ini')
CORES_PATH = '/data/cores'
MAX_WAKE_ATTEMPTS = 5

from testbot4.testbot4_do_pulse import Testbot4


def read_config():
    cfg = configparser.ConfigParser()
    cfg.read(DUT_INI)
    return {
        'host': cfg.get('ssh', 'host', fallback='192.168.3.172'),
        'user': cfg.get('ssh', 'user', fallback='root'),
        'password': cfg.get('ssh', 'password', fallback='arlo'),
    }


def ping(host):
    ret = subprocess.run(
        ['ping', '-c', '1', '-W', '2', host],
        capture_output=True
    )
    return ret.returncode == 0


def wake_device():
    print('  Pressing SYNC button...')
    with Testbot4() as vb:
        vb.pulse(0, duration=2.0)
    time.sleep(10)


def discover_ip_via_serial():
    """Try to get DUT IP from serial console if serial_mux is running."""
    client_bin = os.path.join(SCRIPT_DIR, '..', 'serial_mux', 'serial_mux_client')
    if not os.path.isfile(client_bin):
        return None

    import socket
    cfg = configparser.ConfigParser()
    cfg.read(SERIAL_MUX_INI)
    port = cfg.getint('isp', 'tcp_port', fallback=9001)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(('127.0.0.1', port))
        s.close()
    except (ConnectionRefusedError, OSError):
        return None

    result = subprocess.run(
        [client_bin, '--section', 'isp', '--cmd', '\r\nifconfig iot0\r\n', '--timeout', '3000'],
        capture_output=True, text=True
    )
    import re
    m = re.search(r'inet addr:(\S+)', result.stdout)
    return m.group(1) if m else None


def ensure_reachable(host):
    """Ping DUT, wake if needed, discover IP if all else fails."""
    if ping(host):
        return host

    print(f'DUT at {host} unreachable, attempting wake...')
    for attempt in range(1, MAX_WAKE_ATTEMPTS + 1):
        print(f'  Wake attempt {attempt}/{MAX_WAKE_ATTEMPTS}')
        wake_device()
        if ping(host):
            print(f'  DUT responded after {attempt} wake(s)')
            return host

    print(f'DUT still unreachable after {MAX_WAKE_ATTEMPTS} attempts.')
    print('Trying IP discovery via serial...')
    discovered = discover_ip_via_serial()
    if discovered and discovered != host:
        print(f'  Discovered IP: {discovered} (dut.ini has {host})')
        if ping(discovered):
            return discovered

    print(f'ERROR: Cannot reach DUT. Check power, network, and utils/dut.ini')
    sys.exit(1)


def pull_file(ssh, remote_path, local_path):
    stdin, stdout, stderr = ssh.exec_command(f'cat "{remote_path}"')
    data = stdout.read()
    with open(local_path, 'wb') as f:
        f.write(data)
    return len(data)


def main():
    config = read_config()

    if len(sys.argv) > 1:
        dest = sys.argv[1]
    else:
        ts = time.strftime('%Y%m%d_%H%M%S')
        dest = f'/tmp/crashdump_{ts}'

    os.makedirs(dest, exist_ok=True)

    host = ensure_reachable(config['host'])

    print(f'Connecting to {host}...')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=config['user'], password=config['password'], timeout=10)
    except Exception as e:
        print(f'ERROR: SSH connection failed: {e}')
        sys.exit(1)

    stdin, stdout, stderr = ssh.exec_command(f'ls {CORES_PATH}/ 2>/dev/null')
    files = [f.strip() for f in stdout.read().decode().strip().split('\n') if f.strip()]

    if not files:
        print(f'No crash dump found. {CORES_PATH}/ is empty.')
        ssh.close()
        sys.exit(0)

    print(f'Found {len(files)} file(s) in {CORES_PATH}/:')
    for f in files:
        remote = f'{CORES_PATH}/{f}'
        local = os.path.join(dest, f)
        sz = pull_file(ssh, remote, local)
        print(f'  {f} ({sz // 1024} KB)')

    ssh.close()

    print(f'\nCrash dump saved to: {dest}')

    txt_file = os.path.join(dest, 'arlod-core.txt')
    if os.path.isfile(txt_file):
        with open(txt_file, 'r', errors='replace') as f:
            first_line = f.readline().strip()
        print(f'Firmware at crash: {first_line}')


if __name__ == '__main__':
    main()
