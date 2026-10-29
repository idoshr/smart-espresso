"""A scripted espresso machine: voltages for head, boiler and flow.

The same object drives the live simulator and the reference recordings.
A seed only shifts the flow square wave, so a given seed is repeatable.
"""

from dataclasses import dataclass
from typing import Optional

# The flow sensor's default calibration. The square wave is generated in
# pulses so a replay through WaterFlowAnalogSensor lands on the same volume.
PULSES_PER_LITER = 5880.0

# 0.5 V is 0 bar. 4.5 V is full scale. Head full scale is 20 bar, boiler 5.
_HEAD_BAR_PER_VOLT = 20.0 / 4.0
_BOILER_BAR_PER_VOLT = 5.0 / 4.0


def head_voltage(bar: float) -> float:
    return 0.5 + max(bar, 0.0) / _HEAD_BAR_PER_VOLT


def boiler_voltage(bar: float) -> float:
    return 0.5 + max(bar, 0.0) / _BOILER_BAR_PER_VOLT


def flow_square(t: float, ml_s: float, phase: float) -> float:
    """Idle-low pulse train. 3.3 V while the pulse is high, else 0 V."""
    if ml_s <= 0:
        return 0.0
    period = 1000.0 / (ml_s * PULSES_PER_LITER)
    if ((t + phase) % period) < (period * 0.5):
        return 3.3
    return 0.0


@dataclass
class Segment:
    """One stretch of the script. Times are seconds from the start."""

    start: float
    end: float
    kind: str
    head_bar: float = 0.0
    boiler_bar: float = 1.2
    boiler_from: Optional[float] = None
    flow_mls: float = 0.0
    fault: bool = False


def _add(segments, cursor, duration, **kwargs):
    end = cursor + duration
    segments.append(Segment(start=cursor, end=end, **kwargs))
    return end


def demo_program():
    """One looping cycle: off, heat, shot, flush, steam, a brief fault."""
    segments = []
    t = 0.0
    t = _add(segments, t, 3.0, kind="off", boiler_bar=0.05, head_bar=0.0)
    t = _add(
        segments,
        t,
        8.0,
        kind="heat",
        boiler_from=0.05,
        boiler_bar=1.2,
        head_bar=0.0,
    )
    t = _add(segments, t, 3.0, kind="ready", boiler_bar=1.2)
    t = _add(
        segments,
        t,
        26.0,
        kind="shot",
        head_bar=9.0,
        boiler_bar=1.2,
        flow_mls=1.5,
    )
    t = _add(segments, t, 4.0, kind="settle", boiler_bar=1.2)
    t = _add(
        segments,
        t,
        4.0,
        kind="flush",
        head_bar=0.4,
        boiler_bar=1.2,
        flow_mls=6.0,
    )
    t = _add(segments, t, 3.0, kind="settle", boiler_bar=1.2)
    t = _add(
        segments,
        t,
        5.0,
        kind="steam",
        boiler_from=1.4,
        boiler_bar=0.9,
        head_bar=0.0,
    )
    _add(segments, t, 2.0, kind="fault", boiler_bar=1.2, fault=True)
    return segments


def reference_program():
    """Three different pulls and one flush. This is the CI recording."""
    shots = (
        ("shot", 27.0, 9.0, 1.4),
        ("shot", 24.0, 8.5, 1.6),
        ("shot", 29.0, 9.2, 1.2),
    )
    segments = []
    t = 0.0
    t = _add(
        segments,
        t,
        8.0,
        kind="heat",
        boiler_from=0.3,
        boiler_bar=1.2,
    )
    for _kind, seconds, head, flow in shots:
        t = _add(segments, t, 2.0, kind="ready", boiler_bar=1.2)
        t = _add(
            segments,
            t,
            seconds,
            kind="shot",
            head_bar=head,
            boiler_bar=1.2,
            flow_mls=flow,
        )
        t = _add(segments, t, 4.0, kind="settle", boiler_bar=1.2)
    t = _add(segments, t, 2.0, kind="ready", boiler_bar=1.2)
    t = _add(
        segments,
        t,
        4.0,
        kind="flush",
        head_bar=0.4,
        boiler_bar=1.2,
        flow_mls=8.0,
    )
    _add(segments, t, 4.0, kind="settle", boiler_bar=1.2)
    return segments


class SimulatedMachine:
    """Voltages for channels 0 (head), 1 (boiler) and 2 (flow)."""

    HEAD = 0
    BOILER = 1
    FLOW = 2

    def __init__(self, seed=59, segments=None, loop=False):
        self.seed = int(seed)
        # A fraction of a pulse, stable for this seed. It does not use
        # ``random`` so the waveform cannot drift between interpreters.
        self.phase = (self.seed % 97) * 1.0e-4
        self.segments = list(segments) if segments is not None else demo_program()
        self.loop = loop
        self.duration = self.segments[-1].end if self.segments else 0.0

    def segment_at(self, t: float):
        if not self.segments:
            return None
        if self.loop and self.duration > 0:
            t = t % self.duration
        for segment in self.segments:
            if segment.start <= t < segment.end:
                return segment
        if t >= self.segments[-1].end:
            return self.segments[-1]
        return self.segments[0]

    def _boiler_bar(self, segment, t):
        if segment.boiler_from is None or segment.end <= segment.start:
            return segment.boiler_bar
        frac = (t - segment.start) / (segment.end - segment.start)
        frac = min(max(frac, 0.0), 1.0)
        return segment.boiler_from + (segment.boiler_bar - segment.boiler_from) * frac

    def voltage(self, channel: int, t: float) -> float:
        segment = self.segment_at(t)
        if segment is None:
            return 0.5 if channel != self.FLOW else 0.0
        if segment.fault and channel == self.HEAD:
            raise OSError("simulated sensor fault")
        local_t = t
        if self.loop and self.duration > 0:
            local_t = t % self.duration
        if channel == self.HEAD:
            return head_voltage(segment.head_bar)
        if channel == self.BOILER:
            return boiler_voltage(self._boiler_bar(segment, local_t))
        if channel == self.FLOW:
            return flow_square(t, segment.flow_mls, self.phase)
        raise ValueError(f"unknown channel {channel}")

    def climate(self, t: float):
        """Ambient temperature and humidity. Steady, nudged by the seed."""
        return 22.0 + (self.seed % 5) * 0.1, 45.0 + (self.seed % 3) * 0.2
