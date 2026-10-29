"""The CI install split and the workflows that have to stay green."""

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PI_DISTRIBUTIONS = {
    "gpiozero",
    "luma.core",
    "luma.oled",
    "adafruit-circuitpython-ads1x15",
    "adafruit-blinka",
    "adafruit-circuitpython-dht",
    "adafruit_dht",
}


def _name(requirement):
    return requirement.split("==", 1)[0].strip().lower().replace("_", "-")


class TestDependencySplit(unittest.TestCase):
    def test_requirements_match_core_pi_and_dht(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        extras = project["optional-dependencies"]
        core = project["dependencies"]
        declared = set(core + extras["pi"] + extras["dht"])
        pinned = []
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pinned.append(line)
        self.assertEqual(set(pinned), declared)
        self.assertEqual(extras["ha"], ["requests==2.32.5"])
        self.assertIn("requests==2.32.5", core)
        core_names = {_name(item) for item in core}
        self.assertTrue(core_names.isdisjoint(PI_DISTRIBUTIONS))
        for name in core_names:
            self.assertNotIn("gpio", name)
            self.assertNotIn("luma", name)
            self.assertNotIn("adafruit", name)
            self.assertNotIn("dht", name)


class TestWorkflows(unittest.TestCase):
    def test_pytest_runs_on_every_pull_request(self):
        text = (ROOT / ".github" / "workflows" / "test.yml").read_text()
        self.assertIn("pull_request:", text)
        self.assertIn('"3.11"', text)
        self.assertIn('"3.13"', text)
        self.assertIn("pip install -e .", text)
        self.assertIn('".[test,pi]"', text)
        self.assertNotIn("[dht]", text)
        self.assertNotIn("Adafruit_DHT==", text)
        self.assertIn("pytest", text)

    def test_lint_runs_pre_commit_on_pull_requests_and_main(self):
        text = (ROOT / ".github" / "workflows" / "linting.yml").read_text()
        self.assertIn("pull_request:", text)
        self.assertIn("- main", text)
        self.assertIn("- master", text)
        self.assertIn("pip install pre-commit", text)
        self.assertIn("pre-commit run --all-files", text)
        self.assertNotIn("nox", text)

    def test_label_check_workflow_is_gone(self):
        path = ROOT / ".github" / "workflows" / "labeler-check.yml"
        self.assertFalse(path.exists())
