import unittest

from smart_espresso.analog_sensor.water_flow_sensor import (
    GPIOWaterFlowSensor,
    WaterFlowAnalogSensor,
)


class FakePulseSource:
    """Stand-in for a gpiozero.Button; lets tests drive pulses without hardware."""

    def __init__(self):
        self.when_pressed = None

    def pulse(self, times=1):
        for _ in range(times):
            if self.when_pressed:
                self.when_pressed()


class TestGPIOWaterFlowSensor(unittest.TestCase):
    def _make_sensor(self, **kwargs):
        source = FakePulseSource()
        sensor = GPIOWaterFlowSensor(
            name=kwargs.pop("name", "Brew"),
            pulse_source=source,
            **kwargs,
        )
        return sensor, source

    def test_counts_pulses_and_converts_to_litres(self):
        sensor, source = self._make_sensor(pulses_per_litre=100)
        source.pulse(50)
        self.assertEqual(sensor.pulses, 50)
        self.assertAlmostEqual(sensor.liter, 0.5)

    def test_read_returns_litres_without_adc(self):
        # The base AnalogSensor.read() would call self.adc.read(); a flow sensor
        # has no ADC, so read() must be overridden and must not raise.
        sensor, source = self._make_sensor(pulses_per_litre=1000)
        source.pulse(250)
        self.assertIsNone(sensor.adc)
        self.assertAlmostEqual(sensor.read(), 0.25)
        self.assertAlmostEqual(sensor.value, 0.25)

    def test_reset_zeroes_the_counter(self):
        sensor, source = self._make_sensor(pulses_per_litre=100)
        source.pulse(10)
        self.assertEqual(sensor.pulses, 10)
        sensor.reset()
        self.assertEqual(sensor.pulses, 0)
        self.assertEqual(sensor.liter, 0.0)

    def test_message_and_unit(self):
        sensor, source = self._make_sensor(pulses_per_litre=100)
        source.pulse(25)
        self.assertEqual(sensor.message, "Brew: 0.25 L")
        self.assertEqual(sensor.unit_of_measurement(), "L")
        self.assertAlmostEqual(sensor.normalized_value, 0.25)

    def test_invalid_pulses_per_litre_rejected(self):
        with self.assertRaises(ValueError):
            self._make_sensor(pulses_per_litre=0)
        with self.assertRaises(ValueError):
            self._make_sensor(pulses_per_litre=-10)
        with self.assertRaises(TypeError):
            self._make_sensor(pulses_per_litre="lots")

    def test_invalid_gpio_pin_rejected(self):
        with self.assertRaises(ValueError):
            self._make_sensor(gpio_pin=-1)
        with self.assertRaises(ValueError):
            self._make_sensor(gpio_pin=True)  # bool is not a valid pin

    def test_invalid_name_rejected(self):
        with self.assertRaises(ValueError):
            self._make_sensor(name="   ")

    def test_base_class_liter_is_abstract(self):
        # WaterFlowAnalogSensor is an abstract base; liter must be provided by
        # a subclass and raises if used directly.
        class NoLiter(WaterFlowAnalogSensor):
            pass

        sensor = NoLiter(adc=None, name="X")
        with self.assertRaises(NotImplementedError):
            _ = sensor.liter


if __name__ == "__main__":
    unittest.main()
