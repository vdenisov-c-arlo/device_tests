---
name: usb-crash-test
description: Run the eRPC USB unplug crash test (PEGA-1695). Plugs/unplugs USB during active eRPC traffic, monitors for MCU coredump.
---

# USB Crash Test

## When to Use

To reproduce PEGA-1695 — MCU coredump in `xagent_erpc_client_request_complete()` during USB unplug while eRPC traffic is active.

## Prerequisites

- Device claimed, in always-on mode (USB plugged, ISP active, xagent running)
- serial_mux running on testbot4 — ISP at `192.168.7.100:9001`, MCU at `192.168.7.100:9002`
- Testbot4 reachable at 192.168.7.100 (DO6 = USB VBUS relay)

## Arguments

- Optional: cycle count (default 10 for demo, use 200 for overnight)
- Optional: `--no-isp` to skip ISP console traffic generation (unplug-only mode)

## Steps

### 1. Check serial_mux is reachable

```bash
nc -z -w2 192.168.7.100 9001 && nc -z -w2 192.168.7.100 9002 && echo "OK" || echo "NOT reachable"
```

### 2. Run the test

```bash
python3 $ARLO_CLAUDE_SETTINGS/utils/custom/device_tests/tests/erpc_usb_unplug_crash_test.py -n <cycles>
```

Default to `-n 10` for a demo run. Use `-n 200` for real bug hunting.

Use `timeout: 600000` (10 minutes) in the Bash tool for demo runs, `timeout: 1800000` (30 minutes) for long runs.

### 3. Report result

The script prints a summary at the end:
- Total cycles, passes, crashes detected
- Crash dump files saved to `/tmp/erpc_usb_crash_logs/`

Report the pass/crash ratio and whether any coredumps were captured.

## What It Does (Per Cycle)

1. Ensures USB is plugged (DO6 ON) — ISP powers up
2. Waits for ISP ready (xagent running, eRPC established)
3. Generates eRPC traffic via ISP console (`arlocmd` diagnostics query)
4. Immediately unplugs USB (DO6 OFF) — triggers ISP sleep vote + eRPC teardown race
5. Monitors MCU console for crash/coredump signatures
6. Waits for device to recover (re-plug USB, wait for ISP ready)
7. Reports PASS or CRASH for the cycle

## Error Handling

| Error | Fix |
|-------|-----|
| serial_mux not reachable | Testbot4 may be down — ask user to check |
| Device never reaches ISP ready | May not be in always-on mode — check battery/USB |
| No recovery after crash | Script auto-resets via RESET button (DO2), wait 60s |
| Test hangs | Kill and re-run — likely socket stale state |

## Success Criteria

- PASS: All cycles complete without coredump detection
- FAIL: One or more coredumps captured (expected at ~1% rate with the bug present)
