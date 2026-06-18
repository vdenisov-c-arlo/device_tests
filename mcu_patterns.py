"""Common MCU/ISP log patterns and anomaly detection for test sequences.

Import and use in all test scripts to detect crashes, hangs, and known failure modes.
Stops the test early and reports the issue instead of running blind.

Usage:
    from mcu_patterns import (
        CRASH_PATTERNS, HANG_PATTERNS, SLEEP_INDICATOR,
        check_for_anomalies, AnomalyType, is_crash_dump_line,
        save_crash_dump,
    )
"""

import os
import re
from datetime import datetime
from enum import Enum, auto

# --- Log pattern constants ---

SLEEP_INDICATOR = "Network Stack Suspended, MCU can enter DeepSleep power mode"

# --- MCU-side patterns ---

# MCU hard crashes
MCU_CRASH_PATTERNS = [
    "HardFault",
    "BusFault",
    "MemManage",
    "UsageFault",
    "Assertion failed",
    "Unhandled exception",
    "abort()",
]

# MCU hang indicators — alive but stuck
MCU_HANG_PATTERNS = [
    "xQueueSend fail",
    "Erpc xQueueSend fail",
    "erpc error: 14 Server is stopped",
    "deadlock",
    "watchdog reset",
    "WDT expired",
]

# --- ISP-side patterns ---

# ISP crash/reboot (seeing bootloader means ISP rebooted unexpectedly)
ISP_CRASH_PATTERNS = [
    "IPL ",           # bootloader start (IPL 5bccf6a...)
    "HW Reset",
    "DRAM Size:",
    "Initramfs unpacking",
    "kernel panic",
    "Oops:",
    "segfault",
    "coredump",
    "core dump",
    "Internal error:",
    "Unable to handle kernel",
]

# ISP hang indicators
ISP_HANG_PATTERNS = [
    "watchdog: watchdog0: watchdog did not stop",
    "BUG: soft lockup",
    "INFO: task .* blocked for more than",
    "rcu_sched detected stalls",
]

# eRPC communication failures (can appear on either side)
ERPC_ERROR_PATTERNS = [
    "erpc error:",
    "eRPC timeout",
    "Failed to send data",
    "ENOBUFS",
]

# Legacy combined lists for backward compatibility
CRASH_PATTERNS = MCU_CRASH_PATTERNS + ISP_CRASH_PATTERNS
HANG_PATTERNS = MCU_HANG_PATTERNS + ISP_HANG_PATTERNS
ISP_REBOOT_PATTERNS = ISP_CRASH_PATTERNS

# Sleep/wake state patterns
WAKE_PATTERNS = [
    "pegaDp_sleep_wakeup_by_reason",
    "wakeup_reason",
    "IspPowerOnProcess",
]

ISP_OFF_PATTERNS = [
    "IspPowerStatusIsOffEventProcess",
    "ISP_POWER_IS_OFF",
    "pegaMain_IspPowerOffProcess",
]

ISP_WAKE_PATTERNS = [
    "IspPowerOnProcess",
    "PEGA_MAIN_TASK_ISP_EVENT",
    "ISP_EVENT_CHARGING",
]

SBU_PATTERNS = [
    "SBU_Insert",
    "SBU_Remove",
    "USB plugged",
]

SLEEP_VOTE_PATTERNS = [
    "vote_action",
    "STANDBY",
    "sleep_enter",
]


# --- Anomaly detection ---

class AnomalyType(Enum):
    CRASH = auto()
    HANG = auto()
    ISP_REBOOT = auto()
    ERPC_ERROR = auto()
    NONE = auto()


def check_for_anomalies(lines, check_hang=True, check_crash=True, check_isp_reboot=True):
    """Check a list of log lines for anomalies (legacy, uses combined patterns).

    Returns:
        (AnomalyType, matching_line) if anomaly found, (AnomalyType.NONE, None) otherwise.
    """
    for line in lines:
        if check_crash:
            for pattern in CRASH_PATTERNS:
                if pattern in line:
                    return AnomalyType.CRASH, line

        if check_hang:
            for pattern in HANG_PATTERNS:
                if pattern in line:
                    return AnomalyType.HANG, line

        if check_isp_reboot:
            for pattern in ISP_REBOOT_PATTERNS:
                if pattern in line:
                    return AnomalyType.ISP_REBOOT, line

    return AnomalyType.NONE, None


