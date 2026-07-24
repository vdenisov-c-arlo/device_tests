---
name: ble-onboard
description: BLE onboard a fresh/factory-reset Arlo DUT. Runs wake, BLE scan, key exchange, WiFi provisioning, and cloud claim.
---

# BLE Onboard

## When to Use

After a factory reset or fresh flash, to onboard the DUT via BLE — wake it from shipping mode, provision WiFi credentials, and claim it on the Arlo cloud.

## Prerequisites

- Device in shipping mode or BLE-advertising state (after factory reset or fresh flash)
- Bluetooth adapter UP on the host running the script (`hciconfig hci0` should show UP RUNNING)
- serial_mux running on testbot4 — ISP at `192.168.7.100:9001` (used for FOTA disable step)
- Testbot4 reachable at 192.168.7.100 (SYNC button DO0 used for wake)
- Python packages: `bleak`, `cryptography`, `playwright` (with chromium installed)
- Config file: `$ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/onboard/ble_onboard.ini`

## Arguments

- Optional: step names to run individually (e.g., `scan`, `wifi connect`, `all`)
- Optional: `--ssid <SSID>` and `--psk <passphrase>` to override WiFi config
- If no arguments, runs all steps sequentially

## Steps

### 1. Check prerequisites

```bash
hciconfig hci0 | grep -q "UP RUNNING" && echo "BT OK" || echo "BT DOWN — run: sudo hciconfig hci0 up"
nc -z -w2 192.168.7.100 9001 && echo "serial_mux OK" || echo "serial_mux NOT reachable"
python3 -c "import bleak, cryptography; print('deps OK')"
```

### 2. Run the onboarding script

Full onboarding (all steps):
```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/onboard/ble_onboard.py all
```

Individual steps (for debugging or demo):
```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/onboard/ble_onboard.py wake
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/onboard/ble_onboard.py scan
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/onboard/ble_onboard.py pubkey exchange mode wifi connect discovery claim wait
```

Skip wake (device already in BLE mode after factory reset):
```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/onboard/ble_onboard.py --no-wake all
```

Use `timeout: 300000` (5 minutes) in the Bash tool.

### 3. Report result

The script prints step-by-step progress. Final success = device claimed and confirmed via BLE `DEVICE_CLAIMED` status.

## Onboarding Flow (what happens under the hood)

1. **wake** — 10s long-press SYNC + 3x 3s short presses via testbot4 DO0 (exits shipping mode)
2. **fota** — Disables FOTA URL via serial_mux ISP console (prevents update during onboarding)
3. **scan** — BLE scan for device name (e.g., `AVD6001`), connect, read cert_id + FW version
4. **pubkey** — Fetch device's EC P-256 public key from Arlo cloud using cert_id
5. **exchange** — Write our public key to device via BLE, compute ECDH shared secret
6. **mode** — Set onboarding mode to D2AP_WIFI
7. **wifi** — Encrypt and write WiFi SSID + passphrase via BLE
8. **connect** — Send PAIR command, poll BLE status until WiFi connected (PAIRED)
9. **discovery** — Send encrypted discovery token, wait for cloud registration
10. **claim** — Locate device in Arlo cloud, claim to account
11. **wait** — Poll BLE for DEVICE_CLAIMED confirmation

State is persisted to `.ble_onboard_state.json` between steps, so you can resume after failure.

## Configuration

Config file: `utils/custom/device_tests/onboard/ble_onboard.ini`

Key settings:
- `[arlo_cloud]` — email, password, environment (goldendev)
- `[wifi]` — SSID, PSK, band, security
- `[device]` — BLE advertising name (AVD6001 for Lory 2K)
- `[serial]` — serial_mux host/port for FOTA disable
- `[options]` — skip_fota_disable, skip_discovery, timeout

## Error Handling

| Error | Fix |
|-------|-----|
| BT adapter DOWN | `sudo hciconfig hci0 up` |
| BLE scan finds nothing | Device may not be in advertising mode — retry wake or factory reset |
| Key exchange fails | BLE connection may have dropped — clear state (`--clear-state`) and re-scan |
| WiFi connect timeout | Check SSID/PSK in config, check testbot4 hostapd is running |
| Cloud claim fails | Check account credentials, verify goldendev environment is up |
| Playwright login error | `playwright install chromium`, check credentials |

## Success Criteria

- All steps complete without error
- Device transitions through: BLE connected → WiFi paired → Cloud registered → Claimed
- Final BLE status reads DEVICE_CLAIMED
