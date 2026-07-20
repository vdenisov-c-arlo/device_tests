# USB Queue Overflow Stress Test — 5-Cycle Report

**Date:** 2026-06-23
**Device:** Lory (AVD6001 / C521)
**Firmware:** 0.100.502_2fe7e42
**Test script:** `utils/custom/device_tests/usb_queue_overflow_test.py`

## Summary

Rapid USB plug/unplug via voodoo board DO6 reliably triggers MCU main task queue overflow (100% reproduction rate). The device always self-recovers within 2 seconds — no reset required, no crash, no hang.

## Test Parameters

| Parameter | Value |
|-----------|-------|
| Cycles | 5 |
| Bursts per cycle | 30 |
| Toggle interval | 50ms |
| Recovery observation | 60s |
| Cooldown between cycles | 10s |
| Burst execution | Voodoo board local Modbus (~3ms RTT) |
| Total burst duration | ~1.5s |

## Results

| Cycle | Result | Overflow Errors | Recovery Time |
|-------|--------|----------------|---------------|
| 1 | OVERFLOW_RECOVERED | 39 | ~2s |
| 2 | OVERFLOW_RECOVERED | 42 | ~2s |
| 3 | OVERFLOW_RECOVERED | 42 | ~2s |
| 4 | OVERFLOW_RECOVERED | 42 | ~2s |
| 5 | OVERFLOW_RECOVERED | 42 | ~2s |

```
RESULTS: 5 cycles run
  PASS:               0
  OVERFLOW+RECOVERED: 5
  OVERFLOW+STUCK:     0
  CRASH:              0
  UNRESPONSIVE:       0

  Overflow triggered 5x — device always self-recovered
  Exit code: 0
```

## Observations

- **100% reproducible** — every cycle triggers overflow on the first burst
- **36–42 errors per burst**, all within the first second
- **Zero errors after burst completes** — no cascading failures
- **Recovery in ~2 seconds** — eRPC reconnects, vote_action fires, normal sleep/wake resumes
- **No crash, no hang, no stuck state** across all 5 cycles
- **MCU console responsive throughout** — never needed a reset

## Error Pattern

```
[ERROR]mcu:pega_main.c:pegaMain_TaskSet()  Pega_Main xQueueSend fail , eMainTask= 13, result =101580801
[ERROR]mcu:pega_main.c:pegaMain_TaskSet()  Pega_Main xQueueSend fail , eMainTask= 12, result =101580801
```

- `eMainTask= 12` — SBU_Remove events
- `eMainTask= 13` — SBU_Insert events
- `result = 101580801` — FreeRTOS `errQUEUE_FULL` return from `xQueueSend()`
- Events alternate (12, 13, 12, 13...) matching the rapid plug/unplug pattern

## Root Cause

`pegaMain_TaskSet()` posts events to the FreeRTOS main task queue with `xQueueSend()` using zero timeout. When SBU interrupts arrive at ~20Hz (50ms interval) and the main task is busy processing prior events, the queue fills and subsequent events are silently dropped.

**Contributing factors:**
- No debouncing on SBU (USB detect) GPIO interrupts
- `xQueueSend` with zero timeout — immediate drop on full queue
- Main task cannot drain the queue faster than interrupts fill it at this rate
- No rate-limiting or coalescing of same-type events

## Impact Assessment

**Severity: Low (non-fatal, self-recovering)**

- Dropped events are USB plug/unplug state transitions
- The MCU picks up the final USB state once the burst stops
- No data corruption, no memory leak, no watchdog trip
- Normal operation resumes within 2 seconds
- Only triggers under abnormal conditions (rapid repeated plug/unplug)

**Risk scenarios where this could matter:**
- User rapidly inserting/removing USB-C cable (unlikely to hit 20Hz)
- Faulty USB connector causing electrical bounce (possible)
- Automated test equipment (confirmed by this test)

## Recommended Fixes

1. **Debounce SBU GPIO** — ignore transitions within <100ms of previous edge (simplest, most effective)
2. **Use `xQueueOverwrite` for SBU** — only the latest USB state matters, not every edge
3. **Rate-limit in ISR** — suppress duplicate events if same type already queued
4. **Increase main task queue depth** — buys headroom but doesn't fix root cause

## Reproduction

```bash
cd utils/custom/device_tests
python3 usb_queue_overflow_test.py -n 5 --bursts 30 --interval 0.05 --recovery 60
```

Requirements:
- serial_mux running on voodoo board (192.168.3.1) ports 9001/9002
- Device awake with USB plugged (DO6 high)
- Voodoo board Modbus relay on DO6 controls USB VBUS

## Logs

Session log: `/tmp/usb_queue_overflow_logs/session_20260623_000925.log`

Per-cycle logs in `/tmp/usb_queue_overflow_logs/`:
- `usb_overflow_cycle{1-5}_OVERFLOW_RECOVERED_mcu_*.log`
- `usb_overflow_cycle{1-5}_OVERFLOW_RECOVERED_isp_*.log`
