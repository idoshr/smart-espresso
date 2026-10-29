"""The boot installer copies the repo unit and does not print secrets.

On main, before the installer exists, importing it fails.
"""

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install_service.py"
SHELL = ROOT / "scripts" / "install.sh"


def _load():
    spec = importlib.util.spec_from_file_location("install_service", INSTALLER)
    if spec is None or spec.loader is None:
        raise AssertionError("install_service.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestInstallService(unittest.TestCase):
    def test_unit_file_is_copied_from_the_repo_not_embedded(self):
        install = _load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deploy = root / "deploy"
            deploy.mkdir()
            unit = deploy / "smart-espresso.service"
            # A notify unit, as #77 writes it. The installer must not replace
            # this text with its own Type=simple copy.
            text = (
                "[Service]\n"
                "Type=notify\n"
                "NotifyAccess=main\n"
                "WatchdogSec=30\n"
                "After=network-online.target time-sync.target\n"
            )
            unit.write_text(text, encoding="utf-8")
            dest = root / "out" / "smart-espresso.service"
            install.install_unit(root, dest)
            self.assertEqual(dest.read_text(encoding="utf-8"), text)
            self.assertEqual(install.unit_source(root), unit)

    def test_drop_in_points_at_the_venv_and_clears_execstart(self):
        install = _load()
        text = install.drop_in_text(
            Path("/opt/smart-espresso"),
            Path("/opt/smart-espresso/.venv/bin/python"),
        )
        self.assertIn("ExecStart=\n", text)
        self.assertIn(
            "ExecStart=/opt/smart-espresso/.venv/bin/python "
            "/opt/smart-espresso/main.py\n",
            text,
        )
        self.assertNotIn("Type=", text)
        self.assertNotIn("HA_TOKEN", text)

    def test_existing_env_file_is_kept_and_secrets_are_not_printed(self):
        install = _load()
        secret = "super-secret-token"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smart-espresso.env"
            path.write_text(f"HA_TOKEN={secret}\n", encoding="utf-8")
            os.chmod(path, 0o600)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                install.ensure_env_file(path, log=print)
                install.ensure_env_file(path, log=print)
            self.assertEqual(path.read_text(encoding="utf-8"), f"HA_TOKEN={secret}\n")
            self.assertNotIn(secret, stdout.getvalue())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_new_env_file_is_a_template_without_a_token(self):
        install = _load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smart-espresso.env"
            install.ensure_env_file(path, log=lambda _message: None)
            text = path.read_text(encoding="utf-8")
            self.assertIn("HA_ENABLE=False", text)
            self.assertIn("# HA_TOKEN=", text)
            for line in text.splitlines():
                self.assertFalse(line.startswith("HA_TOKEN="))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_notify_unit_waits_until_active(self):
        install = _load()
        states = iter(["activating", "activating", "active"])
        seen = []

        def poll():
            state = next(states)
            seen.append(state)
            return state

        self.assertEqual(install.wait_until_active(poll, attempts=5), "active")
        self.assertEqual(seen, ["activating", "activating", "active"])

        def failed():
            return "failed"

        with self.assertRaises(RuntimeError):
            install.wait_until_active(failed, attempts=3)

        with self.assertRaises(TimeoutError):
            install.wait_until_active(lambda: "activating", attempts=2)

    def test_dry_run_prints_the_unit_path_and_writes_nothing(self):
        install = _load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            unit = root / "deploy" / "smart-espresso.service"
            unit.write_text(
                "[Service]\nType=notify\nWatchdogSec=30\n", encoding="utf-8"
            )
            dest = root / "installed.service"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = install.main(
                    [
                        "--source",
                        str(root),
                        "--dest",
                        str(root / "opt"),
                        "--unit-path",
                        str(dest),
                        "--env-file",
                        str(root / "smart-espresso.env"),
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertFalse(dest.exists())
            printed = stdout.getvalue()
            self.assertIn(str(unit), printed)
            self.assertIn("[pi,dht]", printed)
            self.assertIn("daemon-reload", printed)
            self.assertIn("is-active", printed)
            self.assertNotIn("HA_TOKEN=", printed)

    def test_shell_script_passes_shellcheck(self):
        shellcheck = subprocess.run(
            ["shellcheck", str(SHELL)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            shellcheck.returncode, 0, shellcheck.stdout + shellcheck.stderr
        )
        text = SHELL.read_text(encoding="utf-8")
        self.assertNotIn("Type=notify", text)
        self.assertNotIn("WatchdogSec", text)
        self.assertIn("deploy/smart-espresso.service", text)


if __name__ == "__main__":
    unittest.main()
