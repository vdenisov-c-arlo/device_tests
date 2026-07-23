---
name: flash-lory
description: Flash Lory firmware via UART. Wakes device via testbot4 SYNC button, logs in to ISP console, waits for network, and issues fwupgrade.
---

# Flash Lory

## When to Use

After a successful `make lory-2k` or `make lory-2k-refresh` build, to deploy the new firmware to the connected Lory device.

## Prerequisites

- A successful build with an `.enc` file in `output/lory-2k/images/`.
- Testbot4 reachable at **192.168.7.100** (remote, not local USB).
- **serial_mux runs on the testbot4** — ISP console at `192.168.7.100:9001`, MCU console at `192.168.7.100:9002`. It is always running on the testbot4; do NOT check localhost.

## Steps

### 1. Check serial_mux is reachable (REQUIRED)

serial_mux runs on the remote testbot4, not locally:

```bash
nc -z -w2 192.168.7.100 9001 && echo "OK" || echo "NOT reachable"
```

**If not reachable**, the testbot4 may be down or network is broken. Ask the user to check.

### 2. Deploy binary to testbot4

The device fetches firmware from the testbot4's HTTP server (192.168.7.100 from device's perspective = 192.168.7.100 from host). Copy the built `.enc` file there:

```bash
ENC=$(ls -t output/lory-2k/images/deploy/binaries/*.enc | head -1)
scp "$ENC" 192.168.7.100:/var/www/lory-2k/bin/
```

The fwupgrade URL is: `http://192.168.7.100/lory-2k/bin/$(basename $ENC)`

### 3. Run the flash script

```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/flash_lory.py "http://192.168.7.100/lory-2k/bin/$(basename $ENC)"
```

The script connects to `192.168.7.100:9001` (serial_mux on testbot4) and handles:
- Waking the device (3x SYNC via testbot4)
- Login (root/arlo)
- Waiting for network (iot0)
- Pinging the update server
- Issuing fwupgrade
- Monitoring progress until reboot
- Verifying firmware version after reboot

**IMPORTANT:** Always pass the explicit URL. Do NOT rely on `grep "fwupgrade" br.log` — it may be stale.

Use `timeout: 600000` (10 minutes) in the Bash tool.

### 3. Report result

The script exits 0 on success, non-zero on failure. Report the confirmed firmware version.

## Error Handling

| Error | Fix |
|-------|-----|
| serial_mux not reachable | Testbot4 at 192.168.7.100 may be down — ask user to check power/network |
| Testbot4 unreachable | Check network to 192.168.7.100, verify board is powered |
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
