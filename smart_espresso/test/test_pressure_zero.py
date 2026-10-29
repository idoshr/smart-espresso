"""Pressure zero tracks a known-idle median and survives a restart."""
import os
import tempfile
import unittest

from smart_espresso.analog_sensor.analog_sensor import ADCInterface
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.shot_history import ShotHistory
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer


class HoldADC(ADCInterface):
    def __init__(self, voltage=0.5, max_voltage=5.0):
        self._voltage = voltage
        self._max_voltage = max_voltage

    def set(self, voltage):
        self._voltage = voltage

    def read(self):
        return min(self._voltage / self._max_voltage, 1.0)

    @property
    def voltage(self):
        return self._voltage


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _feed(sensor, clock, voltage, seconds, step=0.1, trusted=True):
    samples = int(round(seconds / step))
    for _ in range(samples):
        clock.now += step
        sensor.adc.set(voltage)
        try:
            sensor.read()
        except RuntimeError:
            pass
        observe = getattr(sensor, "observe_zero", None)
        if observe is not None:
            observe(trusted)


class TestPressureZero(unittest.TestCase):
    def test_positive_zero_error_is_learned_within_the_idle_window(self):
        clock = Clock()
        # 0.56 V on a 0–2 MPa head is +0.06 V, which reads 0.30 bar
        # until the zero is learned.
        sensor = PressureAnalogSensor(
            adc=HoldADC(0.56),
            name="Head",
            max_pressure_mpa=2.0,
            zero_window_seconds=1.0,
            now=clock,
        )
        sensor.read()
        self.assertAlmostEqual(sensor.bar, 0.30, delta=0.02)

        _feed(sensor, clock, 0.56, seconds=1.2, trusted=True)

        self.assertAlmostEqual(sensor.offset_voltage, 0.06, delta=0.005)
        self.assertAlmostEqual(sensor.bar, 0.0, delta=0.02)

    def test_a_short_dip_does_not_move_later_readings(self):
        sensor = PressureAnalogSensor(
            adc=HoldADC(2.3), name="Head", max_pressure_mpa=2.0
        )
        sensor.read()
        baseline = sensor.bar

        # 0.6 s at 10 Hz, low but still connected. The old ratchet kept this.
        for _ in range(6):
            sensor.adc.set(0.3)
            sensor.read()
        sensor.adc.set(2.3)
        sensor.read()

        self.assertLessEqual(abs(sensor.bar - baseline), 0.02)

    def test_a_dip_inside_the_zero_window_does_not_steer_the_median(self):
        clock = Clock()
        sensor = PressureAnalogSensor(
            adc=HoldADC(0.5),
            name="Head",
            max_pressure_mpa=2.0,
            zero_window_seconds=2.0,
            now=clock,
        )
        _feed(sensor, clock, 0.5, seconds=0.8, trusted=True)
        _feed(sensor, clock, 0.3, seconds=0.6, trusted=True)
        _feed(sensor, clock, 0.5, seconds=0.8, trusted=True)

        self.assertAlmostEqual(sensor.offset_voltage, 0.0, delta=0.005)
        sensor.adc.set(2.3)
        sensor.read()
        self.assertLessEqual(abs(sensor.bar - 9.0), 0.02)

    def test_unplug_and_replug_is_a_fault_and_does_not_shift_the_zero(self):
        sensor = PressureAnalogSensor(
            adc=HoldADC(2.3), name="Head", max_pressure_mpa=2.0
        )
        sensor.read()
        baseline = sensor.bar

        for _ in range(6):
            sensor.adc.set(0.1)
            with self.assertRaises(RuntimeError) as caught:
                sensor.read()
            self.assertIn("disconnected", str(caught.exception).lower())

        sensor.adc.set(2.3)
        sensor.read()
        self.assertLessEqual(abs(sensor.bar - baseline), 0.02)
        self.assertEqual(sensor.offset_voltage, 0.0)

    def test_offset_survives_a_restart(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pressure_head.json")
            sensor = PressureAnalogSensor(
                adc=HoldADC(0.56),
                name="Head",
                max_pressure_mpa=2.0,
                zero_window_seconds=1.0,
                state_path=path,
                now=clock,
            )
            _feed(sensor, clock, 0.56, seconds=1.2, trusted=True)
            self.assertAlmostEqual(sensor.offset_voltage, 0.06, delta=0.005)

            restarted = PressureAnalogSensor(
                adc=HoldADC(0.56),
                name="Head",
                max_pressure_mpa=2.0,
                state_path=path,
            )
            self.assertAlmostEqual(restarted.offset_voltage, 0.06, delta=0.005)
            restarted.read()
            self.assertAlmostEqual(restarted.bar, 0.0, delta=0.02)

    def test_under_0_2_v_is_a_sensor_fault(self):
        adc = HoldADC(0.1)
        sensor = PressureAnalogSensor(adc=adc, name="Head", max_pressure_mpa=2.0)
        server = StatusServer(
            port=0,
            tank=WaterTank(state_path=None),
            shot_history=ShotHistory(state_path=None),
        )
        server.start = lambda: None
        app = SmartEspresso(
            analog_devices=[sensor], web_server=server, render_interval=0
        )
        app.run(max_ticks=1)

        payload = server.app.test_client().get("/api/status").get_json()
        entry = payload["pressure"][0]
        self.assertIsNone(entry["bar"])
        self.assertFalse(entry["health"]["ok"])
        self.assertIn("disconnected", entry["health"]["last_error"].lower())
        self.assertEqual(sensor.offset_voltage, 0.0)

    def test_boiler_zero_tracks_only_while_the_machine_is_off(self):
        sensor = PressureAnalogSensor(
            adc=HoldADC(0.5), name="Boiler", max_pressure_mpa=0.5
        )
        sensor.read()
        self.assertFalse(sensor.tracks_zero("ready", 0.0))
        self.assertTrue(sensor.tracks_zero("off", 0.0))

        head = PressureAnalogSensor(adc=HoldADC(0.5), name="Head", max_pressure_mpa=2.0)
        head.read()
        self.assertTrue(head.tracks_zero("ready", 0.0))
        self.assertFalse(head.tracks_zero("brewing", 1.4))


if __name__ == "__main__":
    unittest.main()
