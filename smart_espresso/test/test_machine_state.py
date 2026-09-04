import unittest
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

    def test_brewing_on_head_pressure(self):
        self.settle(0.0, 1.2)
        self.assertEqual(self.classifier.update(9.1, 1.2), MachineState.BREWING)

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
        snapshot = self.classifier.snapshot()
        self.assertIsNone(snapshot["shot"])
        self.assertAlmostEqual(snapshot["last_shot"]["ml"], 36.4, places=1)

    def test_short_blip_is_not_recorded_as_a_shot(self):
        self.settle(0.0, 1.2)
        self.classifier.update(9.0, 1.2, flow_mls=1.4)
        self.clock.advance(0.5)
        self.classifier.update(9.0, 1.2, flow_mls=1.4)
        self.settle(0.0, 1.2)
        self.assertIsNone(self.classifier.snapshot()["last_shot"])

    def test_unknown_without_pressure_sensors(self):
        self.assertEqual(self.classifier.update(None, None), MachineState.UNKNOWN)

    def test_snapshot_carries_label_and_severity(self):
        self.settle(0.0, 1.2)
        snapshot = self.classifier.snapshot()
        self.assertEqual(snapshot["label"], "Ready to pull")
        self.assertEqual(snapshot["severity"], "good")


if __name__ == "__main__":
    unittest.main()
