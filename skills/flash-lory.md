---
name: flash-lory
description: Flash Lory firmware via UART. Wakes device via voodoo board SYNC button, logs in to ISP console, waits for network, and issues fwupgrade.
---

# Flash Lory

## When to Use

After a successful `make lory-2k` or `make lory-2k-refresh` build, to deploy the new firmware to the connected Lory device.

## Prerequisites

- A successful build with an `.enc` file in `output/lory-2k/images/`.
- Voodoo board reachable (address configured in `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux.ini` `[voodoo]` section).
- **serial_mux must be running** on the ISP port (configured in `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux.ini` `[isp]` section). The flash script connects via TCP.

## Steps

### 1. Check serial_mux is running (REQUIRED)

Read the ISP TCP port from `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux.ini` `[isp]` `tcp_port` (default 9001):

```bash
ISP_PORT=$(grep -A5 '^\[isp\]' $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux.ini | grep tcp_port | cut -d= -f2 | tr -d ' ')
nc -z localhost ${ISP_PORT:-9001} 2>/dev/null && echo "OK" || echo "NOT running"
```

**If not running**, do NOT proceed. Ask the user:

```
serial_mux is not running. Please start it:

  $ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_terminals.sh

Then try /flash-lory again.
```

### 2. Run the flash script

```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/flash_lory.py [optional_fwupgrade_url]
```

The script connects to `localhost:9001` (serial_mux) and handles:
- Waking the device (3x SYNC via voodoo board)
- Login (root/arlo)
- Waiting for network (iot0)
- Pinging the update server
- Issuing fwupgrade
- Monitoring progress until reboot
- Verifying firmware version after reboot

If no URL argument is given, it finds the latest `.enc` in `output/lory-2k/images/`.

Use `timeout: 600000` (10 minutes) in the Bash tool.

### 3. Report result

The script exits 0 on success, non-zero on failure. Report the confirmed firmware version.

## Error Handling

| Error | Fix |
|-------|-----|
| serial_mux not running | Ask user to run `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_terminals.sh` |
| Voodoo board unreachable | Check network to voodoo host (see `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux.ini` `[voodoo]`), verify board is powered |
| No login prompt after wake | Device may need longer wake time, retry SYNC press |
| iot0 never comes up | WiFi may not be configured, check if device is claimed |
| Ping to server fails | Check host HTTP server is running, firewall rules |
| Download stalls | Check HTTP server, image file exists at URL |
| Write fails | Flash may be corrupted — device may need recovery |
| No reboot after write | fwupgrade may have failed silently — check output |
| Wrong version after reboot | OTA applied wrong partition — check dual-bank config |

## Success Criteria

- Device wakes from sleep.
- Login to ISP console succeeds.
- Network (iot0) comes up with an IP.
- fwupgrade command starts downloading.
- Download, write, and reboot stages complete.
- After reboot, os-release VERSION matches the build.
