#!/usr/bin/env python3
"""Rapid Restart Stuck Test — reproduces PEGA-1132 (Jul 2026 variant).

Sends repeated "camtest set_reboot" commands via ISP console to trigger the
eRPC/SDIO shutdown race condition that causes MCU to get stuck and WDT reset.

The failure occurs when ISP reboots before MCU has finished processing the
previous shutdown sequence (pegaDp_shutdown_erpc_and_sdio_processing).

Detection:
  1. MCU console goes silent after seeing eRPC shutdown messages
  2. WDT reset message appears
  3. Core dump or pthread_join errors in eRPC deinit path

Usage:
  python3 rapid_restart_stuck_test.py [NUM_CYCLES] [DELAY_BETWEEN]
    NUM_CYCLES      — number of rapid restarts per round (default 5)
    DELAY_BETWEEN   — seconds to wait between restarts (default 5)

  Smaller DELAY_BETWEEN = more aggressive race (but too small means ISP
  hasn't booted far enough to accept commands).
"""

import time
import sys
import os
import socket
from enum import Enum, auto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.console_utils import DeviceTestBase, SerialMuxReader, isp_init_console, _recv_all, _drain_sock
from voodoo.voodoo_channels import DO_SYNC, DO_RESET

sys.stdout.reconfigure(line_buffering=True)

NUM_RESTARTS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
DELAY_BETWEEN = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
NUM_ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 10

ISP_LOGIN_TIMEOUT = 40
ISP_REBOOT_CMD = "camtest set_reboot"
MCU_WDT_TIMEOUT = 60

ERPC_SHUTDOWN_PATTERNS = [
    "pegaERPC_StopServer",
    "pega_erpc_server_deinit",
    "eRPC from DOWN to STANDBY",
    "eRPC from CONNECTED to DOWN",
    "eRPC from UP to DOWN",
    "step 1 pegaERPC_StopServer",
]

MCU_CRASH_PATTERNS = [
    "pthread_join: non-pthread context",
    "Core dump:sp",
    "HardFault",
    "BusFault",
    "MemManage",
    "UsageFault",
    "Assertion failed",
    "abort()",
    "WDT expired",
    "watchdog reset",
]

MCU_ALIVE_PATTERNS = [
    "pegaBattery",
    "pega_schedule",
    "voter:",
    "WHD Stats",
    "whd_wowl",
    "Network Stack",
    "pegaDp_",
    "IspPowerOn",
    "pga_sm_",
]


class Event(Enum):
    ISP_PROMPT = auto()
    MCU_ERPC_SHUTDOWN = auto()
    MCU_CRASH = auto()
    MCU_WDT_RESET = auto()
    MCU_ALIVE = auto()
    MCU_SILENT = auto()
    ISP_BOOTING = auto()


