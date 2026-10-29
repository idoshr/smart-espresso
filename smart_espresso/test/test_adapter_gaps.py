"""Hardware adapters, an empty median window, and a racing flow reset.

On main, before the fix, an empty ``MedianFilterADC`` window called
``adc.read()``, and ``WaterFlowAnalogSensor.reset()`` racing the sampler
produced a negative flow rate.
"""

import importlib
import sys
import types
import unittest

from smart_espresso.analog_sensor.analog_sensor import ADCInterface, MedianFilterADC
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.sim.machine import (
    Segment,
    SimulatedMachine,
    demo_program,
    reference_program,
)
from smart_espresso.test.test_water_flow_sensor import FakeADC
from smart_espresso.utils import strtobool


class _CountingADC(ADCInterface):
    def __init__(self):
        self.reads = 0
        self.voltage_reads = 0

    def read(self):
        self.reads += 1
        return 0.4

    @property
    def voltage(self):
        self.voltage_reads += 1
        return 1.25


class TestMedianFilter(unittest.TestCase):
    def test_empty_window_does_not_touch_the_adc(self):
        adc = _CountingADC()
        wrapped = MedianFilterADC(adc, window=3)
        self.assertEqual(wrapped.filtered_value, 0.0)
        self.assertEqual(adc.reads, 0)
        self.assertEqual(wrapped.voltage, 1.25)
        self.assertEqual(adc.reads, 0)
        self.assertEqual(adc.voltage_reads, 1)


class TestFlowResetRace(unittest.TestCase):
    def test_a_torn_pulse_window_does_not_go_negative(self):
        # reset() used to zero the pulse total under the lock and the window
        # baseline just after it. The sampler can observe total 0 with the
        # old baseline still in place, and the rate comes out negative.
        sensor = WaterFlowAnalogSensor(
            adc=FakeADC([0.0]),
            name="Brew",
            pulses_per_liter=100,
            flow_interval=0.01,
        )
        sensor._pulses = 0
        sensor._flow_window_pulses = 40
        sensor._flow_window_start = 0.0
        sensor._update_rate(1.0)
        self.assertGreaterEqual(sensor.flow_rate, 0.0)


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class TestADS1115GainPerRead(unittest.TestCase):
    def test_two_channels_apply_their_own_gain_and_clamp(self):
        seen = []

        class _ADS:
            def __init__(self, i2c, address, gain, data_rate):
                self.gain = gain
                self.data_rate = data_rate
                self.address = address

        class _Channel:
            def __init__(self, ads, pin):
                self.ads = ads
                self.pin = pin
                self.voltage_reads = 0

            @property
            def voltage(self):
                self.voltage_reads += 1
                seen.append((self.pin, self.ads.gain, self.ads.data_rate))
                return {0: 2.048, 1: -0.2, 2: 9.0}[self.pin]

            @property
            def value(self):
                seen.append(("raw", self.pin, self.ads.gain))
                return 4321

        ads_mod = _module(
            "adafruit_ads1x15.ads1115", ADS1115=_ADS, P0=0, P1=1, P2=2, P3=3
        )
        analog_mod = _module("adafruit_ads1x15.analog_in", AnalogIn=_Channel)
        package = _module("adafruit_ads1x15")
        board = _module("board", SCL=object(), SDA=object())
        busio = _module("busio", I2C=lambda scl, sda: object())
        saved = {}
        names = (
            "adafruit_ads1x15",
            "adafruit_ads1x15.ads1115",
            "adafruit_ads1x15.analog_in",
            "board",
            "busio",
            "smart_espresso.analog_sensor.ads1115_analog_sensor",
        )
        for name in names:
            saved[name] = sys.modules.get(name)
        sys.modules["adafruit_ads1x15"] = package
        sys.modules["adafruit_ads1x15.ads1115"] = ads_mod
        sys.modules["adafruit_ads1x15.analog_in"] = analog_mod
        sys.modules["board"] = board
        sys.modules["busio"] = busio
        sys.modules.pop("smart_espresso.analog_sensor.ads1115_analog_sensor", None)
        try:
            module = importlib.import_module(
                "smart_espresso.analog_sensor.ads1115_analog_sensor"
            )
            module.ADS1115ADC._i2c = None
            module.ADS1115ADC._ads_instances = {}
            low = module.ADS1115ADC(pin=0, gain=2 / 3, data_rate=860)
            high = module.ADS1115ADC(pin=1, gain=2, data_rate=250)
            wide = module.ADS1115ADC(pin=2, gain=1, data_rate=860)
            self.assertIs(low.ads, high.ads)
            self.assertEqual(low.max_voltage, 6.144)
            self.assertEqual(high.max_voltage, 4.096 / 2)
            self.assertAlmostEqual(low.read(), 2.048 / 6.144)
            self.assertEqual(high.read(), 0.0)
            self.assertEqual(wide.read(), 1.0)
            self.assertEqual(seen[0], (0, 2 / 3, 860))
            self.assertEqual(seen[1], (1, 2, 250))
            self.assertEqual(seen[2], (2, 1, 860))
            reads_before = low.channel.voltage_reads
            self.assertAlmostEqual(low.voltage, 2.048)
            self.assertEqual(low.channel.voltage_reads, reads_before)
            self.assertEqual(low.raw_value, 4321)
            self.assertEqual(seen[-1], ("raw", 0, 2 / 3))
            with self.assertRaises(ValueError):
                module.ADS1115ADC(pin=9)
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous


