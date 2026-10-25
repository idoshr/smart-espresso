import unittest
from time import localtime
from unittest import mock

from smart_espresso.machine_state import MachineState, MachineStateClassifier


class FakeClock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self, start=500.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestMachineStateClassifier(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("smart_espresso.machine_state.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.classifier = MachineStateClassifier()

    def settle(self, head, boiler, flow=0.0, total_ml=0.0, seconds=3.0, step=0.5):
        """Hold one reading long enough for the dwell filter to publish it."""
        for _ in range(int(seconds / step)):
            self.classifier.update(head, boiler, flow, total_ml)
            self.clock.advance(step)
        return self.classifier.update(head, boiler, flow, total_ml)

    def test_off_when_boiler_has_no_pressure(self):
        self.assertEqual(self.settle(0.0, 0.02), MachineState.OFF)

    def test_heating_below_the_ready_band(self):
        self.assertEqual(self.settle(0.0, 0.4), MachineState.HEATING)

    def test_ready_inside_the_band(self):
        self.assertEqual(self.settle(0.0, 1.2), MachineState.READY)

    def test_overpressure_above_the_band(self):
        self.assertEqual(self.settle(0.0, 1.9), MachineState.OVERPRESSURE)

    def test_brewing_starts_on_flow_and_holds_on_head_pressure(self):
        self.settle(0.0, 1.2)
        self.assertEqual(
            self.classifier.update(9.1, 1.2, flow_mls=1.4), MachineState.BREWING
        )
        # Pump off: the puck blocks up, the line stays charged — still a shot.
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.BREWING)

    def test_static_brew_band_pressure_is_not_a_shot(self):
        # An idle heat-exchanger machine sits just above the brew band on
        # static pressure (HX thermal expansion, sensor zero offset) with
        # zero flow. This used to read "Pulling shot" around the clock and
        # record 0 ml "shots".
        self.assertEqual(self.settle(2.06, 1.05), MachineState.READY)
        snapshot = self.classifier.snapshot()
        self.assertIsNone(snapshot["last_shot"])
        self.assertIsNone(snapshot["shot"])

    def test_brew_band_pressure_expires_after_the_hold_window(self):
        self.settle(0.0, 1.2)
        self.classifier.update(9.1, 1.2, flow_mls=1.4)
        # Right after the water stops, brew-band pressure still reads brewing...
        self.clock.advance(5.0)
        self.assertEqual(self.classifier.update(2.2, 1.2), MachineState.BREWING)
        # ...but past the hold window it is only static pressure again.
        self.clock.advance(30.0)
        self.classifier.update(2.2, 1.2)  # candidate flips to the boiler state
        self.clock.advance(2.0)  # dwell so it is published
        self.assertEqual(self.classifier.update(2.2, 1.2), MachineState.READY)
        # No water moved, so nothing is recorded as a shot either.
        self.assertIsNone(self.classifier.snapshot()["last_shot"])

    def test_pump_pressure_without_flow_falls_back_to_the_boiler(self):
        # A pump working against a fully blocked puck builds pump-level
        # pressure with the flow meter seeing nothing — even started from
        # idle, that is a shot...
        self.settle(0.0, 1.2)
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.BREWING)
        # ...but a sensor holding pump-level pressure for 5 minutes with no
        # water moving is stuck, and the state falls back to the boiler's.
        self.clock.advance(301.0)
        self.classifier.update(9.1, 1.2)  # candidate flips to the boiler state
        self.clock.advance(2.0)  # dwell so it is published
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.READY)
        self.assertIsNone(self.classifier.snapshot()["last_shot"])

    def test_stuck_sensor_fault_clears_when_pressure_drops(self):
        self.classifier.update(9.1, 1.2)
        self.clock.advance(301.0)
        self.classifier.update(9.1, 1.2)
        self.clock.advance(2.0)
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.READY)

        # The latch only releases when the pressure itself drops below pump
        # level; a fresh pump-level episode is trusted again for a full
        # window after that.
        self.clock.advance(1.0)
        self.assertEqual(self.classifier.update(2.2, 1.2), MachineState.READY)
        self.clock.advance(1.0)
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.BREWING)

    def test_stuck_head_sensor_without_a_boiler_reads_unknown(self):
        # Head-only setup: with no boiler to fall back on, a sensor holding
        # pump-level pressure for 5 minutes with no water is reported as
        # no reading, not as a confident ready.
        self.classifier.update(9.1, None)
        self.clock.advance(301.0)
        self.classifier.update(9.1, None)  # candidate flips to UNKNOWN
        self.clock.advance(2.0)  # dwell so it is published
        self.assertEqual(self.classifier.update(9.1, None), MachineState.UNKNOWN)

    def test_shot_timer_excludes_a_pause_with_the_line_charged(self):
        self.settle(0.0, 1.2)

        total = 0.0
        for _ in range(10):  # 10 s pull at 1.4 ml/s
            total += 1.4
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=total)

        for _ in range(8):  # 8 s no-flow pause, pump pressure still up
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, total_ml=total)

        for _ in range(10):  # 10 more seconds of flow
            total += 1.4
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=total)

        # The recorded pull is the flowing time only (20 s of samples minus
        # the opening tick), not 20 s plus the 8 s pause.
        shot = self.classifier.snapshot()["shot"]
        self.assertEqual(self.classifier.state, MachineState.BREWING)
        self.assertAlmostEqual(shot["seconds"], 19.0, places=1)
        self.assertAlmostEqual(shot["ml"], 26.6, places=1)

    def test_classifier_recovers_after_the_sensor_fault_clears(self):
        self.classifier.update(9.1, 1.2)
        self.clock.advance(301.0)
        self.classifier.update(9.1, 1.2)
        self.clock.advance(2.0)
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.READY)

        # Sensor unplugged / pressure released: back to the ready band.
        self.clock.advance(1.0)
        self.assertEqual(self.settle(0.0, 1.2), MachineState.READY)

    def test_brewing_with_flow_never_falls_back_to_unknown(self):
        self.settle(0.0, 1.2)
        for _ in range(320):
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=0.0)
        self.assertEqual(self.classifier.state, MachineState.BREWING)

    def test_brewing_on_flow_alone(self):
        self.settle(0.0, 1.2)
        self.assertEqual(
            self.classifier.update(0.1, 1.2, flow_mls=1.8), MachineState.BREWING
        )

    def test_steaming_when_boiler_falls_fast(self):
        self.settle(0.0, 1.3)
        # Draw the boiler down quickly, as opening the steam valve does.
        boiler = 1.3
        for _ in range(8):
            boiler -= 0.05
            self.clock.advance(0.5)
            self.classifier.update(0.0, boiler)
        self.assertEqual(self.classifier.state, MachineState.STEAMING)

    def test_dwell_filter_ignores_a_single_noisy_sample(self):
        self.settle(0.0, 1.2)
        self.clock.advance(0.1)
        self.classifier.update(0.0, 1.9)  # one over-pressure blip
        self.assertEqual(self.classifier.state, MachineState.READY)

    def test_shot_timer_records_seconds_and_volume(self):
        self.settle(0.0, 1.2)

        total = 0.0
        for _ in range(27):  # 27 s pull at ~1.4 ml/s
            total += 1.4
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=total)

        shot = self.classifier.snapshot()["shot"]
        self.assertEqual(self.classifier.state, MachineState.BREWING)
        self.assertAlmostEqual(shot["seconds"], 26.0, places=1)
        self.assertAlmostEqual(shot["ml"], 36.4, places=1)

        # Pump off: the shot closes and is kept as the last shot.
        self.settle(0.0, 1.2, total_ml=total)
        # The recorded duration must be the pull itself, not the pull plus the
        # dwell window that keeps BREWING published after the pump stops.
        snapshot = self.classifier.snapshot()
        self.assertIsNone(snapshot["shot"])
        self.assertAlmostEqual(snapshot["last_shot"]["seconds"], 26.0, places=1)
        self.assertAlmostEqual(snapshot["last_shot"]["ml"], 36.4, places=1)

    def test_short_blip_is_not_recorded_as_a_shot(self):
        self.settle(0.0, 1.2)
        self.classifier.update(9.0, 1.2, flow_mls=1.4)
        self.clock.advance(0.5)
        self.classifier.update(9.0, 1.2, flow_mls=1.4)
        self.settle(0.0, 1.2)
        self.assertIsNone(self.classifier.snapshot()["last_shot"])

    def pull(self, seconds, rate_mls=1.4):
        """Run one qualifying pull and close it, from a ready machine."""
        self.settle(0.0, 1.2)
        total = 0.0
        for _ in range(seconds):
            total += rate_mls
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=rate_mls, total_ml=total)
        self.settle(0.0, 1.2, total_ml=total)

    def test_shot_history_keeps_the_recent_pulls(self):
        for seconds in (27, 24, 29):
            self.pull(seconds)

        history = self.classifier.snapshot()["history"]
        self.assertEqual([shot["seconds"] for shot in history], [26.0, 23.0, 28.0])
        self.assertEqual(self.classifier.snapshot()["last_shot"], history[-1])

    def test_shot_history_is_capped(self):
        for _ in range(MachineStateClassifier.SHOT_HISTORY_MAX + 3):
            self.pull(27)

        history = self.classifier.snapshot()["history"]
        self.assertEqual(len(history), MachineStateClassifier.SHOT_HISTORY_MAX)
        # Oldest first, and the cap trims from the front.
        self.assertEqual(
            [shot["seconds"] for shot in history],
            [26.0] * MachineStateClassifier.SHOT_HISTORY_MAX,
        )

    def test_daily_tally_counts_completed_pulls(self):
        self.pull(27)
        self.pull(24)

        today = self.classifier.snapshot()["today"]
        self.assertEqual(today["shots"], 2)
        # 26 s and 23 s of flowing time at 1.4 ml/s: the pull volumes only.
        self.assertAlmostEqual(today["ml"], (26.0 + 23.0) * 1.4, places=1)

    def test_daily_tally_resets_at_local_midnight(self):
        self.pull(27)
        self.assertEqual(self.classifier.snapshot()["today"]["shots"], 1)

        # The wall clock crosses local midnight between two render ticks;
        # the next update() starts a fresh day (the stamp is only compared,
        # so an out-of-range day number is fine for the test).
        stamp = localtime()[:3]
        next_day = (stamp[0], stamp[1], stamp[2] + 1)
        with mock.patch(
            "smart_espresso.machine_state.localtime", return_value=next_day
        ):
            self.classifier.update(0.0, 1.2)

        today = self.classifier.snapshot()["today"]
        self.assertEqual(today["shots"], 0)
        self.assertEqual(today["ml"], 0.0)

    def test_unknown_without_pressure_sensors(self):
        self.assertEqual(self.classifier.update(None, None), MachineState.UNKNOWN)

    def test_snapshot_carries_label_and_severity(self):
        self.settle(0.0, 1.2)
        snapshot = self.classifier.snapshot()
        self.assertEqual(snapshot["label"], "Ready to pull")
        self.assertEqual(snapshot["severity"], "good")


if __name__ == "__main__":
    unittest.main()
