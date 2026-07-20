#!/usr/bin/env python3
"""Pull logs from Lory DUT via SSH.

Reads DUT connection settings from utils/dut.ini.
Copies /var/log/messages and any rotated archives to a destination directory.

Usage:
    python3 utils/get_logs.py [destination_dir]

If no destination is given, saves to /tmp/logs_<timestamp>/
"""

import configparser
import os
import subprocess
import sys
import time

import paramiko

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DUT_INI = os.path.join(SCRIPT_DIR, 'serial_mux', 'dut.ini')
SERIAL_MUX_INI = os.path.join(SCRIPT_DIR, 'serial_mux', 'serial_mux.ini')
VOODOO_SCRIPT = os.path.join(SCRIPT_DIR, 'voodoo_do_pulse.py')
MAX_WAKE_ATTEMPTS = 5


def read_config():
    cfg = configparser.ConfigParser()
    cfg.read(DUT_INI)
    return {
        'host': cfg.get('ssh', 'host', fallback='192.168.3.172'),
        'user': cfg.get('ssh', 'user', fallback='root'),
        'password': cfg.get('ssh', 'password', fallback='arlo'),
        'messages': cfg.get('logs', 'messages', fallback='/var/log/messages'),
        'archive_dir': cfg.get('logs', 'archive_dir', fallback='/var/log'),
        'archive_pattern': cfg.get('logs', 'archive_pattern', fallback='messages.*'),
    }


def ping(host):
    ret = subprocess.run(
        ['ping', '-c', '1', '-W', '2', host],
        capture_output=True
    )
    return ret.returncode == 0


def wake_device():
    print('  Pressing SYNC button...')
    subprocess.run(
        ['python3', VOODOO_SCRIPT, '0', '2'],
        capture_output=True
    )
    time.sleep(10)


def discover_ip_via_serial():
    """Try to get DUT IP from serial console if serial_mux is running."""
    client_bin = os.path.join(SCRIPT_DIR, 'serial_mux', 'serial_mux_client')
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

    import re
    result = subprocess.run(
        [client_bin, '--section', 'isp', '--cmd', '\r\nifconfig iot0\r\n', '--timeout', '3000'],
        capture_output=True, text=True
    )
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
        dest = f'/tmp/logs_{ts}'

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

    total_files = 0
    total_bytes = 0

    # Pull current messages
    messages_path = config['messages']
    local_messages = os.path.join(dest, 'messages')
    sz = pull_file(ssh, messages_path, local_messages)
    print(f'  messages ({sz // 1024} KB)')
    total_files += 1
    total_bytes += sz

    # Pull archived logs
    archive_dir = config['archive_dir']
    pattern = config['archive_pattern']
    stdin, stdout, stderr = ssh.exec_command(f'ls {archive_dir}/{pattern} 2>/dev/null')
    archives = [f.strip() for f in stdout.read().decode().strip().split('\n') if f.strip()]

    for remote in archives:
        basename = os.path.basename(remote)
        local = os.path.join(dest, basename)
        sz = pull_file(ssh, remote, local)
        print(f'  {basename} ({sz // 1024} KB)')
        total_files += 1
        total_bytes += sz

    ssh.close()

    print(f'\nLogs saved to: {dest}')
    print(f'Total: {total_files} file(s), {total_bytes // 1024} KB')

    # Quick health check
    if os.path.isfile(local_messages) and os.path.getsize(local_messages) > 0:
        print('\nLast 5 lines:')
        with open(local_messages, 'r', errors='replace') as f:
            lines = f.readlines()
            for line in lines[-5:]:
                print(f'  {line.rstrip()}')


if __name__ == '__main__':
    main()
