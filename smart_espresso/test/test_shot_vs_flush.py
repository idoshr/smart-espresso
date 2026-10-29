"""Flushes, rinses and backflushes are not shots. A bloom pause is one shot."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import ShotHistory
from smart_espresso.test.test_status_server import FakeADC, FakeClock
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer

LOGIC = Path(__file__).resolve().parents[1] / "web" / "static" / "dashboard_logic.js"


def _live_presentation(machine, head_bar):
    script = (
        "const logic = require(%s);\n" % json.dumps(str(LOGIC))
        + "const value = logic.livePresentation(%s, %s);\n"
        % (json.dumps(machine), json.dumps(head_bar))
        + "process.stdout.write(JSON.stringify(value));\n"
    )
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


class TestShotVersusFlush(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("smart_espresso.machine_state.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fresh()

    def fresh(self):
        self.history = ShotHistory(state_path=None, now=lambda: 1_700_000_000.0)
        self.tank = WaterTank(state_path=None)
        self.classifier = MachineStateClassifier(shot_history=self.history)

    def settle(self, head, total, seconds=3.0, step=0.5):
        for _ in range(int(seconds / step)):
            self.classifier.update(head, 1.2, flow_mls=0.0, total_ml=total)
            self.tank.update(total)
            self.clock.advance(step)
        return self.classifier.update(head, 1.2, flow_mls=0.0, total_ml=total)

    def play(self, segments):
        """Play (seconds, head bar, ml/s) segments, then vent the head."""
        self.settle(0.0, 0.0)
        total = 0.0
        for seconds, head, flow in segments:
            for _ in range(int(seconds)):
                total += flow
                self.clock.advance(1.0)
                self.classifier.update(head, 1.2, flow_mls=flow, total_ml=total)
                self.tank.update(total)
        self.settle(0.0, total)
        return total

    def test_maintenance_cycles_are_not_shots_and_a_bloom_is_one(self):
        # (name, segments, saved shots). Volumes are chosen so today's rule
        # (any flow longer than 2 s) records the maintenance cycles, and so
        # the 7 s bloom outlasts the 5 s hold plus the 0.8 s dwell.
        cases = (
            ("group flush", [(4, 0.4, 8.0)], 0),
            ("rinse", [(3, 0.4, 1.5)], 0),
            ("backflush", [(4, 9.0, 0.5)], 0),
            ("bloom", [(4, 3.0, 4.0), (7, 3.0, 0.0), (25, 9.0, 1.8)], 1),
            ("normal pull", [(27, 9.0, 1.4)], 1),
            ("two pulls vented between them", None, 2),
        )
        for name, segments, shots in cases:
            with self.subTest(name=name):
                self.fresh()
                if segments is None:
                    self.play([(27, 9.0, 1.4)])
                    self.play([(24, 9.0, 1.4)])
                else:
                    total = self.play(segments)
                    if shots == 0:
                        self.assertGreater(self.tank.used_ml, 0.0)
                        self.assertAlmostEqual(self.tank.used_ml, total, places=1)
                snapshot = self.classifier.snapshot()
                self.assertEqual(self.history.list_payload()["count"], shots)
                self.assertEqual(snapshot["today"]["shots"], shots)
                if name == "bloom":
                    shot = self.history.get(1)
                    self.assertGreater(shot["seconds"], 20.0)
                    self.assertGreater(shot["ml"], 40.0)

    def test_readme_state_table_matches_the_classifier(self):
        readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
        self.assertNotIn("Flow ≥ 0.3 ml/s **or** head ≥ 2 bar", readme)
        brewing = next(
            line for line in readme.splitlines() if line.startswith("| `brewing`")
        )
        self.assertIn("0.3 ml/s", brewing)
        self.assertIn("4.5 bar", brewing)
        self.assertIn("Pulling a shot", brewing)
        self.assertIn("5 ml", readme)
        self.assertIn("0.5 bar", readme)
        self.assertIn("10 s", readme)


class TestFlushFeedsTheTank(unittest.TestCase):
    def test_group_flush_through_the_server_is_not_a_shot(self):
        clock = FakeClock()
        patchers = [
            mock.patch("smart_espresso.machine_state.monotonic", clock),
            mock.patch("smart_espresso.web.status_server.monotonic", clock),
            mock.patch(
                "smart_espresso.analog_sensor.water_flow_sensor.monotonic", clock
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        head_adc = FakeADC(0.5)
        boiler_adc = FakeADC(1.2)
        flow_adc = FakeADC(0.0)
        head = PressureAnalogSensor(adc=head_adc, name="Head", max_pressure_mpa=2.0)
        boiler = PressureAnalogSensor(
            adc=boiler_adc, name="Boiler", max_pressure_mpa=0.5
        )
        flow = WaterFlowAnalogSensor(
            adc=flow_adc, name="Brew", pulses_per_liter=1000, flow_interval=0.2
        )
        server = StatusServer(
            port=8095,
            tank=WaterTank(state_path=None),
            shot_history=ShotHistory(state_path=None),
        )

        def tick():
            head.read()
            boiler.read()
            flow.read()
            server.update([head, boiler, flow])

        for _ in range(6):
            tick()
            clock.advance(0.5)

        # ~10 ml/s for about 4 s with the head at 0.4 bar. Every tick carries
        # the pressure sensors too: the render loop never samples flow alone.
        head_adc.set(0.58)
        flushing = None
        for _ in range(40):
            for voltage in (3.0, 0.0):
                flow_adc.set(voltage)
                tick()
                clock.advance(0.05)
                flushing = server.status_payload()

        self.assertIsNotNone(flushing)
        head_bar = next(
            item["bar"] for item in flushing["pressure"] if item["name"] == "Head"
        )
        self.assertLess(head_bar, 2.0)
        self.assertEqual(flushing["machine"]["state"], "brewing")
        presented = _live_presentation(flushing["machine"], head_bar)
        self.assertEqual(presented["label"], "Flushing")
        self.assertFalse(presented["showShot"])
        self.assertFalse(presented["showTarget"])
        self.assertNotIn("shot", presented["label"].lower())

        head_adc.set(0.5)
        for _ in range(8):
            tick()
            clock.advance(0.5)

        self.assertGreater(server.tank.used_ml, 5.0)
        self.assertEqual(server.shot_history.list_payload()["count"], 0)
        self.assertEqual(server.classifier.snapshot()["today"]["shots"], 0)


if __name__ == "__main__":
    unittest.main()