def check_mcu_line(line):
    """Check a single MCU console line for anomalies.

    Returns:
        (AnomalyType, line) if anomaly found, (AnomalyType.NONE, None) otherwise.
    """
    for pattern in MCU_CRASH_PATTERNS:
        if pattern in line:
            return AnomalyType.CRASH, line
    for pattern in MCU_HANG_PATTERNS:
        if pattern in line:
            return AnomalyType.HANG, line
    return AnomalyType.NONE, None


def check_isp_line(line):
    """Check a single ISP console line for anomalies.

    Returns:
        (AnomalyType, line) if anomaly found, (AnomalyType.NONE, None) otherwise.
    """
    for pattern in ISP_CRASH_PATTERNS:
        if pattern in line:
            return AnomalyType.CRASH, line
    for pattern in ISP_HANG_PATTERNS:
        if pattern in line:
            return AnomalyType.HANG, line
    return AnomalyType.NONE, None


def check_lines_continuous(line, check_hang=True, check_crash=True, check_isp_reboot=True):
    """Check a single line as it arrives (legacy, uses combined patterns).

    Returns:
        (AnomalyType, line) if anomaly found, (AnomalyType.NONE, None) otherwise.
    """
    if check_crash:
        for pattern in CRASH_PATTERNS:
            if pattern in line:
                return AnomalyType.CRASH, line

    if check_hang:
        for pattern in HANG_PATTERNS:
            if pattern in line:
                return AnomalyType.HANG, line

    if check_isp_reboot:
        for pattern in ISP_REBOOT_PATTERNS:
            if pattern in line:
                return AnomalyType.ISP_REBOOT, line

    return AnomalyType.NONE, None


# --- Crash dump detection and saving ---

_HEX_DUMP_RE = re.compile(r'^\[?[0-9a-fA-F]{6,8}\]?\s+([0-9a-fA-F]{2}\s+){4,}')


def is_crash_dump_line(line):
    """Detect raw memory/crash dump lines (hex address + hex data).

    Matches patterns like:
        [8048c000]  f5 eb d7 ff f5 fa f5 6b ...
        0x28000100: 00 01 02 03 ...
    """
    return bool(_HEX_DUMP_RE.match(line.strip()))


def save_crash_dump(lines, output_dir, test_name, cycle_num, source="mcu"):
    """Save crash dump lines to a timestamped file.

    Args:
        lines: list of log lines (will extract the dump portion)
        output_dir: directory to save dump files
        test_name: name of the test (e.g., "usb_wake_b")
        cycle_num: cycle number when crash occurred
        source: "mcu" or "isp"

    Returns:
        Path to saved file, or None if no dump lines found.
    """
    dump_lines = [l for l in lines if is_crash_dump_line(l)]
    context_lines = lines[-20:] if len(lines) > 20 else lines

    if not dump_lines and not any(
        any(p in l for p in MCU_CRASH_PATTERNS + ISP_CRASH_PATTERNS) for l in lines
    ):
        return None

    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"crash_{test_name}_cycle{cycle_num}_{source}_{ts}.log"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w") as f:
        f.write(f"# Crash dump — {test_name} cycle {cycle_num} ({source})\n")
        f.write(f"# Time: {datetime.now().isoformat()}\n")
        f.write(f"# Dump lines: {len(dump_lines)}, context lines: {len(context_lines)}\n\n")
        f.write("--- DUMP ---\n")
        for l in dump_lines:
            f.write(l + "\n")
        f.write("\n--- CONTEXT (last 20 lines) ---\n")
        for l in context_lines:
            f.write(l + "\n")

    return filepath
