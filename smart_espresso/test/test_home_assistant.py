"""Home Assistant publishes must not freeze the render loop."""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from smart_espresso.analog_sensor.analog_sensor import ADCInterface
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.home_assistant import DEFAULT_TIMEOUT, HomeAssistantPublisher, State
from smart_espresso.shot_history import ShotHistory
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer

ROOT = Path(__file__).resolve().parents[2]


class SteadyADC(ADCInterface):
    def __init__(self, voltage=1.5):
        self._voltage = voltage

    def read(self):
        return min(self._voltage / 5.0, 1.0)

    @property
    def voltage(self):
        return self._voltage


class SlowClient:
    """Blocks inside set_state the way a black-holed HA host would."""

    def __init__(self, delay=10.0):
        self.delay = delay
        self.calls = 0
        self.started = threading.Event()

    def set_state(self, state):
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)


class RecoveringClient:
    def __init__(self):
        self.fail = True
        self.failures = 0
        self.successes = 0
        self.states = []
        self.entities = []

    def set_state(self, state):
        if self.fail:
            self.failures += 1
            raise ConnectionError("home assistant down")
        self.successes += 1
        self.states.append(state.state)
        self.entities.append(state.entity_id)


def _pressure(name):
    return PressureAnalogSensor(adc=SteadyADC(), name=name, max_pressure_mpa=2.0)


def _server():
    return StatusServer(
        port=0,
        tank=WaterTank(state_path=None),
        shot_history=ShotHistory(state_path=None),
    )


class TestTimeoutKwargs(unittest.TestCase):
    def test_client_is_built_with_connect_and_read_timeouts(self):
        with mock.patch("smart_espresso.home_assistant.Client") as client:
            from main import build_home_assistant_client

            build_home_assistant_client(
                {
                    "HA_ENABLE": "true",
                    "HA_URL": "http://ha.local:8123",
                    "HA_TOKEN": "token",
                }
            )
        client.assert_called_once_with(
            "http://ha.local:8123/api",
            "token",
            verify_ssl=True,
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertEqual(DEFAULT_TIMEOUT, (2.0, 3.0))


class TestPublisher(unittest.TestCase):
    def test_recovers_when_the_client_starts_succeeding(self):
        client = RecoveringClient()
        publisher = HomeAssistantPublisher(client, min_backoff=0.01, max_backoff=0.05)
        publisher.submit([State("sensor.espresso_machine_state", "ready")])
        publisher.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and client.failures < 1:
                time.sleep(0.01)
            self.assertGreaterEqual(client.failures, 1)
            client.fail = False
            while time.monotonic() < deadline and client.successes < 1:
                time.sleep(0.01)
            self.assertGreaterEqual(client.successes, 1)
        finally:
            publisher.stop()

    def test_only_the_latest_snapshot_is_published(self):
        client = RecoveringClient()
        client.fail = False
        publisher = HomeAssistantPublisher(client)
        publisher.submit([State("sensor.espresso_machine_state", "old")])
        publisher.submit([State("sensor.espresso_machine_state", "new")])
        publisher.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not client.states:
                time.sleep(0.01)
        finally:
            publisher.stop()
        self.assertEqual(client.states, ["new"])


class TestRenderLoop(unittest.TestCase):
    def test_slow_client_does_not_stall_the_loop(self):
        client = SlowClient(delay=10.0)
        server = _server()
        server.start = lambda: None
        ticks = {"n": 0}
        real = server.classifier.update

        def spy(*args, **kwargs):
            ticks["n"] += 1
            return real(*args, **kwargs)

        server.classifier.update = spy
        app = SmartEspresso(
            analog_devices=[_pressure("Head"), _pressure("Boiler")],
            client_ha=client,
            web_server=server,
            render_interval=0.05,
            ha_update_interval=0,
        )
        started = time.monotonic()
        loop = {}

        def stop():
            # stop() joins the worker, which is blocked in the 10 s call.
            # The rate below is the render loop only, before that join.
            loop["elapsed"] = time.monotonic() - started
            HomeAssistantPublisher.stop(app._publisher)

        app._publisher.stop = stop
        completed = app.run(max_seconds=1.0)
        elapsed = loop["elapsed"]
        self.assertGreaterEqual(client.calls, 1)
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(completed / elapsed, 9)
        self.assertGreaterEqual(ticks["n"] / elapsed, 9)

    def test_publishes_machine_shot_and_tank(self):
        client = RecoveringClient()
        client.fail = False
        server = _server()
        app = SmartEspresso(
            analog_devices=[_pressure("Head")],
            client_ha=client,
            web_server=server,
            render_interval=0,
            ha_update_interval=0,
        )
        app._publisher.start()
        try:
            for _ in range(3):
                app._tick()
            deadline = time.monotonic() + 2.0
            wanted = {
                "sensor.espresso_machine_head_pressure",
                "sensor.espresso_machine_state",
                "sensor.espresso_machine_shot",
                "sensor.espresso_water_tank",
            }
            while time.monotonic() < deadline and not wanted.issubset(client.entities):
                time.sleep(0.01)
        finally:
            app._publisher.stop()

        self.assertTrue(wanted.issubset(set(client.entities)))

    def test_failed_sensor_is_left_out_of_the_snapshot(self):
        sensor = _pressure("Boiler")
        server = _server()
        app = SmartEspresso(
            analog_devices=[sensor],
            client_ha=RecoveringClient(),
            web_server=server,
            render_interval=0,
            ha_update_interval=0,
        )
        sensor.adc.read = mock.Mock(side_effect=OSError(121, "Remote I/O error"))
        app._tick()
        ids = {state.entity_id for state in app._ha_states()}
        self.assertNotIn("sensor.espresso_machine_boiler_pressure", ids)
        self.assertIn("sensor.espresso_machine_state", ids)

    def test_misleading_comment_is_gone(self):
        source = (ROOT / "smart_espresso" / "smart_espresso.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("can't stall sensor sampling", source)
        self.assertIn("cannot stall", source)
