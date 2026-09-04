import unittest
from unittest import mock

from smart_espresso.analog_sensor.analog_sensor import ADCInterface
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer


class FakeADC(ADCInterface):
    """ADC stub whose voltage is set directly by the test."""

    def __init__(self, voltage=0.0, max_voltage=5.0):
        self._voltage = voltage
        self._max_voltage = max_voltage

    def set(self, voltage):
        self._voltage = voltage

    def read(self):
        return min(self._voltage / self._max_voltage, 1.0)

    @property
    def voltage(self):
        return self._voltage


class FakeClock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def pulse(sensor, adc, clock, count, server=None, step=0.1):
    """
    Feed `count` complete low->high->low pulses, advancing the clock per sample.

    `server` mirrors the real render loop, which snapshots after every read;
    the recent-water window is derived from those snapshots.
    """
    for _ in range(count):
        for voltage in (3.0, 0.0):
            adc.set(voltage)
            sensor.read()
            if server is not None:
                server.update([sensor])
            clock.advance(step)


class TestStatusServer(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        # Both the sensor and the server read the clock through their own
        # module-level `monotonic` import.
        self.patchers = [
            mock.patch("smart_espresso.web.status_server.monotonic", self.clock),
            mock.patch("smart_espresso.analog_sensor.water_flow_sensor.monotonic", self.clock),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.flow_adc = FakeADC()
        self.flow = WaterFlowAnalogSensor(
            adc=self.flow_adc, name="Brew", pulses_per_liter=100
        )
        # state_path=None keeps the tank counter out of the home directory.
        self.server = StatusServer(port=8099, tank=WaterTank(state_path=None))

    def tick(self):
        self.server.update([self.flow])

    def test_pressure_snapshot_exposes_bar_and_range(self):
        head = PressureAnalogSensor(
            adc=FakeADC(2.5), name="Head", max_pressure_mpa=2.0
        )
        self.server.update([head])

        entry = self.server.status_payload()["pressure"][0]
        self.assertEqual(entry["name"], "Head")
        self.assertEqual(entry["max_bar"], 20.0)
        self.assertAlmostEqual(entry["bar"], 10.0, places=1)

    def test_recent_ml_counts_only_water_inside_the_window(self):
        # Baseline snapshot at zero volume, as the render loop does on its
        # first tick before any water is drawn.
        self.tick()
        # 50 pulses = 0.5 L at 100 pulses/L.
        pulse(self.flow, self.flow_adc, self.clock, 50, server=self.server)
        self.tick()
        self.assertAlmostEqual(
            self.server.status_payload()["flow"][0]["recent_ml"], 500.0, places=1
        )

        # Wait out the whole window with no flow: the old water drops out,
        # while the lifetime total keeps it.
        for _ in range(13):
            self.clock.advance(10.0)
            self.tick()

        entry = self.server.status_payload()["flow"][0]
        self.assertEqual(entry["recent_ml"], 0.0)
        self.assertAlmostEqual(entry["total_ml"], 500.0, places=1)

    def test_chart_buckets_sum_to_recent_volume(self):
        pulse(self.flow, self.flow_adc, self.clock, 10, server=self.server)
        self.tick()
        self.clock.advance(30.0)
        pulse(self.flow, self.flow_adc, self.clock, 10, server=self.server)
        self.tick()

        entry = self.server.status_payload()["flow"][0]
        self.assertEqual(len(entry["chart"]), StatusServer.CHART_BUCKETS)
        self.assertAlmostEqual(sum(entry["chart"]), entry["recent_ml"], places=1)
        self.assertGreater(len([b for b in entry["chart"] if b > 0]), 0)

    def test_history_is_trimmed_to_the_window(self):
        for _ in range(400):
            self.clock.advance(1.0)
            self.tick()

        samples = self.server._flow_history["Brew"]
        max_age = StatusServer.WINDOW_SECONDS + StatusServer.HISTORY_SLACK_SECONDS
        self.assertLessEqual(len(samples), max_age + 2)
        self.assertGreaterEqual(samples[0][0], self.clock.now - max_age - 1)

    def test_non_analog_sensors_fall_back_to_their_message(self):
        class Dummy:
            name = "Room"
            message = "Room: 21.0C 40.0%"

        self.server.update([Dummy()])
        self.assertEqual(
            self.server.status_payload()["other"],
            [{"name": "Room", "message": "Room: 21.0C 40.0%"}],
        )

    def test_payload_carries_machine_state_and_tank(self):
        head = PressureAnalogSensor(adc=FakeADC(2.5), name="Head", max_pressure_mpa=2.0)
        boiler = PressureAnalogSensor(adc=FakeADC(1.46), name="Boiler", max_pressure_mpa=0.5)
        pulse(self.flow, self.flow_adc, self.clock, 20, server=self.server)
        self.server.update([head, boiler, self.flow])

        # The tank counts from the first sample it saw, so the pulse already
        # banked when the baseline was taken is not charged to the tank.
        payload = self.server.status_payload()
        self.assertEqual(payload["machine"]["state"], "brewing")
        self.assertAlmostEqual(payload["tank"]["used_ml"], 190.0, places=1)
        self.assertAlmostEqual(payload["tank"]["remaining_ml"], 1810.0, places=1)

    def test_tank_reset_endpoint_zeroes_the_count(self):
        pulse(self.flow, self.flow_adc, self.clock, 20, server=self.server)
        self.assertAlmostEqual(self.server.tank.used_ml, 190.0, places=1)

        response = self.server.app.test_client().post("/api/tank/reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["used_ml"], 0.0)
        self.assertEqual(response.get_json()["refills"], 1)
        self.assertEqual(self.server.status_payload()["tank"]["used_ml"], 0.0)

    def test_http_routes_serve_page_and_json(self):
        self.tick()
        client = self.server.app.test_client()

        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"<title>Espresso Bar Status</title>", page.data)

        api = client.get("/api/status")
        self.assertEqual(api.status_code, 200)
        payload = api.get_json()
        self.assertEqual(payload["window_seconds"], StatusServer.WINDOW_SECONDS)
        self.assertEqual(payload["flow"][0]["name"], "Brew")


if __name__ == "__main__":
    unittest.main()
