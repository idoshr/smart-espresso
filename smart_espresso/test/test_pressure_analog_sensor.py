import unittest

from smart_espresso.analog_sensor.analog_sensor import ADCInterface, MedianFilterADC
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor


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

    def test_sustained_low_readings_recalibrate_offset(self):
        # Five consecutive low samples are a real drift, not noise.
        sensor = self._sensor([0.45] * 6)
        for _ in range(6):
            sensor.read()

        self.assertAlmostEqual(sensor.offset_voltage, -0.05)
        self.assertAlmostEqual(sensor.bar, 0.0)

    def test_calibration_is_capped_at_minus_0_1v(self):
        sensor = self._sensor([0.2] * 10)
        for _ in range(10):
            sensor.read()

        self.assertEqual(sensor.offset_voltage, -0.1)

    def test_mpa_conversion_is_linear_over_sensor_range(self):
        # 0.5V -> 0 MPa, 4.5V -> max_pressure_mpa.
        sensor = self._sensor([2.5])
        sensor.read()

        self.assertAlmostEqual(sensor.mpa, ((2.5 - 0.5) / 4.0) * 0.5)


if __name__ == "__main__":
    unittest.main()
