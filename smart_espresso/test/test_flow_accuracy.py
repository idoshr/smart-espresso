"""Replay tests for shot-volume counting.

The launch claim is that millilitres are real. A 3-sample median on the
flow channel deletes one-sample pulses, so these tests drive a square wave
through ``WaterFlowAnalogSensor`` at the rate the sensor itself measures.
"""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from smart_espresso.analog_sensor.analog_sensor import ADCInterface, MedianFilterADC
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.shot_history import ShotHistory
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer

ROOT = Path(__file__).resolve().parents[2]


class HoldADC(ADCInterface):
    """ADC stub whose voltage the test changes between samples."""

    def __init__(self, voltage=0.0, max_voltage=3.3):
        self._voltage = voltage
        self._max_voltage = max_voltage

    def set(self, voltage):
        self._voltage = voltage

    def read(self):
        return min(self._voltage / self._max_voltage, 1.0)

    @property
    def voltage(self):
        return self._voltage


class WrappedADC(ADCInterface):
    """Decorator stand-in so a median filter can hide behind another ADC."""

    def __init__(self, adc):
        self.adc = adc

    def read(self):
        return self.adc.read()

    @property
    def voltage(self):
        return self.adc.voltage


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


class TestMedianFilterRejected(unittest.TestCase):
    def test_flow_sensor_refuses_a_median_filtered_adc(self):
        with self.assertRaises(ValueError) as caught:
            WaterFlowAnalogSensor(
                adc=MedianFilterADC(HoldADC(0.0), window=3), name="Brew"
            )
        self.assertIn("MedianFilterADC", str(caught.exception))

    def test_flow_sensor_refuses_a_median_filter_behind_a_wrapper(self):
        adc = WrappedADC(MedianFilterADC(HoldADC(0.0), window=3))
        with self.assertRaises(ValueError):
            WaterFlowAnalogSensor(adc=adc, name="Brew")

    def test_shipped_wiring_leaves_the_flow_channel_unfiltered(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("WaterFlowAnalogSensor(")
        head = source[start:].split("sample_interval", 1)[0]
        self.assertNotIn("MedianFilterADC", head)
        self.assertIn("sample_interval", source[start : start + 400])
        self.assertIn("WaterFlowAnalogSensor.DEFAULT_SAMPLE_INTERVAL", source)


class TestFlowReplay(unittest.TestCase):
    # Long enough that a couple of pulses on the window edge stay inside
    # ±1% at 0.5 ml/s (about 353 pulses; three pulses is 0.85%).
    DURATION = 120.0

    def _replay(self, flow_mls, duty, phase):
        interval = WaterFlowAnalogSensor.DEFAULT_SAMPLE_INTERVAL
        pulses_per_liter = WaterFlowAnalogSensor.DEFAULT_PULSES_PER_LITER
        pulse_hz = flow_mls * pulses_per_liter / 1000.0
        period = 1.0 / pulse_hz
        high_time = period * duty
        clock = FakeClock(0.0)
        adc = HoldADC(0.0)
        with mock.patch(
            "smart_espresso.analog_sensor.water_flow_sensor.monotonic", clock
        ):
            sensor = WaterFlowAnalogSensor(
                adc=adc,
                name="Brew",
                pulses_per_liter=pulses_per_liter,
                flow_interval=self.DURATION,
            )
            adc.set(0.0)
            # The rate log is covered by the warning test; keep this sweep quiet.
            with redirect_stdout(io.StringIO()):
                sensor.read()  # latch idle-low before the pour
                steps = int(round(self.DURATION / interval))
                for index in range(1, steps + 1):
                    clock.now = index * interval
                    wave_t = clock.now + phase * period
                    adc.set(3.3 if (wave_t % period) < high_time else 0.0)
                    sensor.read()
            return sensor.pulses, pulse_hz * self.DURATION, sensor.sample_hz

    def test_counts_within_one_percent_at_the_measured_sample_rate(self):
        flows = (0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        duties = (0.30, 0.40, 0.50, 0.60, 0.70)
        phases = (0.0, 0.5)
        interval = WaterFlowAnalogSensor.DEFAULT_SAMPLE_INTERVAL
        for flow_mls in flows:
            for duty in duties:
                for phase in phases:
                    with self.subTest(flow_mls=flow_mls, duty=duty, phase=phase):
                        counted, expected, measured_hz = self._replay(
                            flow_mls, duty, phase
                        )
                        self.assertAlmostEqual(measured_hz, 1.0 / interval, delta=0.1)
                        error = abs(counted - expected) / expected
                        self.assertLessEqual(
                            error,
                            0.01,
                            f"counted {counted}, expected {expected:.2f}, "
                            f"sample_hz {measured_hz}",
                        )

    def test_status_reports_the_measured_sample_rate(self):
        clock = FakeClock(0.0)
        adc = HoldADC(0.0)
        interval = WaterFlowAnalogSensor.DEFAULT_SAMPLE_INTERVAL
        server = StatusServer(
            port=0,
            tank=WaterTank(state_path=None),
            shot_history=ShotHistory(state_path=None),
        )
        with mock.patch(
            "smart_espresso.analog_sensor.water_flow_sensor.monotonic", clock
        ):
            sensor = WaterFlowAnalogSensor(adc=adc, name="Brew", pulses_per_liter=100)
            sensor.read()
            server.update([sensor])
            self.assertIsNone(server.status_payload()["flow"][0]["sample_hz"])

            steps = int(round(1.0 / interval))
            for index in range(1, steps + 1):
                clock.now = index * interval
                sensor.read()
            server.update([sensor])

        entry = server.status_payload()["flow"][0]
        self.assertEqual(entry["sample_hz"], round(1.0 / interval, 1))

    def test_warns_when_the_pulse_rate_exceeds_the_sample_rate(self):
        # 20 Hz sampling cannot honestly count a 10 Hz pulse train: the
        # short phase of a non-50% duty cycle falls between samples.
        # 10 Hz > 20 / 2.5.
        clock = FakeClock(0.0)
        adc = HoldADC(0.0)
        interval = 0.05
        with mock.patch(
            "smart_espresso.analog_sensor.water_flow_sensor.monotonic", clock
        ):
            sensor = WaterFlowAnalogSensor(
                adc=adc,
                name="Brew",
                pulses_per_liter=5880,
                flow_interval=1.0,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                adc.set(0.0)
                sensor.read()
                high = False
                for index in range(1, 21):
                    clock.now = index * interval
                    high = not high
                    adc.set(3.3 if high else 0.0)
                    sensor.read()
        text = output.getvalue()
        self.assertIn("exceeds", text)
        self.assertIn("sample rate", text)


if __name__ == "__main__":
    unittest.main()
