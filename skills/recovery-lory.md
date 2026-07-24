---
name: recovery-lory
description: Put Lory into USB ROM boot mode via Program+Reset buttons, detect the new SCSI device, and flash with sstar-flash.
---

# Recovery Lory (USB Flash)

## When to Use

When the device needs recovery flashing over USB — either normal OTA is broken, the device is bricked, or you need to do a full factory-level flash via the SigmaStar ROM bootloader.

## Prerequisites

- Testbot4 reachable at the address in `$ARLO_CLAUDE_SETTINGS/utils/custom/serial_mux/serial_mux.ini` `[testbot4]` section.
- Device connected via USB to this host (USB data cable, not charge-only).
- `sstar-flash` tool built at the project's `sstar-flash/sstar-flash` path.
- A `.bin` USB image file (e.g., `USBImage_AVD6001_*.bin`).

## Arguments

The skill requires the path to the `.bin` image file:

```
/recovery-lory <path-to-USBImage.bin>
```

If no argument is provided, look for `firmware/artifacts/USBImage_*.bin` in the project root, or ask the user.

## Steps

### Phase 1: Enter USB Boot Mode (Program + Reset)

The sequence to enter ROM bootloader:
1. Record existing `/dev/sg*` devices (snapshot before).
2. Press and HOLD Program button (DO3).
3. While Program is held, press Reset (DO2) for 2 seconds.
4. Release Reset (keep Program held).
5. Wait 1 second for the SoC to enter ROM boot mode.
6. Release Program button.

**Implementation** using the `Testbot4` class (read-modify-write pattern preserves other DO channels):
- DO bits: DO0=Sync, DO1=Front, DO2=Reset, DO3=Program, DO5=Daynight Shutter, DO6=USB Plug, DO7=PIR

```python
import sys, os, time, subprocess
sys.path.insert(0, os.path.join(os.environ.get('ARLO_CLAUDE_SETTINGS',
    '/home/denisov/arlo/claude_settings'), 'utils', 'custom', 'device_tests'))
from testbot4.testbot4_do_pulse import Testbot4

TESTBOT4 = '192.168.7.100'
DO_RESET = 2
DO_PROGRAM = 3

def ssh_ls_devs():
    r = subprocess.run(['ssh', '-o', 'ConnectTimeout=3', f'root@{TESTBOT4}',
                        'ls /dev/sg* /dev/sd* 2>/dev/null | sort'],
                       capture_output=True, text=True)
    return set(r.stdout.strip().splitlines())

# Snapshot before
before = ssh_ls_devs()
print(f"Baseline devices: {sorted(before)}")

# Button sequence: Program+Reset
with Testbot4() as tb:
    tb.on(DO_PROGRAM)
    print("Program button DOWN")
    time.sleep(0.3)

    tb.on(DO_RESET)
    print("Reset button DOWN (Program still held)")
    time.sleep(2.0)

    tb.off(DO_RESET)
    print("Reset button UP (Program still held)")
    time.sleep(1.0)

    tb.off(DO_PROGRAM)
    print("Program button UP -- waiting for USB enumeration...")

# Detect new SCSI device
sg_device = None
for i in range(10):
    time.sleep(1)
    after = ssh_ls_devs()
    new_devs = after - before
    sg_devs = [d for d in new_devs if d.startswith('/dev/sg')]
    if sg_devs:
        sg_device = sg_devs[0]
        print(f"Detected: {sg_device} (new: {sorted(new_devs)})")
        break
    print(f"  poll {i+1}/10...")

if not sg_device:
    print("FAILED: No new /dev/sgN device after 10 seconds")
    sys.exit(1)

# Write the detected device to a temp file for Phase 3
with open('/tmp/recovery_sg_device', 'w') as f:
    f.write(sg_device)
print(f"ROM boot mode confirmed: {sg_device}")
```

Run this as an inline python3 script via Bash. On success it writes the detected `/dev/sgN` path to `/tmp/recovery_sg_device` for Phase 3.

If no new device appears after 10 seconds, the script exits with error — retry Phase 1 (check USB cable is data-capable).

### Phase 3: Flash with sstar-flash

The device is physically USB-connected to the **testbot4**, not the local workstation. `sstar-flash` is already installed on testbot4.

1. Copy the image to testbot4:

```bash
scp <image.bin> root@192.168.7.100:/tmp/USBImage.bin
```

2. Read the detected device from `/tmp/recovery_sg_device` (written by Phase 1+2 script) and flash:

```bash
SG_DEV=$(cat /tmp/recovery_sg_device)
ssh root@192.168.7.100 "sstar-flash -v $SG_DEV /tmp/USBImage.bin"
```

Monitor the output for "Done." at the end. Tail the last ~25 lines to confirm all script commands executed.

### Phase 4: Reset and Verify

The device does not reboot by itself after flashing. Press the Reset button to boot into the new firmware:

```python
import sys, os, time
sys.path.insert(0, os.path.join(os.environ.get('ARLO_CLAUDE_SETTINGS',
    '/home/denisov/arlo/claude_settings'), 'utils', 'custom', 'device_tests'))
from testbot4.testbot4_do_pulse import Testbot4

DO_RESET = 2

with Testbot4() as tb:
    tb.pulse(DO_RESET, duration=2.0)
    print("Reset pulse sent -- device rebooting")
```

Wait 30 seconds after the reset pulse, then check if the device is accessible via serial console (ISP port) or if it has dropped off USB (expected — it reboots into normal mode, not mass storage).

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| Testbot4 unreachable | Network issue or board powered off | Check 192.168.7.100 reachability |
| No new /dev/sg* after reset | Device didn't enter boot mode | Retry Phase 1; check USB cable is data-capable |
| `sstar-flash` permission denied | Need root for SG_IO | Use `sudo` |
| IPL download failed | SCSI transport error | Check USB connection, retry |
| "device not recognized" | Device not in ROM boot mode | Retry from Phase 1 |
| Script execution failed | Flash write error | May indicate hardware issue |

## Success Criteria

- Device enters USB ROM boot mode (new `/dev/sgN` appears).
- `sstar-flash` parses the image successfully.
- All three phases (IPL → U-Boot → Script) complete without error.
- Tool prints "Done."
