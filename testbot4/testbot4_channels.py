"""Testbot4 digital output channel definitions.

All scripts that reference testbot4 DO channels should import from here.
"""

DO_SYNC = 0       # SYNC button (wake device from sleep)
DO_FRONT = 1      # Front button (doorbell ring)
DO_RESET = 2      # RESET button (hardware reset)
DO_PROGRAM = 3    # Program mode button (DFU)
DO_AMBLIGHT = 5   # Ambient light shutter (ON = closed/Night, OFF = open/Day)
DO_USB = 6        # USB VBUS (ON = charger plugged, OFF = unplugged)
DO_PIR = 7        # PIR (simulate motion trigger)

CHANNEL_NAMES = {
    DO_SYNC: "Sync",
    DO_FRONT: "Front",
    DO_RESET: "Reset",
    DO_PROGRAM: "Program",
    DO_AMBLIGHT: "Amblight Shutter",
    DO_USB: "USB",
    DO_PIR: "PIR",
}
