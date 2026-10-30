"""Shutdown, restart and boot behaviour from issue #60.

These tests failed on main before the fix: SIGTERM left the tank file
unwritten and killed the process, today's tally came back as zero, and
WEB_ENABLE=false never advanced the classifier.
"""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime
from pathlib import Path

from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import ShotHistory
from smart_espresso.water_tank import WaterTank

ROOT = Path(__file__).resolve().parents[2]


class TestGracefulShutdown(unittest.TestCase):
    def test_sigterm_flushes_the_tank_and_threads_exit(self):
        script = textwrap.dedent(
            r"""
            import os
            import threading

            from smart_espresso.analog_sensor.analog_sensor import ADCInterface
            from smart_espresso.analog_sensor.water_flow_sensor import (
                WaterFlowAnalogSensor,
            )
            from smart_espresso.machine_state import MachineStateClassifier
            from smart_espresso.shot_history import ShotHistory
            from smart_espresso.smart_espresso import SmartEspresso
            from smart_espresso.water_tank import WaterTank

            class PulseADC(ADCInterface):
                def __init__(self):
                    self.n = 0
                    self._voltage = 0.0

                def read(self):
                    self.n += 1
                    self._voltage = 3.3 if self.n % 2 else 0.0
                    return self._voltage / 3.3

                @property
                def voltage(self):
                    return self._voltage

            class Marker:
                def __init__(self):
                    self.name = "Marker"
                    self._stop = threading.Event()
                    self._thread = threading.Thread(target=self._run, name="marker")
                    self._thread.start()

                def _run(self):
                    self._stop.wait()

                def read(self):
                    return None

                @property
                def message(self):
                    return "Marker"

                def stop(self):
                    self._stop.set()
                    self._thread.join(timeout=1.0)

            flow = WaterFlowAnalogSensor(
                adc=PulseADC(),
                name="Brew",
                pulses_per_liter=100,
                sample_interval=0.01,
            )
            tank = WaterTank(
                state_path=os.environ["TANK_PATH"],
                save_interval=120,
            )
            history = ShotHistory(state_path=None)
            classifier = MachineStateClassifier(shot_history=history)
            app = SmartEspresso(
                analog_devices=[flow],
                digital_sensors=[],
                web_server=None,
                tank=tank,
                shot_history=history,
                classifier=classifier,
                render_interval=0.05,
            )
            marker = Marker()
            app.digital_sensors.append(marker)
            app.all_sensors.append(marker)
            from smart_espresso.smart_espresso import SensorHealth

            app._health[marker.name] = SensorHealth(marker.name)

            def report():
                while True:
                    print(f"USED {tank.used_ml}", flush=True)
                    if tank.used_ml >= 5:
                        return
                    threading.Event().wait(0.05)

            threading.Thread(target=report, daemon=True).start()
            app.run()
            print(
                "ALIVE",
                flow._thread.is_alive(),
                marker._thread.is_alive(),
                flush=True,
            )
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            tank_path = os.path.join(directory, "tank.json")
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env={**os.environ, "TANK_PATH": tank_path, "HA_ENABLE": "false"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            seen = 0.0
            deadline = time.monotonic() + 15
            stdout_lines = []
            stderr_lines = []
            assert proc.stdout is not None and proc.stderr is not None
            # Stderr has to be drained too. A full pipe stalls the flow
            # thread, the tank stays at 0, and the USED lines never climb.
            while time.monotonic() < deadline and proc.poll() is None and seen < 5:
                ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.2)
                for stream in ready:
                    line = stream.readline()
                    if not line:
                        continue
                    if stream is proc.stderr:
                        stderr_lines.append(line)
                        continue
                    stdout_lines.append(line)
                    if line.startswith("USED"):
                        seen = max(seen, float(line.split()[1]))
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGTERM)
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                self.fail(f"process still running after 2s\n{stdout}\n{stderr}")
            stdout, stderr = proc.communicate()
            full_out = "".join(stdout_lines) + (stdout or "")
            full_err = "".join(stderr_lines) + (stderr or "")
            for line in full_out.splitlines():
                if line.startswith("USED"):
                    seen = max(seen, float(line.split()[1]))
            self.assertEqual(code, 0, full_err + full_out)
            self.assertIn("ALIVE False False", full_out)
            self.assertGreaterEqual(seen, 5, full_err + full_out)
            with open(tank_path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertGreaterEqual(saved["used_ml"], 5)

    def test_today_comes_back_from_shot_history(self):
        moment = datetime.now().astimezone().timestamp()
        history = ShotHistory(state_path=None, now=lambda: moment)
        for seconds, ml in ((3.0, 20.0), (4.0, 30.0)):
            history.open()
            history.sample(0.0, 9.0, 1.2, 1.4)
            history.commit(seconds, ml)

        restarted = MachineStateClassifier(shot_history=history)
        today = restarted.snapshot()["today"]
        self.assertEqual(today["shots"], 2)
        self.assertEqual(today["ml"], 50.0)

    def test_open_pull_is_closed_on_shutdown(self):
        from unittest import mock

        from smart_espresso.test.test_machine_state import FakeClock

        clock = FakeClock()
        history = ShotHistory(state_path=None, now=lambda: 1_700_000_100.0)
        with mock.patch("smart_espresso.machine_state.monotonic", clock):
            classifier = MachineStateClassifier(shot_history=history, dwell_seconds=0.0)
            classifier.update(0.0, 1.2)
            total = 0.0
            for _ in range(6):
                total += 2.0
                clock.advance(1.0)
                classifier.update(9.0, 1.2, flow_mls=2.0, total_ml=total)
            self.assertEqual(history.list_payload()["count"], 0)
            classifier.close_open_pull()
        self.assertEqual(history.list_payload()["count"], 1)
        self.assertGreaterEqual(history.get(1)["ml"], 5)

    def test_headless_sim_tracks_state_tank_and_history(self):
        from smart_espresso.sim.machine import Segment, SimulatedMachine
        from smart_espresso.sim.session import build_sim_sensors, drive, virtual_clock
        from smart_espresso.smart_espresso import SmartEspresso

        segments = []
        cursor = 0.0

        def add(duration, **kwargs):
            nonlocal cursor
            segments.append(Segment(start=cursor, end=cursor + duration, **kwargs))
            cursor += duration

        add(1.0, kind="ready", boiler_bar=1.2)
        add(8.0, kind="shot", head_bar=9.0, boiler_bar=1.2, flow_mls=2.0)
        add(2.0, kind="settle", boiler_bar=1.2, head_bar=0.0, flow_mls=0.0)
        machine = SimulatedMachine(seed=59, segments=segments, loop=False)
        history = ShotHistory(state_path=None)
        tank = WaterTank(state_path=None, save_interval=60)
        classifier = MachineStateClassifier(shot_history=history, dwell_seconds=0.0)
        with virtual_clock() as clock:
            sensors = build_sim_sensors(machine, sample_interval=None)
            app = SmartEspresso(
                analog_devices=sensors,
                digital_sensors=[],
                web_server=None,
                tank=tank,
                shot_history=history,
                classifier=classifier,
                render_interval=0,
            )
            self.assertIsNone(app.web_server)
            drive(app, clock, machine.duration)
        self.assertNotEqual(classifier.snapshot()["state"], "unknown")
        self.assertGreater(tank.used_ml, 5)
        self.assertEqual(history.list_payload()["count"], 1)

    def test_epoch_clock_is_marked_and_restamped_after_a_step(self):
        wall = {"now": 1_000.0}
        history = ShotHistory(state_path=None, now=lambda: wall["now"])
        history.open()
        history.sample(0.0, 9.0, 1.2, 1.4)
        shot = history.commit(3.0, 20.0)
        self.assertFalse(shot["clock_synced"])
        stamped = datetime.fromisoformat(shot["started_at"])
        wall["now"] += 86_400
        self.assertTrue(history.observe_clock())
        moved = history.get(1)
        self.assertTrue(moved["clock_synced"])
        again = datetime.fromisoformat(moved["started_at"])
        delta = (again - stamped).total_seconds()
        self.assertGreater(delta, 86_000)
        self.assertLess(delta, 87_000)

    def test_service_unit_restarts_and_waits_for_the_clock(self):
        text = (ROOT / "deploy" / "smart-espresso.service").read_text(encoding="utf-8")
        self.assertIn("Restart=always", text)
        self.assertIn("RestartSec=2", text)
        self.assertIn("Type=notify", text)
        self.assertIn("NotifyAccess=main", text)
        self.assertIn("WatchdogSec=30", text)
        self.assertIn("time-sync.target", text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Restart=always", readme)
        self.assertIn("WatchdogSec=30", readme)
        self.assertIn("time-sync.target", readme)

    def test_watchdog_ping_uses_notify_socket(self):
        import socket

        from smart_espresso.sd_notify import notify_systemd

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        path = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "notify.sock")
                sock.bind(path)
                sock.settimeout(1)
                previous = os.environ.get("NOTIFY_SOCKET")
                os.environ["NOTIFY_SOCKET"] = path
                try:
                    notify_systemd("READY=1")
                    notify_systemd("WATCHDOG=1")
                finally:
                    if previous is None:
                        os.environ.pop("NOTIFY_SOCKET", None)
                    else:
                        os.environ["NOTIFY_SOCKET"] = previous
                self.assertEqual(sock.recv(64), b"READY=1")
                self.assertEqual(sock.recv(64), b"WATCHDOG=1")
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
