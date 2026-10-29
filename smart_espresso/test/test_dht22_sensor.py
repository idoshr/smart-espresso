"""The DHT22 poll thread survives a bad read, and old samples expire."""
import threading
import time
import unittest

from smart_espresso.shot_history import ShotHistory
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AlternateReader:
    """One good sample, one checksum error, one new sample, then wait."""

    def __init__(self):
        self.calls = 0
        self.second = threading.Event()
        self.hold = threading.Event()
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            call = self.calls
        if call == 2:
            raise RuntimeError("checksum")
        if call == 1:
            return 41.0, 21.0
        if call == 3:
            self.second.set()
            return 43.0, 23.0
        self.hold.wait(timeout=2.0)
        raise RuntimeError("idle")


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestDhtPollThread(unittest.TestCase):
    def test_errors_on_alternate_reads_keep_the_thread_alive(self):
        reader = AlternateReader()
        sensor = DHT22(pin=4, name="Environment", reader=reader, interval=0.05)
        self.addCleanup(sensor.stop)
        self.addCleanup(reader.hold.set)

        self.assertTrue(
            _wait(lambda: reader.second.is_set() and sensor.temperature == 23.0),
            f"calls={reader.calls} temperature={sensor.temperature}",
        )
        self.assertTrue(sensor._poll_thread.is_alive())
        self.assertGreaterEqual(reader.calls, 3)
        humidity, temperature = sensor.read()
        self.assertEqual(humidity, 43.0)
        self.assertEqual(temperature, 23.0)

    def test_values_older_than_three_intervals_are_none(self):
        clock = Clock()
        hold = threading.Event()
        once = {"done": False}

        def reader():
            if not once["done"]:
                once["done"] = True
                return 45.0, 24.0
            hold.wait(timeout=2.0)
            raise RuntimeError("no further sample")

        sensor = DHT22(
            pin=4, name="Environment", reader=reader, interval=2.0, now=clock
        )
        self.addCleanup(sensor.stop)
        self.addCleanup(hold.set)

        self.assertTrue(_wait(lambda: sensor.temperature == 24.0))
        # Still inside the 6 s window: the last sample is the reading.
        clock.advance(6.0)
        self.assertEqual(sensor.temperature, 24.0)
        self.assertEqual(sensor.humidity, 45.0)

        clock.advance(0.01)
        self.assertIsNone(sensor.temperature)
        self.assertIsNone(sensor.humidity)
        self.assertEqual(sensor.read(), (None, None))
        self.assertIn("last seen", sensor.message.lower())
        self.assertNotIn("stale", sensor.message.lower())
        self.assertEqual(sensor.ha_states(), [])

        server = StatusServer(
            port=8094,
            tank=WaterTank(state_path=None),
            shot_history=ShotHistory(state_path=None),
        )
        app = SmartEspresso(
            digital_sensors=[sensor], web_server=server, render_interval=0
        )
        # A fresh sample is published.
        clock.now -= 0.01
        app._tick()
        fresh = {state.entity_id for state in app._ha_states()}
        self.assertIn("sensor.environment_temperature", fresh)
        self.assertTrue(app._health["Environment"].ok)

        # Past the window it is not, and the status row says so.
        clock.advance(0.01)
        hold.set()
        app._tick()
        stale = {state.entity_id for state in app._ha_states()}
        self.assertNotIn("sensor.environment_temperature", stale)
        self.assertNotIn("sensor.environment_humidity", stale)
        self.assertFalse(app._health["Environment"].ok)
        self.assertIn("stale", app._health["Environment"].last_error.lower())
        row = server.status_payload()["other"][0]
        self.assertIn("last seen", row["message"].lower())
        self.assertNotIn("stale", row["message"].lower())
        self.assertFalse(row["health"]["ok"])


def DHT22(**kwargs):
    from smart_espresso.analog_sensor.dht22_sensor import DHT22Sensor

    return DHT22Sensor(**kwargs)


if __name__ == "__main__":
    unittest.main()
