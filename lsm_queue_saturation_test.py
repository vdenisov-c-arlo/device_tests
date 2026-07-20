#!/usr/bin/env python3
"""PEGA-1697: LSM event queue saturation test.

Two independent reproduction triggers are exercised:

  Phase A (Trigger A) — saturation during a concurrent live stream + motion.
    Drives rapid PIR triggers combined with ALS day/night oscillation via the
    voodoo board while a user-initiated live stream is active. eRPC
    post-processing to the MCU slows the LSM (Light Source Manager) event queue
    drain rate; simultaneous PIR + ALS switches overflow the fixed depth-25
    queue.

  Phase B (Trigger B) — saturation during the stream-end transition.
    Triggers an alert recording, oscillates the ALS while the alert stream is
    active, then lets the stream end naturally. During the stream-end
    transition arlod's eRPC to the MCU can become temporarily not-ready
    ("Waiting for arlod eRPC to be ready" / "Waiting for arlogw to be ready"),
    stalling the queue drain and causing saturation AFTER the stream has ended.

This is a REPRODUCTION test: exit code 0 means the bug was successfully
reproduced in at least one run phase. Exit code 1 means it could not be
confirmed.

Prerequisites:
    - Device: AVD5001 (Lory FHD) or AVD6001 (Lory 2K)
    - Device claimed, ISP awake
    - Night vision mode: Auto
    - serial_mux running (MCU on port 9002, ISP on port 9001)
    - Voodoo board reachable
    - Phase A only: user must open a live stream from the Arlo app

Usage:
    python3 lsm_queue_saturation_test.py --phase both --cycles 3
    python3 lsm_queue_saturation_test.py --phase a --stress-iterations 20
    python3 lsm_queue_saturation_test.py --phase b --post-completion-time 90
"""

import argparse
import os
import sys
import time
from datetime import datetime
from enum import Enum, auto

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(line_buffering=True)

from console_utils import DeviceTestBase, SerialMuxReader
from mcu_patterns import (
    MCU_CRASH_PATTERNS, ISP_CRASH_PATTERNS,
    check_mcu_line, check_isp_line, AnomalyType,
)
from voodoo_channels import DO_AMBLIGHT, DO_PIR


class Event(Enum):
    QUEUE_SATURATED = auto()
    NIGHT_MODE = auto()
    DAY_MODE = auto()
    STREAM_ACTIVE = auto()
    STREAM_END = auto()
    ERPC_NOT_READY = auto()
    MCU_NV_NOTIFY = auto()
    MCU_DAYNIGHT_SET = auto()
    MCU_PIR = auto()
    MCU_ERPC_ERROR = auto()
    CRASH_DETECTED = auto()


