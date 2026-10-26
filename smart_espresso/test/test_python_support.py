"""Python 3.13 import path and the metadata that advertises it."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def _run_python(script, env=None):
    """Run ``script`` in a fresh interpreter so import blockers stay local."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


_BLOCK_HOME_ASSISTANT = """
import sys

class _BlockHomeAssistant:
    def find_spec(self, fullname, path, target=None):
        if fullname == "homeassistant_api" or fullname.startswith("homeassistant_api."):
            raise ModuleNotFoundError("homeassistant_api is not installed")
        return None

sys.meta_path.insert(0, _BlockHomeAssistant())
"""


class TestImportsWithoutHomeAssistantApi(unittest.TestCase):
    def test_flow_sensor_imports_without_the_package(self):
        script = (
            _BLOCK_HOME_ASSISTANT
            + """
import smart_espresso.analog_sensor as sensors
import smart_espresso.analog_sensor.water_flow_sensor
assert "homeassistant_api" not in sys.modules
assert not hasattr(sensors, "DHT22Sensor")
"""
        )
        result = _run_python(script)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pressure_sensor_imports_without_the_package(self):
        script = (
            _BLOCK_HOME_ASSISTANT
            + """
import smart_espresso.analog_sensor.pressure_analog_sensor
assert "homeassistant_api" not in sys.modules
"""
        )
        result = _run_python(script)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_starts_with_ha_disabled(self):
        # "Starts" here is the import and client construction path. Building
        # the ADS1115 needs the Pi I2C bus, which this check does not have.
        script = """
import main
import sys
assert main.client_ha is None
assert "homeassistant_api" not in sys.modules
print("started")
"""
        result = _run_python(
            script,
            env={
                "HA_ENABLE": "false",
                "DHT_ENABLE": "false",
                "WEB_ENABLE": "false",
                "DISPLAY_ENABLE": "false",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("started", result.stdout)


class TestHomeAssistantClient(unittest.TestCase):
    def test_set_state_posts_the_entity(self):
        session = mock.Mock()
        session.headers = {}
        response = mock.Mock()
        session.post.return_value = response
        with mock.patch(
            "smart_espresso.home_assistant.requests.Session", return_value=session
        ):
            from smart_espresso.home_assistant import Client, State

            client = Client("http://ha.local:8123/api", "token-1")
            client.set_state(
                State(
                    entity_id="sensor.espresso_machine_head_pressure",
                    state="9.10",
                    attributes={"unit_of_measurement": "Bar"},
                )
            )

        url = session.post.call_args.args[0]
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(
            url,
            "http://ha.local:8123/api/states/sensor.espresso_machine_head_pressure",
        )
        self.assertEqual(payload["state"], "9.10")
        self.assertEqual(payload["attributes"]["unit_of_measurement"], "Bar")
        self.assertEqual(session.headers["Authorization"], "Bearer token-1")
        response.raise_for_status.assert_called_once()


class TestSupportedPythonMetadata(unittest.TestCase):
    def test_classifiers_match_the_ci_matrix(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
            encoding="utf-8"
        )
        classified = re.findall(
            r"Programming Language :: Python :: (3\.\d+)", pyproject
        )
        matrix = re.findall(r"python-version: \[(.*?)\]", workflow, re.S)
        self.assertEqual(len(matrix), 1)
        versions = re.findall(r'"3\.\d+"', matrix[0])
        versions = [version.strip('"') for version in versions]
        self.assertEqual(classified, versions)
        self.assertEqual(versions, ["3.11", "3.13"])
        self.assertNotIn("PyPy", pyproject)
        self.assertNotIn("3.12", classified)
        self.assertIn('requires-python = ">=3.11"', pyproject)

    def test_requirements_do_not_pin_homeassistant_api(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("homeassistant-api", text)
        self.assertIn("requests==", text)


if __name__ == "__main__":
    unittest.main()