class RapidRestartTest(DeviceTestBase):
    _test_name = "rapid_restart_stuck"
    _log_dir = "/tmp/rapid_restart_logs"
    _reset_recovery_timeout = 120

    def __init__(self):
        super().__init__()
        self.mcu_last_line_time = time.time()
        self.mcu_stuck = False
        self.crash_detected = False
        self.crash_line = ""

    def _check_events(self, line, source):
        if source == "MCU":
            self.mcu_last_line_time = time.time()

            for pattern in MCU_CRASH_PATTERNS:
                if pattern in line:
                    self.crash_detected = True
                    self.crash_line = line
                    self.event_callback(Event.MCU_CRASH, source, line)
                    return

            for pattern in ERPC_SHUTDOWN_PATTERNS:
                if pattern in line:
                    self.event_callback(Event.MCU_ERPC_SHUTDOWN, source, line)
                    return

            if "SDIO HM Application" in line or "main()" in line:
                self.event_callback(Event.MCU_WDT_RESET, source, line)
                return

            for pattern in MCU_ALIVE_PATTERNS:
                if pattern in line:
                    self.event_callback(Event.MCU_ALIVE, source, line)
                    return

        elif source == "ISP":
            if "login:" in line or line.rstrip().endswith("#"):
                self.event_callback(Event.ISP_PROMPT, source, line)
            elif "COLD BOOT" in line or "Linux version" in line:
                self.event_callback(Event.ISP_BOOTING, source, line)

    def _isp_send_cmd(self, cmd, wait=1.0):
        """Send a command to ISP console."""
        if not self.isp or not self.isp.sock:
            return False
        try:
            self.isp.sock.sendall(f"{cmd}\r\n".encode())
            time.sleep(wait)
            return True
        except OSError as e:
            print(f"    [ERROR] ISP send failed: {e}")
            return False

    def _isp_login(self, timeout=ISP_LOGIN_TIMEOUT):
        """Wait for ISP to boot and login. Returns True on success.

        Uses the reader's captured lines to detect prompts, since the reader
        thread owns the socket reads. Sends CR/LF to provoke prompt output.
        """
        if not self.isp or not self.isp.sock:
            return False

        deadline = time.time() + timeout
        print(f"    [ISP] Waiting for login prompt (up to {timeout}s)...")

        self.isp.clear_lines()
        self.isp.start_recording()

        while time.time() < deadline:
            try:
                self.isp.sock.sendall(b"\x03\r\n")
            except OSError:
                time.sleep(2)
                continue

            time.sleep(3)
            lines = self.isp.get_lines()

            for line in lines:
                if "login:" in line:
                    try:
                        self.isp.sock.sendall(b"root\r\n")
                        time.sleep(2)
                        lines2 = self.isp.get_lines()
                        for l2 in lines2:
                            if "assword:" in l2:
                                self.isp.sock.sendall(b"arlo\r\n")
                                time.sleep(2)
                                break
                    except OSError:
                        continue
                    self.isp.stop_recording()
                    self.isp.clear_lines()
                    print("    [ISP] Logged in")
                    return True
                elif line.rstrip().endswith("#") or line.strip() == "#":
                    self.isp.stop_recording()
                    self.isp.clear_lines()
                    print("    [ISP] Already logged in")
                    return True

            self.isp.clear_lines()

        self.isp.stop_recording()
        print("    [ISP] Login timeout!")
        return False

    def _check_mcu_stuck(self, timeout=MCU_WDT_TIMEOUT):
        """Monitor MCU for stuck state (no output for extended period).

        Returns:
            "alive"  — MCU is still producing output
            "stuck"  — MCU went silent (stuck)
            "crash"  — crash/coredump detected
            "wdt"    — WDT reset detected (MCU rebooted)
        """
        deadline = time.time() + timeout
        silence_start = None

        while time.time() < deadline:
            time.sleep(1)

            crash = self.check_event(Event.MCU_CRASH)
            if crash:
                return "crash"

            wdt = self.check_event(Event.MCU_WDT_RESET)
            if wdt:
                return "wdt"

            silence = time.time() - self.mcu_last_line_time
            if silence > 15:
                if silence_start is None:
                    silence_start = time.time()
                    print(f"    [MCU] Silent for {silence:.0f}s...")
                elif time.time() - silence_start > 30:
                    return "stuck"
            else:
                silence_start = None

        return "alive"

    def run_cycle(self, cycle_num):
        """One round of rapid restarts."""
        print(f"\n{'='*60}")
        print(f"[ROUND {cycle_num}/{NUM_ROUNDS}] "
              f"Sending {NUM_RESTARTS} restarts with {DELAY_BETWEEN}s gap")
        print(f"{'='*60}")

        self.clear_events()
        self.crash_detected = False
        self.crash_line = ""
        self.mcu_stuck = False
        self.mcu.start_recording()
        self.isp.start_recording()

        # First, make sure device is up and ISP is accessible
        print("  [SETUP] Ensuring device is online...")
        if not self._isp_login():
            print("  [SETUP] Can't reach ISP — trying SYNC wake...")
            self.press_button(DO_SYNC, 2.0)
            time.sleep(20)
            if not self._isp_login():
                print("  [FAIL] Device unreachable")
                self.save_logs(cycle_num, "unreachable")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False

        # Send rapid restarts
        for restart_num in range(1, NUM_RESTARTS + 1):
            print(f"\n  [RESTART {restart_num}/{NUM_RESTARTS}]")

            # Check if MCU already crashed from previous restart
            crash = self.check_event(Event.MCU_CRASH)
            if crash:
                print(f"    [CRASH!] MCU crashed: {crash[2][:120]}")
                self.save_logs(cycle_num, f"crash_restart{restart_num}")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False

            # Send reboot command
            print(f"    [CMD] Sending '{ISP_REBOOT_CMD}'...")
            if not self._isp_send_cmd(ISP_REBOOT_CMD, wait=0.5):
                print(f"    [WARN] Failed to send command (ISP may have rebooted)")

            # Wait for ISP to reboot
            if restart_num < NUM_RESTARTS:
                print(f"    [WAIT] {DELAY_BETWEEN}s before next restart...")

                # During the wait, monitor MCU for stuck/crash
                wait_deadline = time.time() + DELAY_BETWEEN
                while time.time() < wait_deadline:
                    time.sleep(0.5)
                    crash = self.check_event(Event.MCU_CRASH)
                    if crash:
                        print(f"    [CRASH!] MCU crashed during wait: {crash[2][:120]}")
                        self.save_logs(cycle_num, f"crash_restart{restart_num}")
                        self.mcu.stop_recording()
                        self.isp.stop_recording()
                        return False

                # Try to login for next restart
                if not self._isp_login(timeout=max(DELAY_BETWEEN, 30)):
                    # ISP not ready — try shorter wait, send blind command
                    print(f"    [WARN] ISP not ready — sending blind reboot")
                    self._isp_send_cmd(ISP_REBOOT_CMD, wait=0.5)

        # After last restart, wait for ISP to come back and login
        print(f"\n  [VERIFY] Waiting for ISP to come back after last restart...")
        time.sleep(5)

        # Check for crash/WDT during the reboot
        crash = self.check_event(Event.MCU_CRASH)
        if crash:
            print(f"  [FAIL] *** MCU CRASH during restart: {crash[2][:120]} ***")
            self.save_logs(cycle_num, "CRASH_REPRO")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        wdt = self.check_event(Event.MCU_WDT_RESET)
        if wdt:
            print(f"  [FAIL] *** WDT RESET during restart sequence ***")
            self.save_logs(cycle_num, "WDT_REPRO")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Now try to login to ISP — this confirms ISP is back up
        if not self._isp_login(timeout=ISP_LOGIN_TIMEOUT):
            # ISP didn't come back — the whole device might be stuck
            print(f"  [FAIL] ISP never came back after restart!")
            self.save_logs(cycle_num, "ISP_STUCK")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        print(f"  [VERIFY] ISP is back. Waiting 20s for MCU to wake via eRPC...")
        time.sleep(20)

        # NOW check: MCU should be alive since ISP reconnected
        # Check for crash/WDT that happened during the wait
        crash = self.check_event(Event.MCU_CRASH)
        if crash:
            print(f"  [FAIL] *** MCU CRASH after ISP reconnect: {crash[2][:120]} ***")
            self.save_logs(cycle_num, "CRASH_REPRO")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        wdt = self.check_event(Event.MCU_WDT_RESET)
        if wdt:
            print(f"  [FAIL] *** WDT RESET — MCU was stuck ***")
            self.save_logs(cycle_num, "WDT_REPRO")
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Check if MCU has produced any output recently
        mcu_lines = self.mcu.get_lines()
        silence = time.time() - self.mcu_last_line_time
        print(f"  [DEBUG] MCU recorded {len(mcu_lines)} lines, silence={silence:.0f}s")
        if mcu_lines:
            print(f"  [DEBUG] Last MCU lines:")
            for l in mcu_lines[-5:]:
                print(f"    {l[:120]}")

        if silence > 30:
            # MCU silent even though ISP is up — try to wake MCU via SYNC
            print(f"  [WARN] MCU silent for {silence:.0f}s despite ISP being up")
            print(f"  [VERIFY] Pressing SYNC to force MCU wake...")
            self.press_button(DO_SYNC, 2.0)
            time.sleep(10)

            # Check again after SYNC
            new_silence = time.time() - self.mcu_last_line_time
            new_lines = self.mcu.get_lines()
            print(f"  [DEBUG] After SYNC: {len(new_lines)} total lines, silence={new_silence:.0f}s")

            if new_silence > 15:
                # Still no output after button press — MCU is truly stuck
                print(f"  [FAIL] *** PEGA-1132 REPRODUCED: MCU unresponsive after SYNC ***")
                self.save_logs(cycle_num, "STUCK_REPRO")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False
            else:
                # MCU woke up from SYNC — it was just in deep sleep, ISP failed to wake it
                print(f"  [WARN] MCU was in deep sleep — ISP didn't wake it via eRPC")
                print(f"         This might indicate ISP eRPC client didn't reconnect")
                self.save_logs(cycle_num, "ISP_ERPC_FAIL")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                return False
        else:
            self.mcu.stop_recording()
            self.isp.stop_recording()
            print(f"  [PASS] MCU alive after rapid restarts (last output {silence:.0f}s ago)")
            print(f"  [SETTLE] Waiting 15s before next round...")
            time.sleep(15)
            return True

    def recovery(self, cycle):
        """Reset device after failure."""
        print(f"\n  [RECOVERY] Cycle {cycle} failed. Hardware reset...")
        self.press_button(DO_RESET, 1.0)
        time.sleep(5)

        print("  [RECOVERY] Reconnecting consoles...")
        self.reconnect_consoles()
        self.clear_events()

        print(f"  [RECOVERY] Waiting {self._reset_recovery_timeout}s for device to come up...")
        deadline = time.time() + self._reset_recovery_timeout
        while time.time() < deadline:
            time.sleep(5)
            if self._isp_login(timeout=10):
                print("  [RECOVERY] ISP back online")
                time.sleep(20)
                return True

        print("  [RECOVERY] Failed to recover device")
        return False


if __name__ == "__main__":
    print(f"PEGA-1132 Rapid Restart Stuck Test")
    print(f"  Restarts per round: {NUM_RESTARTS}")
    print(f"  Delay between restarts: {DELAY_BETWEEN}s")
    print(f"  Number of rounds: {NUM_ROUNDS}")
    print()

    test = RapidRestartTest()
    sys.exit(test.run(num_cycles=NUM_ROUNDS))