class LSMQueueSaturationTest(DeviceTestBase):
    _test_name = "lsm_queue_saturation"
    _log_dir = "/tmp/lsm_queue_saturation_logs"
    _sleep_timeout = 60
    _reset_recovery_timeout = 120

    def __init__(self, cycles, stress_iterations, observation_time, wait_stream,
                 phase, post_completion_time):
        super().__init__()
        self.num_cycles = cycles
        self.stress_iterations = stress_iterations
        self.observation_time = observation_time
        self.wait_stream = wait_stream
        self.phase = phase
        self.post_completion_time = post_completion_time
        # Phase A tallies
        self.reproduced_cycles_a = 0
        self.total_saturation_events_a = 0
        self.phase_a_results = []
        # Phase B tallies
        self.reproduced_cycles_b = 0
        self.total_saturation_events_b = 0
        self.phase_b_results = []
        # Stream-state tracking for STREAM_END detection
        self._stream_was_active = False

    def _check_events(self, line, source):
        if source == "ISP":
            if "send failed for event" in line:
                self.event_callback(Event.QUEUE_SATURATED, source, line)
            elif ("Waiting for arlod eRPC to be ready" in line
                  or "Waiting for arlogw to be ready" in line):
                self.event_callback(Event.ERPC_NOT_READY, source, line)
            elif "alert_stream_completion_cb" in line:
                self._stream_was_active = False
                self.event_callback(Event.STREAM_END, source, line)
            elif "lsm_enter_night_state" in line or "lsm_process_event_night" in line:
                self.event_callback(Event.NIGHT_MODE, source, line)
            elif "lsm_enter_day_state" in line or "lsm_process_event_day" in line:
                self.event_callback(Event.DAY_MODE, source, line)
            elif "userStreamActive" in line or "alertStreamActiveWatchAlong" in line:
                self.event_callback(Event.STREAM_ACTIVE, source, line)
            elif "alertStreamActive" in line:
                # Alert stream is up; remember it so we can spot the transition away.
                self._stream_was_active = True
                self.event_callback(Event.STREAM_ACTIVE, source, line)
            elif (self._stream_was_active
                  and ("streamState" in line or "stream state" in line
                       or "StreamActive" in line)):
                # Transition away from alertStreamActive to some other stream state.
                self._stream_was_active = False
                self.event_callback(Event.STREAM_END, source, line)
            # ISP crash check
            for pat in ISP_CRASH_PATTERNS:
                if pat.lower() in line.lower():
                    self.event_callback(Event.CRASH_DETECTED, source, line)
                    return
        elif source == "MCU":
            if "NotifyNightVision" in line or "pegaERPC_NotifyNightVision" in line:
                self.event_callback(Event.MCU_NV_NOTIFY, source, line)
            elif "DayNightStateSet" in line:
                self.event_callback(Event.MCU_DAYNIGHT_SET, source, line)
            elif "PIR_TASK_INTERRUPT_TRIGGER" in line or "pegaPIR" in line:
                self.event_callback(Event.MCU_PIR, source, line)
            elif "erpc error" in line or "send failed" in line:
                self.event_callback(Event.MCU_ERPC_ERROR, source, line)
            # MCU crash check
            for pat in MCU_CRASH_PATTERNS:
                if pat in line:
                    self.event_callback(Event.CRASH_DETECTED, source, line)
                    return

    def _wait_for_stream(self):
        """Wait for live stream to become active. Returns True if detected."""
        print(f"  [1] Waiting for live stream (up to {self.wait_stream}s)...")
        print("      >>> Open a live stream from the Arlo app NOW <<<")
        result = self.wait_for_event(Event.STREAM_ACTIVE, self.wait_stream)
        if result:
            print(f"  [OK] Stream active: {result[2][:100]}")
            return True
        return False

    def _set_night_and_wait(self):
        """Set night environment and wait for night mode confirmation."""
        print("  [2] Setting night environment (closing ALS shutter)...")
        self.voodoo_on(DO_AMBLIGHT)
        time.sleep(1)

        print("  [2] Waiting for night mode (30s)...")
        result = self.wait_for_any_event(
            {Event.NIGHT_MODE, Event.MCU_DAYNIGHT_SET}, timeout=30)
        if result:
            print(f"  [OK] Night mode confirmed: {result[2][:100]}")
            return True
        print("  [WARN] Night mode not confirmed within 30s, continuing anyway")
        return False

    def _run_stress_phase(self, cycle):
        """Run rapid PIR + ALS oscillation. Returns False if crash detected."""
        print(f"  [4] Stress phase: {self.stress_iterations} iterations...")

        for i in range(self.stress_iterations):
            # PIR trigger
            self.press_button(DO_PIR, 0.3)
            time.sleep(0.5)

            # Flip to day
            self.voodoo_off(DO_AMBLIGHT)
            time.sleep(0.8)

            # Another PIR trigger
            self.press_button(DO_PIR, 0.3)
            time.sleep(0.5)

            # Flip back to night
            self.voodoo_on(DO_AMBLIGHT)
            time.sleep(0.8)

            # Check for crash mid-stress
            crash = self.check_event(Event.CRASH_DETECTED)
            if crash:
                print(f"  [CRASH] Detected at iteration {i+1}: {crash[2][:100]}")
                self.save_logs(cycle, "crash")
                return False

            # Progress indicator every 5 iterations
            if (i + 1) % 5 == 0:
                sat = self.check_event(Event.QUEUE_SATURATED)
                sat_indicator = " [SATURATED!]" if sat else ""
                print(f"      iteration {i+1}/{self.stress_iterations}{sat_indicator}")

        return True

    def _evaluate_cycle(self, cycle):
        """Stop recording, count events, print summary. Returns True if reproduced."""
        self.mcu.stop_recording()
        self.isp.stop_recording()

        isp_lines = self.isp.get_lines()
        mcu_lines = self.mcu.get_lines()

        # Count ISP events
        queue_saturated_count = sum(
            1 for l in isp_lines if "send failed for event" in l)
        isp_night_count = sum(
            1 for l in isp_lines
            if "lsm_enter_night_state" in l or "lsm_process_event_night" in l)
        isp_day_count = sum(
            1 for l in isp_lines
            if "lsm_enter_day_state" in l or "lsm_process_event_day" in l)

        # Count MCU events
        mcu_nv_notify_count = sum(
            1 for l in mcu_lines
            if "NotifyNightVision" in l or "pegaERPC_NotifyNightVision" in l)
        mcu_daynight_set_count = sum(
            1 for l in mcu_lines if "DayNightStateSet" in l)
        mcu_pir_count = sum(
            1 for l in mcu_lines
            if "PIR_TASK_INTERRUPT_TRIGGER" in l or "pegaPIR" in l)
        mcu_erpc_error_count = sum(
            1 for l in mcu_lines
            if "erpc error" in l or "send failed" in l)

        reproduced = queue_saturated_count > 0
        verdict = "REPRODUCED" if reproduced else "NOT REPRODUCED"

        print(f"\n  [CYCLE {cycle} SUMMARY - PHASE A]")
        print(f"    ISP: queue saturated = {queue_saturated_count}, "
              f"night transitions = {isp_night_count}, "
              f"day transitions = {isp_day_count}")
        print(f"    MCU: NV notifications = {mcu_nv_notify_count}, "
              f"DayNightStateSet = {mcu_daynight_set_count}, "
              f"PIR events = {mcu_pir_count}, "
              f"eRPC errors = {mcu_erpc_error_count}")
        print(f"    Verdict: {verdict}")

        if reproduced:
            self.reproduced_cycles_a += 1
            self.total_saturation_events_a += queue_saturated_count
            self.save_logs(cycle, "reproduced_a")

        return reproduced

    def run_cycle_phase_a(self, cycle):
        """Run a single Phase A stress cycle. Returns True if reproduced."""
        print(f"\n{'='*60}")
        print(f"[PHASE A CYCLE {cycle}/{self.num_cycles}] "
              f"[{datetime.now().strftime('%H:%M:%S')}]")
        print(f"{'='*60}")

        # Step 1: Wait for stream active
        if not self._wait_for_stream():
            if cycle == 1:
                print("\n  [ABORT] No live stream detected within timeout.")
                print("  Please open a live stream from the Arlo app and re-run.")
                return False
            else:
                print("  [WARN] Stream not detected, proceeding anyway "
                      "(may already be active)")

        # Step 2-3: Set night environment and wait
        self._set_night_and_wait()

        # Step 4: Start recording
        print("  [3] Starting recording...")
        self.clear_events()
        self.mcu.clear_lines()
        self.isp.clear_lines()
        self.mcu.start_recording()
        self.isp.start_recording()

        # Step 5: Stress phase
        stress_ok = self._run_stress_phase(cycle)
        if not stress_ok:
            self.mcu.stop_recording()
            self.isp.stop_recording()
            return False

        # Step 6: Observation window
        print(f"  [5] Observation window ({self.observation_time}s)...")
        time.sleep(self.observation_time)

        # Step 7-8: Evaluate
        reproduced = self._evaluate_cycle(cycle)

        # Step 9: Cleanup
        print("  [6] Cleanup: restoring day environment...")
        self.voodoo_off(DO_AMBLIGHT)
        time.sleep(10)

        return reproduced

    # --- Phase B ---

    def _drain_phase_b_events(self, seen):
        """Drain the event queue, recording first-seen wall-clock times.

        Mutates `seen` in place: first timestamps for saturation / erpc-not-ready
        / stream-end, running counts, and any crash line.
        """
        while True:
            evt = None
            with self.event_lock:
                if self.events:
                    evt = self.events.pop(0)
            if evt is None:
                break
            event_type, _src, line = evt
            now = time.time()
            if event_type == Event.QUEUE_SATURATED:
                seen["saturation_count"] += 1
                if seen["saturation_time"] is None:
                    seen["saturation_time"] = now
                    seen["saturation_line"] = line
            elif event_type == Event.ERPC_NOT_READY:
                seen["erpc_count"] += 1
                if seen["erpc_time"] is None:
                    seen["erpc_time"] = now
                    seen["erpc_line"] = line
            elif event_type == Event.STREAM_END:
                if seen["stream_end_time"] is None:
                    seen["stream_end_time"] = now
                    seen["stream_end_line"] = line
            elif event_type == Event.CRASH_DETECTED:
                if seen["crash"] is None:
                    seen["crash"] = line

    def _evaluate_cycle_phase_b(self, cycle, seen, ref):
        """Print Phase B summary and return True if Trigger B reproduced.

        `ref` is the wall-clock reference (recording start) used for relative
        timing display.
        """
        self.mcu.stop_recording()
        self.isp.stop_recording()

        saturation = seen["saturation_time"] is not None
        erpc = seen["erpc_time"] is not None
        stream_end = seen["stream_end_time"] is not None

        # Determine whether saturation occurred before or after the stream end.
        saturation_after = None
        if saturation and stream_end:
            saturation_after = seen["saturation_time"] >= seen["stream_end_time"]

        # Trigger B is confirmed when saturation follows the stream-end
        # transition, or (when the stream-end line was not matched) when both
        # saturation and eRPC-not-ready appear together in the post window.
        trigger_b = False
        if saturation and stream_end and saturation_after:
            trigger_b = True
        elif saturation and erpc and not stream_end:
            trigger_b = True

        def rel(t):
            return f"+{t - ref:.1f}s" if t is not None else "n/a"

        print(f"\n  [CYCLE {cycle} SUMMARY - PHASE B]")
        print(f"    Queue saturation events:  {seen['saturation_count']} "
              f"(first at {rel(seen['saturation_time'])})")
        print(f"    eRPC-not-ready events:    {seen['erpc_count']} "
              f"(first at {rel(seen['erpc_time'])})")
        print(f"    Stream end detected:      "
              f"{'yes' if stream_end else 'no'} (at {rel(seen['stream_end_time'])})")

        if saturation and stream_end:
            delta = seen["saturation_time"] - seen["stream_end_time"]
            when = "AFTER" if saturation_after else "BEFORE"
            print(f"    Saturation vs stream end: {when} "
                  f"(delta {delta:+.1f}s)")
        elif saturation:
            print(f"    Saturation vs stream end: unknown "
                  f"(stream-end line not matched)")

        if trigger_b:
            if erpc:
                verdict = "REPRODUCED (Trigger B, strong: eRPC-not-ready present)"
            else:
                verdict = "REPRODUCED (Trigger B)"
        elif saturation and stream_end and saturation_after is False:
            verdict = "NOT REPRODUCED (saturation before stream end - Trigger A-like)"
        else:
            verdict = "NOT REPRODUCED"
        print(f"    Verdict: {verdict}")

        if trigger_b:
            self.reproduced_cycles_b += 1
            self.total_saturation_events_b += seen["saturation_count"]
            self.save_logs(cycle, "reproduced_b")

        return trigger_b

    def run_cycle_phase_b(self, cycle):
        """Run a single Phase B stream-end transition cycle. Returns True if reproduced."""
        print(f"\n{'='*60}")
        print(f"[PHASE B CYCLE {cycle}/{self.num_cycles}] "
              f"[{datetime.now().strftime('%H:%M:%S')}]")
        print(f"{'='*60}")

        seen = {
            "saturation_time": None, "saturation_line": None, "saturation_count": 0,
            "erpc_time": None, "erpc_line": None, "erpc_count": 0,
            "stream_end_time": None, "stream_end_line": None,
            "crash": None,
        }

        # Step 1: Set night environment and wait for night mode.
        self._stream_was_active = False
        self._set_night_and_wait()

        # Step 2: Trigger an alert recording (PIR pulse), then start recording.
        print("  [3] Triggering alert recording (PIR pulse)...")
        self.clear_events()
        self.mcu.clear_lines()
        self.isp.clear_lines()
        self.mcu.start_recording()
        self.isp.start_recording()
        self.press_button(DO_PIR, 0.3)

        # Step 3: Wait briefly for the alert stream to start.
        result = self.wait_for_event(Event.STREAM_ACTIVE, 2)
        if result:
            print(f"  [OK] Alert stream active: {result[2][:100]}")
        else:
            print("  [WARN] Alert stream not detected within 2s, continuing anyway")
            self._stream_was_active = True

        # Step 4: Oscillate ALS during the active alert stream (~10s window).
        print("  [4] Oscillating ALS during active stream (~10s)...")
        ref = time.time()
        osc_iterations = 5
        for i in range(osc_iterations):
            self.voodoo_off(DO_AMBLIGHT)
            time.sleep(0.8)
            self._drain_phase_b_events(seen)
            self.voodoo_on(DO_AMBLIGHT)
            time.sleep(0.8)
            self._drain_phase_b_events(seen)
            if seen["crash"]:
                print(f"  [CRASH] Detected during oscillation: {seen['crash'][:100]}")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                self.save_logs(cycle, "crash_b")
                return False
            print(f"      oscillation {i+1}/{osc_iterations}")

        # Step 5: Stop oscillating and let the stream end naturally.
        print("  [5] Oscillation stopped; letting stream end naturally...")

        # Step 6: Post-completion observation window.
        print(f"  [6] Post-completion observation ({self.post_completion_time}s)...")
        deadline = time.time() + self.post_completion_time
        while time.time() < deadline:
            self._drain_phase_b_events(seen)
            if seen["crash"]:
                print(f"  [CRASH] Detected post-completion: {seen['crash'][:100]}")
                self.mcu.stop_recording()
                self.isp.stop_recording()
                self.save_logs(cycle, "crash_b")
                return False
            time.sleep(0.5)
        self._drain_phase_b_events(seen)

        # Step 7: Evaluate.
        reproduced = self._evaluate_cycle_phase_b(cycle, seen, ref)

        # Step 8: Cleanup.
        print("  [7] Cleanup: restoring day environment...")
        self.voodoo_off(DO_AMBLIGHT)
        time.sleep(10)

        return reproduced

    def _run_phase_cycles(self, phase_label, cycle_fn, results):
        """Run num_cycles of a phase, appending outcomes to `results`.

        Returns False if the run aborted early (e.g. crash without recovery).
        """
        for cycle in range(1, self.num_cycles + 1):
            reproduced = cycle_fn(cycle)
            results.append(reproduced)

            # Phase A cycle 1 with no stream is a hard abort.
            if phase_label == "A" and cycle == 1 and not reproduced:
                crash = self.check_event(Event.CRASH_DETECTED)
                if not crash and not any(results):
                    # Distinguish "no stream" abort from a genuine miss: the
                    # cycle prints its own ABORT message; nothing more to do.
                    pass

            crash = self.check_event(Event.CRASH_DETECTED)
            if crash:
                print(f"  [CRASH] Detected post-cycle: {crash[2][:100]}")
                self.save_logs(cycle, f"crash_postcycle_{phase_label.lower()}")
                if not self.recovery(cycle):
                    print("  [ABORT] Cannot recover, stopping")
                    return False
        return True

    def run(self):
        """Main test loop. Exit 0 = reproduced in a run phase, exit 1 = not."""
        os.makedirs(self._log_dir, exist_ok=True)

        run_a = self.phase in ("a", "both")
        run_b = self.phase in ("b", "both")

        print(f"=== PEGA-1697: LSM Queue Saturation Test ===")
        print(f"  Phase:                {self.phase}")
        print(f"  Cycles:               {self.num_cycles}")
        if run_a:
            print(f"  Stress iterations:    {self.stress_iterations}")
            print(f"  Observation time:     {self.observation_time}s")
            print(f"  Stream wait:          {self.wait_stream}s")
        if run_b:
            print(f"  Post-completion time: {self.post_completion_time}s")
        print(f"  Log directory:        {self._log_dir}")
        print()

        print("[INIT] Connecting consoles...")
        self.connect_consoles()

        print("[INIT] Initializing ISP console (login + tail -f)...")
        self.init_isp_console()

        print("[INIT] Draining stale buffers...")
        time.sleep(2)
        self.mcu.clear_lines()
        self.isp.clear_lines()
        self.clear_events()
        print("[INIT] Ready")
        print()

        if run_a:
            self._run_phase_cycles("A", self.run_cycle_phase_a, self.phase_a_results)

        if run_b:
            self.clear_events()
            self._run_phase_cycles("B", self.run_cycle_phase_b, self.phase_b_results)

        self.disconnect_consoles()

        # Overall summary
        print(f"\n{'='*60}")
        print(f"PEGA-1697 LSM QUEUE SATURATION - OVERALL SUMMARY")
        print(f"{'='*60}")
        if run_a:
            print(f"  Phase A: {self.reproduced_cycles_a}/{len(self.phase_a_results)} "
                  f"cycles reproduced (Trigger A) "
                  f"[{self.total_saturation_events_a} saturation events]")
            for i, r in enumerate(self.phase_a_results, 1):
                status = "REPRODUCED" if r else "NOT REPRODUCED"
                print(f"    Phase A Cycle {i}: {status}")
        if run_b:
            print(f"  Phase B: {self.reproduced_cycles_b}/{len(self.phase_b_results)} "
                  f"cycles reproduced (Trigger B) "
                  f"[{self.total_saturation_events_b} saturation events]")
            for i, r in enumerate(self.phase_b_results, 1):
                status = "REPRODUCED" if r else "NOT REPRODUCED"
                print(f"    Phase B Cycle {i}: {status}")
        print(f"  Log directory: {self._log_dir}")
        print(f"{'='*60}")

        any_reproduced = self.reproduced_cycles_a > 0 or self.reproduced_cycles_b > 0
        if any_reproduced:
            print(f"\nRESULT: BUG REPRODUCED")
            if run_a:
                print(f"  Phase A: {self.reproduced_cycles_a}/"
                      f"{len(self.phase_a_results)} cycles (Trigger A)")
            if run_b:
                print(f"  Phase B: {self.reproduced_cycles_b}/"
                      f"{len(self.phase_b_results)} cycles (Trigger B)")
            return 0
        else:
            print(f"\nRESULT: NOT REPRODUCED")
            print("  Suggestions:")
            if run_a:
                print("  - Phase A: ensure a live stream is active during the "
                      "entire stress phase")
                print("  - Phase A: try more stress iterations "
                      "(--stress-iterations 25)")
            if run_b:
                print("  - Phase B: increase --post-completion-time to observe "
                      "later saturation")
                print("  - Phase B: ensure the alert stream actually starts "
                      "(PIR must be armed)")
            print("  - Ensure ALS is near the day/night threshold")
            print("  - Try more cycles (--cycles 5)")
            return 1


