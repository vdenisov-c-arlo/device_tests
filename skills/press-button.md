---
name: press-button
description: Press a button on the device via testbot4's digital outputs. Supports Sync, Front, Reset, and Program Mode buttons.
---

# Press Button

## When to Use

When you need to simulate a physical button press on the connected device — to wake from deep sleep, trigger pairing, factory reset, enter programming mode, etc.

## Prerequisites

- Testbot4 reachable at `192.168.7.100` (Modbus TCP, port 502).
- Script: `$ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/testbot4_do_pulse.py`.

## Button Map

Canonical definitions in `utils/custom/device_tests/testbot4/testbot4_channels.py`.

| DO# | Constant | Button | Typical Use |
|-----|----------|--------|-------------|
| 0 | DO_SYNC | Sync Button | Wake from deep sleep, start pairing |
| 1 | DO_FRONT | Front Button | Doorbell ring, user interaction |
| 2 | DO_RESET | Reset Button | Hardware reset (reboot device) |
| 3 | DO_PROGRAM | Program Mode Button | Enter bootloader / DFU mode |
| 6 | DO_USB | USB Plug | Simulate USB charger connect/disconnect (set ON=plugged, OFF=unplugged — NOT pulsed) |
| 7 | DO_PIR | PIR | Simulate PIR motion trigger |

## Steps

### 1. Determine which button and duration

Parse the user's request to identify:
- **Button name** → map to DO number (see table above)
- **Duration** → default 1 second for a normal press. Use longer for wake:
  - Wake from sleep: 2s (Sync)
  - PIR trigger: 3s
  - All other buttons: 1s

If the user says "press sync" or "wake the device", use DO0.
If the user says "press front button" or "ring the doorbell", use DO1.
If the user says "reset" or "hw reset", use DO2.
If the user says "program mode" or "DFU", use DO3.
If the user says "plug USB" or "connect charger" or "USB plug", set DO6 ON (and leave it on).
If the user says "unplug USB" or "disconnect charger" or "USB unplug", set DO6 OFF.
If the user says "PIR" or "trigger motion" or "simulate motion", use DO7.

### 2. Execute the pulse with verification (REQUIRED for Sync button)

For the Sync button (DO0), you MUST verify the press was received by checking the MCU serial log for a BUTTON event. The device may not register the press if it's in deep sleep or transitioning states.

**Procedure:**

1. Note the current MCU log line count: `wc -l /tmp/serial_logs/mcu_*.log`
2. Send a short initial pulse (0.5s): `python3 utils/custom/device_tests/testbot4_do_pulse.py 0 0.5`
3. Wait 500ms, then check new MCU log lines for `[BUTTON]` or `pegaERPC_NotifyButtonState`:
   ```bash
   tail -n +<prev_count> /tmp/serial_logs/mcu_<latest>.log | grep -a "BUTTON"
   ```
4. **If no BUTTON event appears:** release (already released), wait 500ms, and retry.
5. **Repeat up to 5 attempts.** If all 5 fail, escalate to the user — the device may be unresponsive or disconnected.
6. **Once a BUTTON event is confirmed:** if a longer hold is needed (e.g., 15s for factory reset), immediately send the full-duration pulse: `python3 utils/custom/device_tests/testbot4_do_pulse.py 0 <DURATION>`

For non-Sync buttons (DO1-DO3), verification is optional — just execute the pulse directly.

**Script location:** `utils/custom/device_tests/testbot4_do_pulse.py` (relative to cambuildroot root)

**Pulse mode** (turn on, wait, turn off):
```bash
python3 utils/custom/device_tests/testbot4_do_pulse.py 0 2    # Sync button, 2 seconds (wake)
python3 utils/custom/device_tests/testbot4_do_pulse.py 1 1    # Front button, 1 second (ring)
python3 utils/custom/device_tests/testbot4_do_pulse.py 2 1    # Reset button, 1 second (HW reset)
python3 utils/custom/device_tests/testbot4_do_pulse.py 3 1    # Program mode button, 1 second
python3 utils/custom/device_tests/testbot4_do_pulse.py 7 3    # PIR trigger, 3 seconds
```

**Set on/off indefinitely** (for USB plug, or holding a button):
```bash
python3 utils/custom/device_tests/testbot4_do_pulse.py --on 6     # USB plugged (DO6 ON, stays on)
python3 utils/custom/device_tests/testbot4_do_pulse.py --off 6    # USB unplugged (DO6 OFF)
python3 utils/custom/device_tests/testbot4_do_pulse.py --on 0     # Hold sync button indefinitely
python3 utils/custom/device_tests/testbot4_do_pulse.py --off 0    # Release sync button
```

**Read current state:**
```bash
python3 utils/custom/device_tests/testbot4_do_pulse.py --read     # Shows which DOs are currently ON
```

Note: `--on`/`--off` use read-modify-write so they don't disturb other active DOs.

### 3. Report result

Confirm which button was pressed and for how long. If the script errors (e.g., testbot4 unreachable), report the connection failure.

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `Connection refused` | Testbot4 not powered or wrong IP | Check power and network to 192.168.7.100 |
| `timed out` | Network path blocked | Check that host can reach 192.168.7.100 |
| `Modbus exception` | Invalid DO register or board firmware issue | Verify board is a testbot4 with DO support |

## Arguments

The skill accepts an optional argument string: `<button_name_or_number> [duration]`

Examples:
- `/press-button sync` → DO0, 1s
- `/press-button sync 2` → DO0, 2s
- `/press-button front` → DO1, 1s
- `/press-button 0 3` → DO0, 3s
- `/press-button reset` → DO2, 1s
- `/press-button program` → DO3, 1s
- `/press-button usb on` → DO6 ON (charger plugged, stays on)
- `/press-button usb off` → DO6 OFF (charger unplugged)
- `/press-button pir` → DO7, 3s
- `/press-button read` → read and display current DO state

If no arguments provided, ask the user which button to press.
