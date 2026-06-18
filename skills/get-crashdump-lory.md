---
name: get-crashdump-lory
description: SSH to Lory DUT, pull arlod crash dump files (core.gz + metadata .txt) from /data/cores/, save to JIRA or general folder
---

# Get Arlod Crash Dump from Lory DUT

## When to Use

When investigating an arlod crash — after seeing "segfault", "SIGABRT", or "arlod exited" in logs, or when a JIRA ticket references a crash/coredump.

## Background

On Lory, the kernel `core_pattern` is set to `|/bin/core.sh %E`. When arlod crashes:
- `/data/cores/arlod-core.gz` — gzipped ELF core dump (typically 3-5 MB)
- `/data/cores/arlod-core.txt` — metadata snapshot at crash time:
  - Firmware VERSION from `/etc/os-release`
  - dmalloc log (if present)
  - `netstat -ntp` output
  - `ps -Topid,ppid,stat,etime,time,vsz,rss,args` output
  - Last 200 lines of `/var/log/messages`

Other processes that crash will have `<procname>-core.gz` / `<procname>-core.txt`.

The crash dump persists across reboots (stored on `/data` ubifs partition).

## Configuration

DUT settings in `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini`:
- `[ssh]` — host, user, password

## Steps

### 1. Determine destination folder

**If working on a JIRA ticket** (ticket ID known from conversation context):
```
$ARLO_CLAUDE_SETTINGS/JIRA/<TICKET>/crashdump_<YYYYMMDD_HHMMSS>/
```

**If no JIRA context:**
```
$ARLO_CLAUDE_SETTINGS/JIRA/_general/crashdump_<YYYYMMDD_HHMMSS>/
```

Create the destination directory.

### 2. Read DUT config

```bash
DUT_HOST=$(grep -A5 '^\[ssh\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini | grep '^host' | cut -d= -f2 | tr -d ' ')
DUT_USER=$(grep -A5 '^\[ssh\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini | grep '^user' | cut -d= -f2 | tr -d ' ')
DUT_PASS=$(grep -A5 '^\[ssh\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/dut.ini | grep '^password' | cut -d= -f2 | tr -d ' ')
```

### 3. Check DUT reachability (with wake retry)

Same as `getlogs-lory`:
1. `ping -c 1 -W 2 $DUT_HOST`
2. If unreachable, wake via SYNC button (`python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/voodoo_do_pulse.py 0 2`), wait 10s, retry up to 5 times
3. If still unreachable, try serial IP discovery
4. If all fails, report failure and stop

### 4. Check if crash dump exists

```python
ssh.exec_command('ls /data/cores/ 2>&1')
```

If `/data/cores/` is empty or doesn't exist, report:
```
No crash dump found on device. /data/cores/ is empty.
```
And stop (exit 0, not an error).

### 5. Pull crash dump files

Use paramiko (SFTP not available on Lory — use `exec_command` + `cat`):

```python
python3 -c "
import paramiko, os

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

# List all files in /data/cores/
stdin, stdout, stderr = ssh.exec_command('ls /data/cores/ 2>/dev/null')
files = [f.strip() for f in stdout.read().decode().strip().split('\n') if f.strip()]

if not files:
    print('No crash dump files found')
    ssh.close()
    exit(0)

for f in files:
    remote = f'/data/cores/{f}'
    local = os.path.join(dest, f)
    sz = pull_file(remote, local)
    print(f'  {f} ({sz // 1024} KB)')

ssh.close()
print(f'Crash dump saved to: {dest}')
"
```

### 6. Display crash metadata

After pulling files, read and display the `.txt` file:

```bash
cat "$DEST/arlod-core.txt" | head -5   # firmware version + dmalloc header
echo "---"
grep -c "^" "$DEST/arlod-core.txt"     # total lines
```

Report to user:
```
Crash dump pulled:
  arlod-core.gz   (XXXX KB) — core dump
  arlod-core.txt  (XX KB) — metadata

Firmware at crash: <VERSION>
Crash metadata includes: dmalloc log, netstat, ps, last 200 syslog lines

Saved to: <destination_folder>/
```

### 7. (Optional) Ask about cleanup

After successful retrieval, inform the user:
```
Crash dump is still on device at /data/cores/.
Want me to clear it? (Useful to detect new crashes later)
```

If user says yes:
```python
ssh.exec_command('rm -f /data/cores/*')
```

Do NOT clear automatically — always ask.

## Analyzing the Core Dump

The core dump can be analyzed with GDB using the cross-toolchain:

```bash
# Path to cross-gdb (built by Buildroot)
GDB=output/lory-2k/host/bin/arm-linux-gnueabihf-gdb

# Path to unstripped arlod binary
ARLOD=output/lory-2k/build/arlod-*/arlod

# Decompress core
gunzip -k <dest>/arlod-core.gz

# Analyze
$GDB $ARLOD <dest>/arlod-core
# In GDB: bt, info threads, thread apply all bt
```

Note: The toolchain and unstripped binary must match the firmware version that produced the crash.

## Error Handling

| Error | Action |
|-------|--------|
| DUT unreachable | Wake via SYNC × 5, then serial IP discovery |
| SSH auth failure | Check `dut.ini`, device may have prod firmware (different password) |
| /data/cores empty | Report "no crash dump" — not an error |
| File transfer timeout | Retry once (core.gz can be 3-5 MB, takes time over SSH) |
| Multiple crash files | Pull ALL files (may have crashes from different processes) |

## Success Criteria

- All files from `/data/cores/` are copied to destination
- Firmware version at crash time is reported
- Destination path is reported to user
