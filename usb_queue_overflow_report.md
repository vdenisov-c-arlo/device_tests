# USB Rapid Plug/Unplug Queue Overflow Test Report

**Date:** 2026-06-22
**Device:** Lory (AVD6001 / C521)
**Test script:** `utils/custom/device_tests/usb_queue_overflow_test.py`

## Summary

Rapid USB plug/unplug via voodoo board DO6 reliably triggers MCU main task queue overflow. The issue reproduces on the first attempt with default parameters.

## Test Parameters

| Parameter | Value |
|-----------|-------|
| Bursts per cycle | 30 |
| Toggle interval | 50ms |
| Burst execution | Voodoo board local Modbus (~3ms RTT) |
| Total burst duration | ~1.5s |

## Result

**OVERFLOW triggered on cycle 1/1.**

## Error Pattern

```
[ERROR]mcu:pega_main.c:pegaMain_TaskSet()  Pega_Main xQueueSend fail , eMainTask= 13, result =101580801
[ERROR]mcu:pega_main.c:pegaMain_TaskSet()  Pega_Main xQueueSend fail , eMainTask= 12, result =101580801
```

- **40 overflow errors** in a single burst
- Two task types overflow alternately:
  - `eMainTask= 12` — SBU_Remove events
  - `eMainTask= 13` — SBU_Insert events
- `result = 101580801` — FreeRTOS `errQUEUE_FULL` return from `xQueueSend()`

## Sequence of Events (from log)

1. Device was in deep sleep (USB unplugged prior)
2. First USB plug wakes MCU: `pegaDp_sleep_wakeup_by_reason[USB_REMOVE]`
3. MCU starts ISP power-on sequence, eRPC init, SDIO HM re-init
4. During this heavy initialization, rapid USB toggle events flood the queue
5. Main task queue saturates — 40 consecutive `xQueueSend fail` errors
6. Events are silently dropped (no retry, no backpressure)

## Root Cause Analysis

The `pegaMain_TaskSet()` function posts events to the FreeRTOS main task queue with `xQueueSend()` (no timeout / `0` tick wait). When the main task is busy processing ISP power-on (SDIO HM init, eRPC server start, WiFi resume — takes hundreds of ms), the queue fills up and subsequent SBU interrupt-driven events are dropped.

**Contributing factors:**
- Queue depth appears insufficient for burst interrupt traffic
- No debouncing or rate-limiting on SBU (USB detect) GPIO interrupts
- `xQueueSend` called from ISR context with zero timeout — immediate drop on full queue
- Main task blocked on slow operations (SDIO HM, eRPC init) during wake

## Recovery Behavior

**The DUT self-recovers without intervention.** Tested with 60s observation after burst (no reset):

- 37 overflow errors, all within the first second of the burst
- By second 2, normal operation resumed (eRPC connected, sleep votes processed)
- MCU completed full wake→operate→sleep cycle within 60s
- Ended in deep sleep normally (`Network Stack Suspended`)
- MCU console remained responsive throughout

**Verdict:** Non-fatal. Dropped SBU events don't cause a permanent bad state — the MCU picks up the final USB state eventually and resumes normal operation. The overflow is a robustness issue (log noise, potential for missed state in edge cases) but not a crasher or hang.

## Potential Fixes

1. **Debounce SBU GPIO** — ignore transitions within <100ms of previous (hardware or software)
2. **Increase main task queue depth** — buys headroom but doesn't fix the root cause
3. **Use `xQueueOverwrite` for SBU** — only the latest USB state matters, not every edge
4. **Rate-limit in ISR** — drop duplicate events if same type already pending

## Logs

- MCU log: `/tmp/usb_queue_overflow_logs/usb_overflow_cycle1_OVERFLOW_mcu_231432.log`
- ISP log: `/tmp/usb_queue_overflow_logs/usb_overflow_cycle1_OVERFLOW_isp_231432.log`
- Session log: `/tmp/usb_queue_overflow_logs/session_20260622_231333.log`

## Reproduction

```bash
cd utils/custom/device_tests
python3 usb_queue_overflow_test.py -n 1 --bursts 30 --interval 0.05
```

Requires: serial_mux running on voodoo board (192.168.3.1), device in standby/sleep with USB unplugged.
