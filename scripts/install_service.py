"""Install smart-espresso as a systemd service that starts on boot.

The unit file is the repo's ``deploy/smart-espresso.service``. This script
copies that file. It does not embed a second copy, so a unit that is
``Type=notify`` with ``WatchdogSec`` is installed as written.

A rerun upgrades the virtualenv and reloads the service. An existing
``/etc/smart-espresso.env`` is left in place and is never printed.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ENV_TEMPLATE = """\
# Smart Espresso service environment. Mode 0600.
# The installer never prints this file. Uncomment and set HA_TOKEN locally.
ADC_TYPE=ADS1115
WEB_ENABLE=True
HA_ENABLE=False
# HA_URL=
# HA_TOKEN=
"""


def unit_source(repo: Path) -> Path:
    """Path of the unit file that ships with this checkout."""
    return Path(repo) / "deploy" / "smart-espresso.service"


def install_unit(repo: Path, dest: Path) -> None:
    """Copy the repo unit onto ``dest`` byte for byte."""
    source = unit_source(repo)
    if not source.is_file():
        raise FileNotFoundError(f"unit file is missing: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    os.chmod(dest, 0o644)


def drop_in_text(checkout: Path, python: Path) -> str:
    """Override ExecStart with the venv interpreter. Leave Type= alone."""
    checkout = Path(checkout)
    python = Path(python)
    return (
        "[Service]\n"
        f"WorkingDirectory={checkout}\n"
        "ExecStart=\n"
        f"ExecStart={python} {checkout / 'main.py'}\n"
        "EnvironmentFile=-/etc/smart-espresso.env\n"
    )


def ensure_env_file(path: Path, log=print) -> None:
    """Create a secret-free template, or leave an existing file untouched."""
    path = Path(path)
    if path.exists():
        log(f"leaving existing env file in place: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ENV_TEMPLATE, encoding="utf-8")
    os.chmod(path, 0o600)
    log(f"wrote env template: {path}")


def wait_until_active(is_active, attempts: int = 30, pause=None) -> str:
    """Poll until systemd reports active.

    ``Type=notify`` stays ``activating`` until the process sends ``READY=1``.
    ``failed`` stops the wait. Anything else is polled again.
    """

    def _noop() -> None:
        return None

    if pause is None:
        pause = _noop
    last = "unknown"
    for _attempt in range(attempts):
        last = is_active()
        if last == "active":
            return last
        if last == "failed":
            raise RuntimeError("smart-espresso.service failed to start")
        pause()
    raise TimeoutError(
        f"smart-espresso.service did not become active (last state: {last})"
    )


def pip_install_command(venv_python: Path, checkout: Path) -> list:
    """Editable install of the Pi and DHT extras into ``venv_python``."""
    return [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "-e",
        f"{checkout}[pi,dht]",
    ]


def build_plan(source: Path, dest: Path, unit_path: Path, env_file: Path) -> list:
    """Human-readable steps. No secret values."""
    python = dest / ".venv" / "bin" / "python"
    return [
        f"unit source: {unit_source(source)}",
        f"install unit: {unit_path}",
        f"venv: {python}",
        "pip install -e .[pi,dht]",
        f"env file (create only if missing): {env_file}",
        "systemctl daemon-reload",
        "systemctl enable smart-espresso.service",
        "systemctl restart smart-espresso.service",
        "systemctl is-active smart-espresso.service",
    ]


def _run(command, runner) -> None:
    if runner is None:
        subprocess.run(command, check=True)
        return
    runner(command)


def apply_install(
    source: Path,
    dest: Path,
    unit_path: Path,
    env_file: Path,
    runner=None,
    is_active=None,
    pause=None,
    log=print,
) -> None:
    """Create the venv, copy the unit, and wait until the service is active."""
    source = Path(source)
    dest = Path(dest)
    venv_python = dest / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        _run([sys.executable, "-m", "venv", str(dest / ".venv")], runner)
    _run(pip_install_command(venv_python, source), runner)
    ensure_env_file(env_file, log=log)
    install_unit(source, unit_path)
    drop_in = unit_path.parent / "smart-espresso.service.d" / "install.conf"
    drop_in.parent.mkdir(parents=True, exist_ok=True)
    # WorkingDirectory is the checkout (where main.py lives). The venv may
    # sit beside it. Type= and WatchdogSec stay in the unit file itself.
    drop_in.write_text(drop_in_text(source, venv_python), encoding="utf-8")
    os.chmod(drop_in, 0o644)
    _run(["systemctl", "daemon-reload"], runner)
    _run(["systemctl", "enable", "smart-espresso.service"], runner)
    _run(["systemctl", "restart", "smart-espresso.service"], runner)

    def poll():
        if is_active is not None:
            return is_active()
        completed = subprocess.run(
            ["systemctl", "is-active", "smart-espresso.service"],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() or "unknown"

    def _pause():
        if pause is not None:
            pause()
        else:
            import time

            time.sleep(1)

    state = wait_until_active(poll, pause=_pause)
    log(f"smart-espresso.service is {state}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the smart-espresso boot service"
    )
    parser.add_argument(
        "--source", required=True, help="Checkout that contains deploy/"
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Virtualenv location (default: the checkout)",
    )
    parser.add_argument(
        "--unit-path",
        default="/etc/systemd/system/smart-espresso.service",
    )
    parser.add_argument("--env-file", default="/etc/smart-espresso.env")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    dest = Path(args.dest) if args.dest else source
    unit_path = Path(args.unit_path)
    env_file = Path(args.env_file)
    if not unit_source(source).is_file():
        print(f"missing unit file: {unit_source(source)}", file=sys.stderr)
        return 1

    for line in build_plan(source, dest, unit_path, env_file):
        print(line)
    if args.dry_run:
        return 0
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("run as root (sudo ./scripts/install.sh)", file=sys.stderr)
        return 1
    # The service runs from the checkout the unit was copied from, so a
    # rerun upgrades that tree in place.
    apply_install(source, dest, unit_path, env_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
