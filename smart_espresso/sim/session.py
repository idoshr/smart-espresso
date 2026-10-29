"""Build a simulated monitor and step it on a virtual clock."""

import os
from contextlib import contextmanager
from unittest import mock

from smart_espresso.analog_sensor.analog_sensor import MedianFilterADC
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.shot_history import ShotHistory
from smart_espresso.sim.adc import CsvRecorder, ReplayADC, SignalADC, load_recording
from smart_espresso.sim.machine import SimulatedMachine, reference_program
from smart_espresso.sim.sensors import SimDHT, SimOLED
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer


def build_sim_sensors(machine, recorder=None, sample_interval=None):
    """Head, boiler and flow using the real sensor classes and fake ADCs.

    ``sample_interval=None`` samples flow on ``read`` (tests, replay). The
    live process passes the normal 5 ms background period.
    """
    head_adc = SignalADC(machine, SimulatedMachine.HEAD, recorder=recorder)
    boiler_adc = SignalADC(machine, SimulatedMachine.BOILER, recorder=recorder)
    flow_adc = SignalADC(machine, SimulatedMachine.FLOW, recorder=recorder)
    sensors = [
        PressureAnalogSensor(
            adc=MedianFilterADC(head_adc),
            name="Head",
            max_pressure_mpa=2.0,
        ),
        PressureAnalogSensor(
            adc=MedianFilterADC(boiler_adc),
            name="Boiler",
            max_pressure_mpa=0.5,
        ),
        WaterFlowAnalogSensor(
            adc=flow_adc,
            name="Brew",
            sample_interval=sample_interval,
        ),
    ]
    return sensors


def build_replay_sensors(path, sample_interval=None):
    channels = load_recording(path)
    flow = WaterFlowAnalogSensor(
        adc=ReplayADC(channels.get(2, [])),
        name="Brew",
        sample_interval=sample_interval,
    )
    sensors = [
        PressureAnalogSensor(
            adc=MedianFilterADC(ReplayADC(channels.get(0, []))),
            name="Head",
            max_pressure_mpa=2.0,
        ),
        PressureAnalogSensor(
            adc=MedianFilterADC(ReplayADC(channels.get(1, []))),
            name="Boiler",
            max_pressure_mpa=0.5,
        ),
        flow,
    ]
    return sensors


def build_sim_app(
    machine=None,
    seed=59,
    segments=None,
    loop=False,
    recorder=None,
    sample_interval=None,
    port=8093,
    history=None,
    tank=None,
):
    """Wire fake sensors into the real server, classifier and (fake) OLED."""
    if machine is None:
        machine = SimulatedMachine(seed=seed, segments=segments, loop=loop)
    sensors = build_sim_sensors(
        machine, recorder=recorder, sample_interval=sample_interval
    )
    if history is None:
        history = ShotHistory(state_path=None)
    if tank is None:
        tank = WaterTank(state_path=None)
    server = StatusServer(port=port, tank=tank, shot_history=history)
    display = SimOLED()
    app = SmartEspresso(
        analog_devices=sensors,
        digital_sensors=[SimDHT(machine)],
        client_ha=None,
        display=display,
        web_server=server,
        render_interval=0,
        ha_update_interval=0,
    )
    return app


def drive(app, clock, seconds, sample_dt=0.005, render_dt=0.1):
    """Step ``clock`` and the sensors. Flow is sampled every ``sample_dt``.

    The render loop (classifier, status, OLED) runs every ``render_dt``.
    Returns the published machine state at each render, with repeats kept.
    """
    origin = clock.now
    steps = int(round(seconds / sample_dt))
    render_every = max(int(round(render_dt / sample_dt)), 1)
    states = []
    flow_sensors = [s for s in app.analog_devices if hasattr(s, "flow_rate")]
    for index in range(1, steps + 1):
        clock.now = round(origin + index * sample_dt, 6)
        for sensor in flow_sensors:
            if sensor.sample_interval is None:
                sensor.read()
        if index % render_every == 0:
            app._tick()
            if app.web_server is not None:
                states.append(app.web_server.classifier.state)
    return states


def collapsed(states):
    """Drop repeated states so a golden file can list the sequence."""
    sequence = []
    for state in states:
        if not sequence or sequence[-1] != state:
            sequence.append(state)
    return sequence


# Modules that call ``time.monotonic`` while a virtual clock is running.
CLOCK_MODULES = (
    "smart_espresso.sim.adc",
    "smart_espresso.sim.sensors",
    "smart_espresso.analog_sensor.water_flow_sensor",
    "smart_espresso.machine_state",
    "smart_espresso.web.status_server",
    "smart_espresso.smart_espresso",
)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@contextmanager
def virtual_clock(clock=None):
    """Patch every ``monotonic`` the sensors and the classifier consult."""
    clock = clock or _Clock()
    patches = [mock.patch(f"{name}.monotonic", clock) for name in CLOCK_MODULES]
    for patch in patches:
        patch.start()
    try:
        yield clock
    finally:
        for patch in patches:
            patch.stop()


def record_reference(path, seed=59, sample_dt=0.005):
    """Write the 3-shot + flush session to ``path`` and return the app."""
    machine = SimulatedMachine(seed=seed, segments=reference_program(), loop=False)
    recorder = CsvRecorder(path)
    with virtual_clock() as clock:
        app = build_sim_app(machine=machine, recorder=recorder, sample_interval=None)
        try:
            drive(app, clock, machine.duration, sample_dt=sample_dt)
        finally:
            recorder.close()
    return app


def replay_recording(path, sample_dt=0.005, render_dt=0.1):
    """Play a recording through the real sensors and classifier.

    Returns ``(app, states)``. The clock is virtual, so a long trace does
    not take wall time.
    """
    channels = load_recording(path)
    duration = 0.0
    for samples in channels.values():
        if samples:
            duration = max(duration, samples[-1][0])
    with virtual_clock() as clock:
        sensors = [
            PressureAnalogSensor(
                adc=MedianFilterADC(ReplayADC(channels.get(0, []))),
                name="Head",
                max_pressure_mpa=2.0,
            ),
            PressureAnalogSensor(
                adc=MedianFilterADC(ReplayADC(channels.get(1, []))),
                name="Boiler",
                max_pressure_mpa=0.5,
            ),
            WaterFlowAnalogSensor(
                adc=ReplayADC(channels.get(2, [])),
                name="Brew",
                sample_interval=None,
            ),
        ]
        history = ShotHistory(state_path=None)
        server = StatusServer(
            port=0,
            tank=WaterTank(state_path=None),
            shot_history=history,
        )
        app = SmartEspresso(
            analog_devices=sensors,
            digital_sensors=[],
            display=SimOLED(),
            web_server=server,
            render_interval=0,
        )
        states = drive(app, clock, duration, sample_dt=sample_dt, render_dt=render_dt)
    return app, states


def sim_state_paths(base):
    """Shot log and tank file for a simulator run, under ``base``."""
    return (
        os.path.join(base, "sim_shot_history.json"),
        os.path.join(base, "sim_water_tank.json"),
    )
