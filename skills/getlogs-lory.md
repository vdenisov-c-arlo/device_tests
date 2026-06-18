---
name: getlogs-lory
description: SSH to DUT, copy all log archives and current /var/log/messages, save to JIRA folder or general logs folder
---

# Get Logs from Lory DUT

## When to Use

When you need device logs for analysis — during bug investigation, after a crash, after a test, or when asked to "get logs" / "grab logs" / "pull logs from device."

## Configuration

All DUT settings are in `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini`:
- `[ssh]` — host IP, user, password
- `[logs]` — paths to log files and archives on device
- `[serial]` — fallback IP discovery command

## Steps

### 1. Determine destination folder

**If working on a JIRA ticket** (ticket ID known from conversation context):
```
$ARLO_CLAUDE_SETTINGS/JIRA/<TICKET>/logs_<YYYYMMDD_HHMMSS>/
```

**If no JIRA context:**
```
$ARLO_CLAUDE_SETTINGS/JIRA/_general/logs_<YYYYMMDD_HHMMSS>/
```

Create the destination directory.

### 2. Read DUT config

```bash
DUT_HOST=$(grep -A5 '^\[ssh\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini | grep '^host' | cut -d= -f2 | tr -d ' ')
DUT_USER=$(grep -A5 '^\[ssh\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini | grep '^user' | cut -d= -f2 | tr -d ' ')
DUT_PASS=$(grep -A5 '^\[ssh\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini | grep '^password' | cut -d= -f2 | tr -d ' ')
```

### 3. Check DUT reachability (with wake retry)

```bash
ping -c 1 -W 2 $DUT_HOST >/dev/null 2>&1 && echo "DUT reachable" || echo "DUT unreachable"
```

**If unreachable**, attempt to wake the device:

1. Press SYNC button via voodoo board: `python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/voodoo_do_pulse.py 0 2`
2. Wait 10 seconds
3. Retry ping
4. Repeat up to **5 times**

If still unreachable after 5 attempts, **stop and report failure**:
```
ERROR: DUT at <IP> is not reachable after 5 wake attempts.
Possible causes:
  - Device is not powered
  - WiFi not configured/connected
  - IP address has changed — check $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini [ssh] host

To discover current IP via serial:
  ./serial_mux_client --section isp --cmd "ifconfig iot0\r\n" --timeout 3000
```

### 4. Try IP discovery via serial (if ping fails and serial_mux is running)

Before giving up, if serial_mux is running on the ISP port, try to discover the IP:

```bash
nc -z localhost 9001 2>/dev/null && \
  $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux_client --section isp --cmd $'\r\nifconfig iot0\r\n' --timeout 3000 2>/dev/null | \
  grep 'inet addr' | sed 's/.*inet addr:\([^ ]*\).*/\1/'
```

If a valid IP is returned and it differs from `dut.ini`, use the discovered IP for this session and inform the user:
```
DUT IP discovered via serial: <new_ip> (dut.ini has <old_ip>)
Consider updating $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini if this is permanent.
```

### 5. Copy logs from device

**Note:** Lory does not have SFTP. Use paramiko `exec_command` with `cat` to pull files.

```python
python3 -c "
import paramiko, os, sys

host = '<DUT_HOST>'
user = '<DUT_USER>'
password = '<DUT_PASS>'
dest = '<DEST>'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=password, timeout=10)

def pull_file(remote_path, local_path):
    stdin, stdout, stderr = ssh.exec_command(f'cat \"{remote_path}\"')
    data = stdout.read()
    with open(local_path, 'wb') as f:
        f.write(data)
    return len(data)

# Copy current messages
sz = pull_file('/var/log/messages', os.path.join(dest, 'messages'))
print(f'  messages ({sz // 1024} KB)')

# List and copy archives
stdin, stdout, stderr = ssh.exec_command('ls /var/log/messages.* 2>/dev/null')
archives = stdout.read().decode().strip().split('\n')
for f in archives:
    f = f.strip()
    if not f:
        continue
    basename = os.path.basename(f)
    sz = pull_file(f, os.path.join(dest, basename))
    print(f'  {basename} ({sz // 1024} KB)')

ssh.close()
print(f'All logs saved to: {dest}')
"
```

If `sshpass` is available, you can alternatively use:
```bash
SSH_CMD="sshpass -p $DUT_PASS ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $DUT_USER@$DUT_HOST"
$SSH_CMD "cat /var/log/messages" > "$DEST/messages"
$SSH_CMD "ls /var/log/messages.* 2>/dev/null" | while read f; do
    $SSH_CMD "cat $f" > "$DEST/$(basename $f)"
done
```

### 6. Report results

After copying, report:
```
Logs saved to: <destination_folder>/
  messages         (XX KB) — current log
  messages.0.gz    (XX KB) — rotated archive
  messages.1.gz    (XX KB) — rotated archive
  ...

Total: N files, XX KB
```

Also show the last 5 lines of `messages` as a quick health check:
```bash
tail -5 "$DEST/messages"
```

## Error Handling

| Error | Action |
|-------|--------|
| DUT unreachable (ping fails) | Wake via SYNC × 5, then try serial IP discovery |
| SSH auth failure | Check `dut.ini` password, device may have prod firmware |
| SCP timeout | Device might be entering sleep — retry once |
| No log files on device | Report empty `/var/log/` — device may have just been factory reset |
| Serial mux not running (for IP discovery) | Skip serial discovery, report ping failure |

## Success Criteria

- At least `/var/log/messages` is copied to the destination
- File is non-empty
- Destination path is reported to the user
