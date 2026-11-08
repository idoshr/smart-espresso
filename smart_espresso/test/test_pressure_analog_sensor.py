import io
import unittest
from contextlib import redirect_stdout

import main
from smart_espresso.analog_sensor.analog_sensor import ADCInterface, MedianFilterADC
from smart_espresso.analog_sensor.pressure_analog_sensor import (
    PressureAnalogSensor,
    pressure_voltages_from_env,
)


class SequenceADC(ADCInterface):
    """ADC stub that plays back a predefined sequence of voltages."""

    def __init__(self, voltages, max_voltage=5.0):
        self._voltages = list(voltages)
        self._index = -1
        self._max_voltage = max_voltage
        self._cached_voltage = 0.0

    def read(self):
        self._index = min(self._index + 1, len(self._voltages) - 1)
        self._cached_voltage = self._voltages[self._index]
        return min(self._cached_voltage / self._max_voltage, 1.0)

    @property
    def voltage(self):
        return self._cached_voltage


class TestMedianFilterADC(unittest.TestCase):
    def test_single_sample_spike_is_rejected(self):
        # A one-sample noise spike must not be reported by value or voltage.
        adc = MedianFilterADC(SequenceADC([0.5, 0.5, 0.5, 4.5, 0.5, 0.5]))
        values = [adc.read() for _ in range(6)]

        # The window fills on the third sample; the spike afterwards is
        # outvoted by its clean neighbours.
        self.assertEqual(values[2], 0.5 / 5.0)
        self.assertEqual(values[3], 0.5 / 5.0)
        self.assertEqual(values[4], 0.5 / 5.0)

    def test_reports_raw_until_window_fills(self):
        # Before the window is full there is no majority to vote with, so
        # the raw sample is reported (a two-read startup transient).
        adc = MedianFilterADC(SequenceADC([0.5, 4.5]))

        self.assertEqual(adc.read(), 0.5 / 5.0)
        self.assertEqual(adc.read(), 4.5 / 5.0)

    def test_spike_voltage_is_rejected(self):
        # Consumers reading .voltage (the pressure conversion path) must see
        # the median, not the glitched sample.
        adc = MedianFilterADC(SequenceADC([0.5, 0.5, 4.5]))
        adc.read()
        adc.read()
        adc.read()

        self.assertEqual(adc.voltage, 0.5)

    def test_sustained_change_passes_through(self):
        # A real change (several consecutive samples) must not be filtered out.
        adc = MedianFilterADC(SequenceADC([0.5, 0.5, 2.5, 2.5, 2.5]))
        for _ in range(5):
            adc.read()

        self.assertEqual(adc.voltage, 2.5)

    def test_voltage_delegates_before_first_read(self):
        inner = SequenceADC([1.0])
        inner.read()
        adc = MedianFilterADC(inner)

        self.assertEqual(adc.voltage, 1.0)

    def test_window_must_be_positive(self):
        # window=0 would silently disable the filter and double every
        # hardware read (deque(maxlen=0) discards all samples).
        with self.assertRaises(ValueError):
            MedianFilterADC(SequenceADC([0.5]), window=0)


class TestPressureAutoCalibration(unittest.TestCase):
    def _sensor(self, voltages, **kwargs):
        return PressureAnalogSensor(
            adc=SequenceADC(voltages), name="Boiler", max_pressure_mpa=0.5, **kwargs
        )

    def test_single_low_glitch_does_not_shift_offset(self):
        # One glitched sample below zero must not permanently raise the
        # baseline: with the old logic a single 0.1V glitch added ~0.5 Bar
        # of phantom pressure to every later reading.
        sensor = self._sensor([0.5, 0.4, 0.5, 0.5, 0.5])
        for _ in range(5):
            sensor.read()

        self.assertEqual(sensor.offset_voltage, 0.0)
        self.assertAlmostEqual(sensor.bar, 0.0)

    def test_low_readings_alone_do_not_move_the_zero(self):
        # A run of low samples is not a known zero. The old ratchet treated
        # five of them as drift and permanently lowered the offset.
        sensor = self._sensor([0.45] * 6)
        for _ in range(6):
            sensor.read()

        self.assertEqual(sensor.offset_voltage, 0.0)

    def test_a_low_but_connected_reading_is_not_a_fault(self):
        sensor = self._sensor([0.2] * 3)
        for _ in range(3):
            sensor.read()

        self.assertEqual(sensor.offset_voltage, 0.0)
        self.assertFalse(sensor._disconnected)

    def test_mpa_conversion_is_linear_over_sensor_range(self):
        # 0.5V -> 0 MPa, 4.5V -> max_pressure_mpa.
        sensor = self._sensor([2.5])
        sensor.read()

        self.assertAlmostEqual(sensor.mpa, ((2.5 - 0.5) / 4.0) * 0.5)


class HoldADC(ADCInterface):
    """One voltage, with an optional supply the span check can see."""

    def __init__(self, voltage, max_voltage=5.0):
        self._voltage = voltage
        self.max_voltage = max_voltage

    def set(self, voltage):
        self._voltage = voltage

    def read(self):
        return min(self._voltage / self.max_voltage, 1.0)

    @property
    def voltage(self):
        return self._voltage


