import time
import unittest

from smart_espresso.analog_sensor.analog_sensor import ADCInterface
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor


class FakeADC(ADCInterface):
    """ADC stub that plays back a predefined sequence of voltages."""

    def __init__(self, voltages, max_voltage=3.3):
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


class TestWaterFlowAnalogSensor(unittest.TestCase):
    def test_counts_one_pulse_per_rising_edge(self):
        # Three clean pulses (low -> high -> low each time).
        voltages = [0.0, 3.0, 0.0, 3.0, 0.0, 3.0, 0.0]
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC(voltages), name="Brew", pulses_per_liter=100
        )
        for _ in voltages:
            sensor.read()

        self.assertEqual(sensor.pulses, 3)
        self.assertAlmostEqual(sensor.liter, 0.03)

    def test_hysteresis_ignores_noise_between_thresholds(self):
        # Voltage wobbling in the dead band must not create extra pulses.
        voltages = [0.0, 3.0, 1.5, 2.0, 1.5, 2.2, 0.0, 3.0, 0.0]
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC(voltages), name="Brew", pulses_per_liter=100
        )
        for _ in voltages:
            sensor.read()

        # Only two genuine low->high crossings, despite the mid-band wobble.
        self.assertEqual(sensor.pulses, 2)

    def test_inverted_sensor_counts_falling_edges(self):
        # An idle-high (open-collector + pull-up) sensor pulses low.
        voltages = [3.0, 0.0, 3.0, 0.0, 3.0]
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC(voltages), name="Brew", pulses_per_liter=100
        )
        for _ in voltages:
            sensor.read()

        self.assertEqual(sensor.pulses, 2)
        self.assertAlmostEqual(sensor.liter, 0.02)

    def test_idle_high_startup_counts_no_phantom_pulse(self):
        # Latching the idle level must not count as a pulse.
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([3.0, 3.0, 3.0, 3.0]), name="Brew", pulses_per_liter=100
        )
        for _ in range(4):
            sensor.read()

        self.assertEqual(sensor.pulses, 0)

    def test_reset_keeps_the_polarity_latch(self):
        # Re-syncing after a reset would count the idle level as a pulse.
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([3.0, 0.0, 3.0]), name="Brew", pulses_per_liter=100
        )
        for _ in range(3):
            sensor.read()
        self.assertEqual(sensor.pulses, 1)

        sensor.reset()
        sensor.read()  # idle-high again: must not count
        self.assertEqual(sensor.pulses, 0)

    def test_threaded_read_returns_a_float_immediately(self):
        # The warm-up sample in __init__ fills the cache, so even with a long
        # interval (thread never fires during the test) read() is never None.
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([1.5]),
            name="Brew",
            pulses_per_liter=100,
            sample_interval=60.0,
        )
        self.addCleanup(sensor.stop)

        self.assertAlmostEqual(sensor.read(), 1.5 / 3.3)

    def test_background_sampler_counts_pulses(self):
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([0.0, 3.0, 0.0, 3.0, 0.0, 3.0, 0.0]),
            name="Brew",
            pulses_per_liter=100,
            sample_interval=0.001,
        )
        self.addCleanup(sensor.stop)

        deadline = time.monotonic() + 5.0
        while sensor.pulses < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sensor.pulses, 3)

    def test_reset_zeroes_volume(self):
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([0.0, 3.0, 0.0]), name="Brew", pulses_per_liter=100
        )
        for _ in range(3):
            sensor.read()
        self.assertEqual(sensor.pulses, 1)

        sensor.reset()
        self.assertEqual(sensor.pulses, 0)
        self.assertEqual(sensor.liter, 0.0)
        self.assertEqual(sensor.flow_rate, 0.0)

    def test_message_and_unit(self):
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([0.0]), name="Brew", pulses_per_liter=100
        )
        self.assertEqual(WaterFlowAnalogSensor.unit_of_measurement(), "L")
        self.assertEqual(sensor.message, "Brew: 0.0 L")

    def test_invalid_thresholds_raise(self):
        with self.assertRaises(ValueError):
            WaterFlowAnalogSensor(
                adc=FakeADC([0.0]),
                name="Brew",
                high_threshold=1.0,
                low_threshold=2.0,
            )


if __name__ == "__main__":
    unittest.main()
