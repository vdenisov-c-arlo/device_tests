#!/usr/bin/env python3
"""Record ISP and MCU console logs until stopped. Saves to /tmp/console_capture/."""

import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import SerialMuxReader, get_serial_mux_config, isp_init_console

LOG_DIR = "/tmp/console_capture"


def main():
    cfg = get_serial_mux_config()
    os.makedirs(LOG_DIR, exist_ok=True)

    print("[*] Connecting to ISP ({}:{})...".format(cfg['isp_host'], cfg['isp_port']))
    isp = SerialMuxReader("ISP", cfg['isp_host'], cfg['isp_port'])
    isp.connect()

    print("[*] Initializing ISP console (login + tail -f)...")
    isp_init_console(isp.sock)
    time.sleep(2)

    print("[*] Connecting to MCU ({}:{})...".format(cfg['mcu_host'], cfg['mcu_port']))
    mcu = SerialMuxReader("MCU", cfg['mcu_host'], cfg['mcu_port'])
    mcu.connect()

    isp.start_recording()
    mcu.start_recording()
    isp.start()
    mcu.start()

    print("[*] Recording. Press Ctrl+C or send SIGINT to stop.\n")

    stopped = False

    def on_stop(sig, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    try:
        while not stopped:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    isp.stop_recording()
    mcu.stop_recording()

    ts = time.strftime("%Y%m%d_%H%M%S")
    isp_lines = isp.get_lines()
    mcu_lines = mcu.get_lines()

    isp_path = os.path.join(LOG_DIR, f"isp_{ts}.log")
    mcu_path = os.path.join(LOG_DIR, f"mcu_{ts}.log")

    with open(isp_path, "w") as f:
        f.write("\n".join(isp_lines))
    with open(mcu_path, "w") as f:
        f.write("\n".join(mcu_lines))

    print(f"\n[*] Captured {len(isp_lines)} ISP lines → {isp_path}")
    print(f"[*] Captured {len(mcu_lines)} MCU lines → {mcu_path}")

    isp.disconnect()
    mcu.disconnect()


if __name__ == "__main__":
    main()