class TestPressureVoltageRange(unittest.TestCase):
    def _sensor(self, voltage, **kwargs):
        supply = kwargs.pop("supply", 5.0)
        return PressureAnalogSensor(
            adc=HoldADC(voltage, max_voltage=supply),
            name="Head",
            max_pressure_mpa=2.0,
            **kwargs,
        )

    def test_default_span_is_unchanged(self):
        # 0.5 V is zero, 4.5 V is full scale, and the midpoint is linear.
        sensor = self._sensor(0.5)
        self.assertEqual(sensor.min_voltage, PressureAnalogSensor.SENSOR_MIN_VOLTAGE)
        self.assertEqual(sensor.max_voltage, PressureAnalogSensor.SENSOR_MAX_VOLTAGE)
        self.assertEqual(sensor.voltage_range, 4.0)
        self.assertAlmostEqual(sensor.mpa, 0.0)
        sensor.adc.set(4.5)
        self.assertAlmostEqual(sensor.mpa, 2.0)
        sensor.adc.set(2.5)
        self.assertAlmostEqual(sensor.mpa, 1.0)

    def test_custom_span_maps_zero_full_scale_and_midpoint(self):
        # A 3.3 V ratiometric transducer: 0.33 V at zero, 2.97 V at full scale.
        sensor = self._sensor(0.33, min_voltage=0.33, max_voltage=2.97)
        self.assertAlmostEqual(sensor.mpa, 0.0)
        sensor.adc.set(2.97)
        self.assertAlmostEqual(sensor.mpa, 2.0)
        sensor.adc.set((0.33 + 2.97) / 2)
        self.assertAlmostEqual(sensor.mpa, 1.0)

    def test_outside_the_span_clamps_the_way_it_always_has(self):
        # Below the zero voltage is 0 pressure. Above full scale the
        # conversion keeps climbing; it has never been capped.
        sensor = self._sensor(0.0, min_voltage=0.33, max_voltage=2.97)
        self.assertAlmostEqual(sensor.mpa, 0.0)
        sensor.adc.set(3.5)
        span = 2.97 - 0.33
        self.assertAlmostEqual(sensor.mpa, ((3.5 - 0.33) / span) * 2.0)
        self.assertGreater(sensor.mpa, 2.0)

    def test_default_span_still_starts_on_a_lower_reference(self):
        # Leaving the range unset must not disable a 3.3 V ADC.
        sensor = self._sensor(0.5, supply=3.3)
        self.assertEqual((sensor.min_voltage, sensor.max_voltage), (0.5, 4.5))

    def test_min_must_be_below_max(self):
        with self.assertRaises(ValueError) as caught:
            self._sensor(1.0, min_voltage=4.5, max_voltage=0.5)
        self.assertIn("must be below", str(caught.exception))
        with self.assertRaises(ValueError):
            self._sensor(1.0, min_voltage=1.0, max_voltage=1.0)

    def test_explicit_range_must_sit_inside_the_adc_supply(self):
        with self.assertRaises(ValueError) as caught:
            self._sensor(1.0, supply=3.3, min_voltage=0.2, max_voltage=4.0)
        self.assertIn("outside the ADC supply", str(caught.exception))
        with self.assertRaises(ValueError):
            self._sensor(1.0, min_voltage=-0.1, max_voltage=4.0)
        with self.assertRaises(ValueError):
            self._sensor(1.0, min_voltage=float("nan"), max_voltage=4.0)
        # No published full-scale means the 5 V supply the defaults assume.
        with self.assertRaises(ValueError):
            PressureAnalogSensor(
                adc=SequenceADC([1.0]),
                name="Head",
                max_pressure_mpa=2.0,
                min_voltage=0.2,
                max_voltage=6.0,
            )
        wrapped = MedianFilterADC(HoldADC(1.0, max_voltage=3.3))
        with self.assertRaises(ValueError):
            PressureAnalogSensor(
                adc=wrapped,
                name="Boiler",
                max_pressure_mpa=0.5,
                min_voltage=0.33,
                max_voltage=3.5,
            )

    def test_env_prefers_the_sensor_and_falls_back_to_the_shared_pair(self):
        self.assertEqual(pressure_voltages_from_env("Head", {}), (None, None))
        self.assertEqual(
            pressure_voltages_from_env(
                "Head", {"PRESSURE_V_MIN": "0.33", "PRESSURE_V_MAX": "2.97"}
            ),
            (0.33, 2.97),
        )
        self.assertEqual(
            pressure_voltages_from_env(
                "BOILER",
                {
                    "PRESSURE_V_MIN": "0.33",
                    "BOILER_V_MIN": "0.40",
                    "PRESSURE_V_MAX": "2.97",
                },
            ),
            (0.40, 2.97),
        )
        with self.assertRaises(ValueError) as caught:
            pressure_voltages_from_env("HEAD", {"HEAD_V_MAX": "nope"})
        self.assertIn("HEAD_V_MAX", str(caught.exception))
        with self.assertRaises(ValueError):
            pressure_voltages_from_env("HEAD", {"HEAD_V_MIN": "nan"})

    def test_bad_env_disables_only_that_sensor(self):
        def builder(pin, gain):
            return HoldADC(0.5, max_voltage=5.0)

        with main._temporary_environ(
            {"HEAD_V_MIN": "0.33", "HEAD_V_MAX": "2.97", "BOILER_V_MAX": "9"}
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                sensors = main.build_analog_devices(
                    adc_builder=builder, flow_sample_interval=None
                )
        text = output.getvalue()
        self.assertIn("WARNING: Boiler sensor disabled", text)
        self.assertIn("outside the ADC supply", text)
        names = [sensor.name for sensor in sensors]
        self.assertEqual(names, ["Head", "Brew"])
        head = sensors[0]
        self.assertEqual((head.min_voltage, head.max_voltage), (0.33, 2.97))


if __name__ == "__main__":
    unittest.main()
