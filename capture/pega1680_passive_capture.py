#!/usr/bin/env python3
"""
PEGA-1680: Passive ISP+MCU capture.

Connects to both consoles, writes logs to disk continuously.
No login, no commands — just raw capture. User handles login/tail/toggles manually.

Usage:
    python3 utils/custom/device_tests/pega1680_passive_capture.py
    # Kill with SIGTERM to stop. Logs are already on disk.
"""

import os
import signal
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib.console_utils import get_serial_mux_config, SerialMuxReader

LOG_DIR = "/tmp/pega1680_logs"


class PassiveCapture:
    def __init__(self):
        self.cfg = get_serial_mux_config()
        self.isp = None
        self.mcu = None
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.isp_file = None
        self.mcu_file = None
        self.isp_count = 0
        self.mcu_count = 0

    def _isp_cb(self, line, source):
        self.isp_count += 1
        self.isp_file.write(f"{time.time():.3f} {line}\n")
        self.isp_file.flush()

    def _mcu_cb(self, line, source):
        self.mcu_count += 1
        self.mcu_file.write(f"{time.time():.3f} {line}\n")
        self.mcu_file.flush()

    def run(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        isp_path = os.path.join(LOG_DIR, f"isp_{self.timestamp}.log")
        mcu_path = os.path.join(LOG_DIR, f"mcu_{self.timestamp}.log")

        self.isp_file = open(isp_path, "w")
        self.mcu_file = open(mcu_path, "w")

        def _shutdown(sig, frame):
            print(f"\nStopped. ISP: {self.isp_count} lines, MCU: {self.mcu_count} lines")
            print(f"  {isp_path}")
            print(f"  {mcu_path}")
            self.isp_file.close()
            self.mcu_file.close()
            if self.isp:
                self.isp.disconnect()
            if self.mcu:
                self.mcu.disconnect()
            os._exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        print("[1/2] Connecting ISP (passive)...")
        self.isp = SerialMuxReader(
            "ISP", self.cfg['isp_host'], self.cfg['isp_port'],
            event_callback=self._isp_cb)
        self.isp.connect()
        self.isp.start()
        print("  [OK] ISP connected")

        print("[2/2] Connecting MCU (192.168.7.100:9002)...")
        self.mcu = SerialMuxReader(
            "MCU", self.cfg['mcu_host'], self.cfg['mcu_port'],
            event_callback=self._mcu_cb)
        self.mcu.connect()
        self.mcu.start()
        print("  [OK] MCU connected")

        print(f"\n{'='*60}")
        print("CAPTURING — logs written live to disk")
        print(f"  ISP: {isp_path}")
        print(f"  MCU: {mcu_path}")
        print(f"{'='*60}")
        sys.stdout.flush()

        while True:
            time.sleep(1)


if __name__ == "__main__":
    PassiveCapture().run()