class TestMCP3008(unittest.TestCase):
    def test_read_caches_voltage_from_one_sample(self):
        created = {}

        class _Pot:
            def __init__(self, pin):
                self.pin = pin
                self.max_voltage = 3.3
                self._value = 0.5
                self.value_reads = 0
                self.voltage_reads = 0
                created[pin] = self

            @property
            def value(self):
                self.value_reads += 1
                return self._value

            @property
            def voltage(self):
                self.voltage_reads += 1
                return self._value * self.max_voltage

        gpio = _module("gpiozero", MCP3008=_Pot)
        saved_gpio = sys.modules.get("gpiozero")
        saved_mod = sys.modules.get(
            "smart_espresso.analog_sensor.mcp3008_analog_sensor"
        )
        sys.modules["gpiozero"] = gpio
        sys.modules.pop("smart_espresso.analog_sensor.mcp3008_analog_sensor", None)
        try:
            module = importlib.import_module(
                "smart_espresso.analog_sensor.mcp3008_analog_sensor"
            )
            adc = module.MCP3008ADC(pin=3)
            pot = created[3]
            self.assertAlmostEqual(adc.voltage, 1.65)
            self.assertEqual(pot.voltage_reads, 1)
            self.assertAlmostEqual(adc.read(), 0.5)
            self.assertEqual(pot.value_reads, 1)
            voltage_reads = pot.voltage_reads
            self.assertAlmostEqual(adc.voltage, 1.65)
            self.assertEqual(pot.voltage_reads, voltage_reads)
            with self.assertRaises(ValueError):
                module.MCP3008ADC(pin=8)
        finally:
            if saved_gpio is None:
                sys.modules.pop("gpiozero", None)
            else:
                sys.modules["gpiozero"] = saved_gpio
            if saved_mod is None:
                sys.modules.pop(
                    "smart_espresso.analog_sensor.mcp3008_analog_sensor", None
                )
            else:
                sys.modules[
                    "smart_espresso.analog_sensor.mcp3008_analog_sensor"
                ] = saved_mod


class TestSimProgram(unittest.TestCase):
    def test_demo_and_reference_cover_the_scripted_machine(self):
        demo = demo_program()
        reference = reference_program()
        self.assertGreaterEqual(len(demo), 6)
        self.assertEqual(sum(segment.kind == "shot" for segment in reference), 3)
        machine = SimulatedMachine(seed=59, loop=True)
        self.assertIsNotNone(machine.segment_at(0.5))
        self.assertIsNotNone(machine.segment_at(machine.duration + 1.0))
        self.assertGreater(machine.voltage(SimulatedMachine.HEAD, 1.0), 0.0)
        self.assertGreater(machine.voltage(SimulatedMachine.BOILER, 4.0), 0.0)
        self.assertGreaterEqual(machine.voltage(SimulatedMachine.FLOW, 20.0), 0.0)
        fault = next(segment for segment in demo if segment.fault)
        with self.assertRaises(OSError):
            machine.voltage(SimulatedMachine.HEAD, fault.start + 0.1)
        with self.assertRaises(ValueError):
            machine.voltage(9, 1.0)
        empty = SimulatedMachine(seed=1, segments=[])
        self.assertIsNone(empty.segment_at(1.0))
        self.assertEqual(empty.voltage(SimulatedMachine.HEAD, 1.0), 0.5)
        self.assertEqual(empty.voltage(SimulatedMachine.FLOW, 1.0), 0.0)
        flat = Segment(start=0.0, end=0.0, kind="flat", boiler_from=0.2, boiler_bar=1.0)
        self.assertEqual(machine._boiler_bar(flat, 0.0), 1.0)
        self.assertEqual(machine.climate(0.0)[0] > 0, True)


class TestBuildFromEnv(unittest.TestCase):
    def test_sim_env_builds_fakes_without_the_web_server(self):
        from main import build_from_env

        devices, digital, panel, serve, ha = build_from_env(
            {
                "ADC_TYPE": "SIM",
                "WEB_ENABLE": "false",
                "DHT_ENABLE": "true",
                "DISPLAY_ENABLE": "true",
                "HA_ENABLE": "false",
                "SIM_SEED": "59",
            }
        )
        try:
            self.assertIsNone(serve)
            self.assertIsNone(ha)
            self.assertEqual(
                [sensor.name for sensor in devices], ["Head", "Boiler", "Brew"]
            )
            self.assertEqual(digital[0].name, "Environment")
            self.assertIsNotNone(panel)
            self.assertTrue(panel.sim_display)
        finally:
            for sensor in devices:
                stop = getattr(sensor, "stop", None)
                if stop is not None:
                    stop()

    def test_home_assistant_requires_a_url_and_token(self):
        from main import build_from_env

        with self.assertRaises(ValueError):
            build_from_env(
                {
                    "HA_ENABLE": "true",
                    "WEB_ENABLE": "false",
                    "DHT_ENABLE": "false",
                    "DISPLAY_ENABLE": "false",
                    "ADC_TYPE": "SIM",
                }
            )


class TestStrToBool(unittest.TestCase):
    def test_unknown_word_is_rejected(self):
        with self.assertRaises(TypeError):
            strtobool("maybe")


class TestDHTDriver(unittest.TestCase):
    def test_missing_board_pin_is_reported(self):
        board = _module("board")
        adafruit = _module("adafruit_dht")

        class _Device:
            def __init__(self, pin, use_pulseio=False):
                self.temperature = 20.0
                self.humidity = 40.0

            def exit(self):
                return None

        adafruit.DHT22 = _Device
        saved = {name: sys.modules.get(name) for name in ("board", "adafruit_dht")}
        sys.modules["board"] = board
        sys.modules["adafruit_dht"] = adafruit
        try:
            from smart_espresso.analog_sensor.dht22_sensor import DHT22Sensor

            with self.assertRaises(ImportError):
                DHT22Sensor(pin=4, interval=0.05)
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous


if __name__ == "__main__":
    unittest.main()