def main():
    parser = argparse.ArgumentParser(
        description="PEGA-1697: LSM event queue saturation reproduction test. "
                    "Phase A drives PIR + ALS oscillation during a live stream; "
                    "Phase B drives ALS oscillation into the alert stream-end "
                    "transition to overflow the LSM event queue on Lory.")
    parser.add_argument("--phase", choices=["a", "b", "both"], default="both",
                        help="Which trigger to exercise: a (live-stream), "
                             "b (stream-end), or both (default: both)")
    parser.add_argument("--cycles", type=int, default=3,
                        help="Number of cycles per selected phase (default: 3)")
    parser.add_argument("--stress-iterations", type=int, default=15,
                        help="Phase A PIR+ALS toggles per cycle (default: 15)")
    parser.add_argument("--observation-time", type=int, default=30,
                        help="Phase A seconds to watch after stress phase "
                             "(default: 30)")
    parser.add_argument("--wait-stream", type=int, default=120,
                        help="Phase A max seconds to wait for stream active "
                             "(default: 120)")
    parser.add_argument("--post-completion-time", type=int, default=60,
                        help="Phase B seconds to observe after stream ends "
                             "(default: 60)")
    args = parser.parse_args()

    test = LSMQueueSaturationTest(
        cycles=args.cycles,
        stress_iterations=args.stress_iterations,
        observation_time=args.observation_time,
        wait_stream=args.wait_stream,
        phase=args.phase,
        post_completion_time=args.post_completion_time,
    )
    sys.exit(test.run())


if __name__ == "__main__":
    main()
