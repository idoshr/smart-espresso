"""A sensor or display fault must not kill the monitor."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import main
from smart_espresso.analog_sensor.analog_sensor import ADCInterface
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import ShotHistory
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer


class FlakyADC(ADCInterface):
    """Raises OSError on every 5th read, like a transient I2C error."""

    def __init__(self, voltage=1.46):
        self.calls = 0
        self._voltage = voltage
        self.fail = False

    def read(self):
        self.calls += 1
        if self.fail or self.calls % 5 == 0:
            raise OSError(121, "Remote I/O error")
        return min(self._voltage / 5.0, 1.0)

    @property
    def voltage(self):
        return self._voltage


def _server(**kwargs):
    return StatusServer(
        port=0,
        tank=WaterTank(state_path=None),
        shot_history=ShotHistory(state_path=None),
        **kwargs,
    )


class TestSensorFaults(unittest.TestCase):
    def test_periodic_oserror_does_not_stop_the_loop(self):
        adc = FlakyADC()
        sensor = PressureAnalogSensor(adc=adc, name="Boiler", max_pressure_mpa=0.5)
        server = _server()
        updates = {"n": 0, "last": None}
        real = server.classifier.update

        def spy(head, boiler, flow=0.0, total=0.0):
            updates["n"] += 1
            updates["last"] = (head, boiler, flow, total)
            return real(head, boiler, flow, total)

        server.classifier.update = spy
        app = SmartEspresso(
            analog_devices=[sensor], web_server=server, render_interval=0
        )
        # start() binds a port; the assertions use the in-process snapshot
        # and the test client, so the listen thread is not required.
        server.start = lambda: None

        output = io.StringIO()
        with redirect_stdout(output):
            ticks = app.run(max_ticks=1000)

        self.assertEqual(ticks, 1000)
        self.assertEqual(updates["n"], 1000)
        # Tick 1000 is a failure (every 5th read), so the classifier sees
        # no boiler pressure rather than the cached 1.2 bar.
        self.assertIsNone(updates["last"][1])

        payload = server.app.test_client().get("/api/status").get_json()
        health = payload["pressure"][0]["health"]
        self.assertFalse(health["ok"])
        self.assertIn("OSError", health["last_error"])
        self.assertIn("last_ok_age_s", health)
        self.assertIsNotNone(health["last_ok_age_s"])
        self.assertIsNone(payload["pressure"][0]["bar"])
        # One throttled line, not one per failed read.
        self.assertEqual(output.getvalue().count("read failed"), 1)
        self.assertGreater(sensor.adc.calls, 5)

    def test_a_failed_head_reading_is_not_reused_as_brewing(self):
        adc = FlakyADC(voltage=2.3)  # 9 bar on the 0-2 MPa head sensor
        adc.fail = False
        head = PressureAnalogSensor(adc=adc, name="Head", max_pressure_mpa=2.0)
        classifier = MachineStateClassifier(
            dwell_seconds=0, shot_history=ShotHistory(state_path=None)
        )
        server = _server(classifier=classifier)
        server.start = lambda: None
        app = SmartEspresso(analog_devices=[head], web_server=server, render_interval=0)
        app.run(max_ticks=1)
        self.assertEqual(classifier.state, "brewing")

        adc.fail = True
        app.run(max_ticks=1)
        self.assertNotEqual(classifier.state, "brewing")
        self.assertIsNone(server.status_payload()["pressure"][0]["bar"])

    def test_display_errors_do_not_stop_the_loop(self):
        sensor = PressureAnalogSensor(
            adc=FlakyADC(voltage=1.46), name="Boiler", max_pressure_mpa=0.5
        )
        # The 5th read would raise; keep this test on successful reads.
        sensor.adc.fail = False
        sensor.adc.read = lambda: min(sensor.adc.voltage / 5.0, 1.0)
        app = SmartEspresso(
            analog_devices=[sensor],
            display=object(),
            render_interval=0,
        )
        with mock.patch(
            "smart_espresso.smart_espresso._display_canvas",
            side_effect=OSError(121, "Remote I/O error"),
        ):
            ticks = app.run(max_ticks=20)
        self.assertEqual(ticks, 20)
        self.assertGreaterEqual(app._display_health.error_count, 20)


class TestMissingDevices(unittest.TestCase):
    def test_a_missing_channel_display_and_dht_are_skipped(self):
        def adc_builder(pin, gain):
            if pin == 1:
                raise OSError(121, "Remote I/O error")

            class _ADC(ADCInterface):
                def read(self):
                    return 0.2

                @property
                def voltage(self):
                    return 1.0

            return _ADC()

        output = io.StringIO()
        with redirect_stdout(output):
            sensors = main.build_analog_devices(
                adc_builder=adc_builder, flow_sample_interval=None
            )
            display = main.build_display(
                enabled=True,
                factory=lambda: (_ for _ in ()).throw(OSError(121, "Remote I/O error")),
            )
            digital = main.build_digital_sensors(
                enabled=True,
                factory=lambda: (_ for _ in ()).throw(ImportError("Adafruit_DHT")),
            )

        text = output.getvalue()
        self.assertIn("WARNING: Boiler sensor disabled", text)
        self.assertIn("WARNING: display disabled", text)
        self.assertIn("WARNING: DHT22 disabled", text)
        self.assertEqual([sensor.name for sensor in sensors], ["Head", "Brew"])
        self.assertIsNone(display)
        self.assertEqual(digital, [])

        server = _server()
        server.start = lambda: None
        app = SmartEspresso(
            analog_devices=sensors, web_server=server, render_interval=0
        )
        self.assertEqual(app.run(max_ticks=1), 1)
        for sensor in sensors:
            if hasattr(sensor, "stop"):
                sensor.stop()


if __name__ == "__main__":
    unittest.main()
