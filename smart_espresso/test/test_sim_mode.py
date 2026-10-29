"""The simulator serves the real status API without a Pi."""

import unittest
from pathlib import Path

from smart_espresso.sim.machine import Segment, SimulatedMachine
from smart_espresso.sim.session import build_sim_app, collapsed, drive, virtual_clock
from smart_espresso.test.test_python_support import _run_python

ROOT = Path(__file__).resolve().parents[2]


def _short_program():
    """Fault, then one pull long enough to be saved."""
    segments = []
    cursor = 0.0

    def add(duration, **kwargs):
        nonlocal cursor
        segments.append(Segment(start=cursor, end=cursor + duration, **kwargs))
        cursor += duration

    add(0.4, kind="fault", boiler_bar=1.2, fault=True)
    add(2.0, kind="ready", boiler_bar=1.2)
    add(8.0, kind="shot", head_bar=9.0, boiler_bar=1.2, flow_mls=2.0)
    add(3.0, kind="settle", boiler_bar=1.2, head_bar=0.0, flow_mls=0.0)
    return segments


class TestSimMode(unittest.TestCase):
    def test_importing_sim_does_not_load_hardware(self):
        script = r"""
import sys

class _Block:
    def find_spec(self, fullname, path, target=None):
        blocked = (
            "board",
            "busio",
            "gpiozero",
            "spidev",
            "Adafruit_DHT",
            "adafruit_dht",
            "adafruit_ads1x15",
            "luma",
            "luma.core",
            "luma.oled",
            "luma.core.render",
        )
        if fullname in blocked or fullname.startswith("luma."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, _Block())
import main
assert main.client_ha is None
sensors = main.build_analog_devices(adc_type="SIM", flow_sample_interval=None)
assert [s.name for s in sensors] == ["Head", "Boiler", "Brew"]
assert sensors[2].sample_interval is None
print("sim-ok")
"""
        result = _run_python(
            script,
            env={
                "HA_ENABLE": "false",
                "DHT_ENABLE": "false",
                "WEB_ENABLE": "false",
                "DISPLAY_ENABLE": "false",
                "ADC_TYPE": "SIM",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("sim-ok", result.stdout)

    def test_simulated_shot_shows_on_the_status_api(self):
        machine = SimulatedMachine(seed=59, segments=_short_program(), loop=False)
        with virtual_clock() as clock:
            app = build_sim_app(machine=machine, sample_interval=None, port=0)
            states = drive(app, clock, machine.duration)
            payload = app.web_server.app.test_client().get("/api/status").get_json()
            shots = app.web_server.shot_history.list_payload()
            ha = {state.entity_id for state in app._ha_states()}

        self.assertIn("brewing", states)
        self.assertEqual(
            [row["name"] for row in payload["pressure"]], ["Head", "Boiler"]
        )
        self.assertTrue(payload["pressure"][0]["health"]["ok"])
        self.assertGreater(app._health["Head"].error_count, 0)
        self.assertIn("simulated", app._health["Head"].last_error.lower())
        self.assertEqual(shots["count"], 1)
        shot = shots["shots"][0]
        self.assertGreaterEqual(shot["seconds"], 2.0)
        self.assertGreaterEqual(shot["ml"], 5.0)
        self.assertIn("sensor.espresso_machine_state", ha)
        self.assertIn("sensor.espresso_machine_head_pressure", ha)
        self.assertGreater(app.display.frames, 0)
        self.assertIn("Environment", payload["other"][0]["name"])
        self.assertTrue(payload["other"][0]["health"]["ok"])

    def test_readme_documents_sim_and_the_hil_script(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("ADC_TYPE=SIM", readme)
        self.assertIn("--sim", readme)
        self.assertIn("hil_checklist.py", readme)
        self.assertIn("dtparam=i2c_arm_baudrate=400000", readme)
        self.assertIn("sample_hz", readme)


class TestReplayGolden(unittest.TestCase):
    def test_three_shots_and_a_flush_match_the_recording(self):
        from smart_espresso.sim.session import replay_recording

        recording = ROOT / "smart_espresso" / "test" / "replay" / "session.csv"
        golden_path = ROOT / "smart_espresso" / "test" / "replay" / "golden.txt"
        self.assertTrue(recording.is_file(), "reference recording is missing")
        app, states = replay_recording(str(recording))
        shots = list(reversed(app.web_server.shot_history.list_payload()["shots"]))
        lines = [
            "shots " + str(len(shots)),
        ]
        for shot in shots:
            lines.append(f"{shot['seconds']:.1f} {shot['ml']:.1f}")
        lines.append("states " + " ".join(collapsed(states)))
        actual = "\n".join(lines) + "\n"
        expected = golden_path.read_text(encoding="utf-8")
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
