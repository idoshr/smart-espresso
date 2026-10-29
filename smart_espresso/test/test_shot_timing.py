"""Shot seconds and millilitres come from pulses, not the 1 s flow average."""
import io
import random
import unittest
from contextlib import redirect_stdout
from unittest import mock

from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.machine_state import MachineState
from smart_espresso.shot_history import ShotHistory
from smart_espresso.test.test_flow_accuracy import HoldADC
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer

PULSES_PER_LITER = 5880.0
POUR_SECONDS = 25.0
POUR_ML = 50.0
SAMPLE_SECONDS = 0.005
# 0.5 V is 0 bar. 2.3 V is 9 bar on a 0–2 MPa head sensor.
# 1.46 V is 1.2 bar on a 0–0.5 MPa boiler sensor.
HEAD_VOLT = 2.3
BOILER_VOLT = 1.46
IDLE_VOLT = 0.5


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _pour(seed_bits, stall):
    """One 25 s / 50 ml pull. ``stall`` freezes the render loop for 5 s."""
    duty, phase = seed_bits
    clock = Clock()
    pulse_hz = POUR_ML / 1000.0 * PULSES_PER_LITER / POUR_SECONDS
    period = 1.0 / pulse_hz
    high = period * duty
    patches = [
        mock.patch("smart_espresso.analog_sensor.water_flow_sensor.monotonic", clock),
        mock.patch("smart_espresso.machine_state.monotonic", clock),
        mock.patch("smart_espresso.web.status_server.monotonic", clock),
    ]
    for patch in patches:
        patch.start()
    try:
        history = ShotHistory(state_path=None)
        server = StatusServer(
            port=0,
            tank=WaterTank(state_path=None),
            shot_history=history,
        )
        flow_adc = HoldADC(0.0)
        head = PressureAnalogSensor(
            adc=HoldADC(IDLE_VOLT), name="Head", max_pressure_mpa=2.0
        )
        boiler = PressureAnalogSensor(
            adc=HoldADC(BOILER_VOLT), name="Boiler", max_pressure_mpa=0.5
        )
        flow = WaterFlowAnalogSensor(
            adc=flow_adc,
            name="Brew",
            pulses_per_liter=PULSES_PER_LITER,
            flow_interval=1.0,
        )
        head_adc = head.adc
        pour_at = 1.0
        pour_end = pour_at + POUR_SECONDS
        stall_at = pour_at + 10.0
        stall_end = stall_at + 5.0

        sample_index = 0

        def step(flowing):
            nonlocal sample_index
            sample_index += 1
            if flowing:
                wave = (clock.now - pour_at + phase * period) % period
                flow_adc.set(3.3 if wave < high else 0.0)
            else:
                flow_adc.set(0.0)
            flow.read()
            frozen = stall and stall_at <= clock.now < stall_end
            if sample_index % 20 == 0 and not frozen:
                head_adc.set(HEAD_VOLT if flowing else IDLE_VOLT)
                head.read()
                boiler.read()
                server.update([head, boiler, flow])

        with redirect_stdout(io.StringIO()):
            while clock.now < pour_at:
                clock.now += SAMPLE_SECONDS
                step(False)
            while clock.now < pour_end:
                clock.now += SAMPLE_SECONDS
                step(True)
            settle_end = clock.now + 2.5
            while clock.now < settle_end:
                clock.now += SAMPLE_SECONDS
                step(False)
        shot = history.list_payload()["shots"]
        return (shot[0] if shot else None), flow.pulses
    finally:
        for patch in patches:
            patch.stop()


class TestShotTiming(unittest.TestCase):
    def test_random_pulls_match_pulses_including_a_render_stall(self):
        rng = random.Random(56)
        ml_per_pulse = 1000.0 / PULSES_PER_LITER
        worst_seconds = 0.0
        worst_pulses = 0.0
        saw_stall = False
        for index in range(300):
            duty = 0.35 + rng.random() * 0.30
            phase = rng.random()
            stall = index == 17
            shot, pulses = _pour((duty, phase), stall)
            self.assertIsNotNone(shot, f"pull {index} was not recorded")
            seconds_error = abs(shot["seconds"] - POUR_SECONDS)
            pulse_error = abs(shot["ml"] - pulses * ml_per_pulse) / ml_per_pulse
            worst_seconds = max(worst_seconds, seconds_error)
            worst_pulses = max(worst_pulses, pulse_error)
            self.assertLessEqual(
                seconds_error,
                0.2,
                f"pull {index} seconds {shot['seconds']} off by {seconds_error}",
            )
            self.assertLessEqual(
                pulse_error,
                1.0,
                f"pull {index} ml {shot['ml']} is {pulse_error:.2f} pulses "
                f"from {pulses} counted",
            )
            if stall:
                saw_stall = True
                self.assertLessEqual(seconds_error, 0.2)
                self.assertLessEqual(pulse_error, 1.0)
        self.assertTrue(saw_stall)
        self.assertLessEqual(worst_seconds, 0.2)
        self.assertLessEqual(worst_pulses, 1.0)

    def test_one_pulse_is_not_flow_at_either_calibration(self):
        for pulses_per_liter in (450.0, 5880.0):
            with self.subTest(pulses_per_liter=pulses_per_liter):
                state = _one_pulse(pulses_per_liter)
                self.assertNotEqual(state, MachineState.BREWING)


def _one_pulse(pulses_per_liter):
    clock = Clock()
    patches = [
        mock.patch("smart_espresso.analog_sensor.water_flow_sensor.monotonic", clock),
        mock.patch("smart_espresso.machine_state.monotonic", clock),
        mock.patch("smart_espresso.web.status_server.monotonic", clock),
    ]
    for patch in patches:
        patch.start()
    try:
        server = StatusServer(
            port=0,
            tank=WaterTank(state_path=None),
            shot_history=ShotHistory(state_path=None),
        )
        flow_adc = HoldADC(0.0)
        head = PressureAnalogSensor(
            adc=HoldADC(IDLE_VOLT), name="Head", max_pressure_mpa=2.0
        )
        boiler = PressureAnalogSensor(
            adc=HoldADC(BOILER_VOLT), name="Boiler", max_pressure_mpa=0.5
        )
        flow = WaterFlowAnalogSensor(
            adc=flow_adc,
            name="Brew",
            pulses_per_liter=pulses_per_liter,
            flow_interval=1.0,
        )
        with redirect_stdout(io.StringIO()):
            flow.read()  # latch idle-low
            head.read()
            boiler.read()
            server.update([head, boiler, flow])
            clock.now = 0.2
            flow_adc.set(3.3)
            flow.read()
            clock.now = 1.2
            flow_adc.set(0.0)
            flow.read()
            head.read()
            boiler.read()
            server.update([head, boiler, flow])
        return server.classifier.state
    finally:
        for patch in patches:
            patch.stop()


if __name__ == "__main__":
    unittest.main()
