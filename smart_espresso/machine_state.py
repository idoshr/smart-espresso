"""Classify what the espresso machine is doing from its live sensor readings."""
import threading
from time import monotonic
from typing import Optional


class MachineState:
    """Machine state identifiers, with the label shown in the UI."""

    OFF = "off"
    HEATING = "heating"
    READY = "ready"
    BREWING = "brewing"
    STEAMING = "steaming"
    OVERPRESSURE = "overpressure"
    UNKNOWN = "unknown"

    # Written the way a barista would call it out, not the way the sensor
    # reads it.
    LABELS = {
        OFF: "Resting",
        HEATING: "Warming up",
        READY: "Ready to pull",
        BREWING: "Pulling shot",
        STEAMING: "Steaming milk",
        OVERPRESSURE: "Pressure high",
        UNKNOWN: "No reading",
    }

    # Rendered as a colour/severity band in the UI.
    SEVERITY = {
        OFF: "idle",
        HEATING: "warn",
        READY: "good",
        BREWING: "active",
        STEAMING: "active",
        OVERPRESSURE: "alert",
        UNKNOWN: "idle",
    }


class MachineStateClassifier:
    """
    Derive a single machine state from head pressure, boiler pressure and flow.

    The rules, in priority order:

    * water moving through the group (flow above ``brew_flow_mls``) or the head
      under pressure (above ``brew_head_bar``) means a shot is being pulled;
    * a boiler that is not just low but *dropping fast* means steam is being
      drawn, which is what separates steaming from a cold-start heat-up;
    * boiler below ``ready_min_bar`` is heating, above ``ready_max_bar`` is an
      over-pressure warning, and in between is ready to brew.

    A state must persist for ``dwell_seconds`` before it is published, so a
    single noisy ADC sample cannot make the dashboard flicker between states.

    The render loop updates this from its own thread while Flask worker threads
    read :meth:`snapshot`, so both are guarded by a lock: a reader must never
    catch a half-applied state change, such as a new state carrying the
    previous state's start time.
    """

    # Below this the boiler is not being held at all: the machine is off.
    OFF_BAR = 0.15
    # Typical E61 shot: the pump reaches well past 2 bar at the group.
    BREW_HEAD_BAR = 2.0
    BREW_FLOW_MLS = 0.3
    # Steam boiler idle band for a heat-exchanger machine.
    READY_MIN_BAR = 0.8
    READY_MAX_BAR = 1.6
    # Boiler pressure falling this fast (bar/second) means steam is being used.
    STEAM_DROP_BAR_PER_S = 0.02

    def __init__(
        self,
        off_bar: float = OFF_BAR,
        brew_head_bar: float = BREW_HEAD_BAR,
        brew_flow_mls: float = BREW_FLOW_MLS,
        ready_min_bar: float = READY_MIN_BAR,
        ready_max_bar: float = READY_MAX_BAR,
        steam_drop_bar_per_s: float = STEAM_DROP_BAR_PER_S,
        dwell_seconds: float = 0.8,
    ):
        """
        Initialize the classifier.

        Args:
            off_bar: Boiler pressure below which the machine counts as off.
            brew_head_bar: Head pressure at/above which a shot is in progress.
            brew_flow_mls: Flow rate (ml/s) at/above which a shot is in progress.
            ready_min_bar: Bottom of the boiler's ready band.
            ready_max_bar: Top of the boiler's ready band.
            steam_drop_bar_per_s: Boiler fall rate that indicates steam use.
            dwell_seconds: How long a candidate state must hold before it is
                published, to suppress flicker from noisy samples.
        """
        self.off_bar = off_bar
        self.brew_head_bar = brew_head_bar
        self.brew_flow_mls = brew_flow_mls
        self.ready_min_bar = ready_min_bar
        self.ready_max_bar = ready_max_bar
        self.steam_drop_bar_per_s = steam_drop_bar_per_s
        self.dwell_seconds = dwell_seconds

        self._lock = threading.Lock()
        self.state: str = MachineState.UNKNOWN
        self._state_since: float = monotonic()
        self._candidate: Optional[str] = None
        self._candidate_since: float = 0.0

        self._last_boiler: Optional[float] = None
        self._last_boiler_time: Optional[float] = None
        self._boiler_rate: float = 0.0

        self._shot_start: Optional[float] = None
        self._shot_start_ml: float = 0.0
        self._shot_seconds: float = 0.0
        self._shot_ml: float = 0.0
        self._last_shot: Optional[dict] = None

    def update(
        self,
        head_bar: Optional[float],
        boiler_bar: Optional[float],
        flow_mls: float = 0.0,
        total_ml: float = 0.0,
    ) -> str:
        """
        Feed one sample and return the current published state.

        Args:
            head_bar: Brew head pressure in bar, or None if not fitted.
            boiler_bar: Boiler pressure in bar, or None if not fitted.
            flow_mls: Current flow rate in millilitres per second.
            total_ml: Lifetime volume in millilitres, used for the shot timer.
        """
        now = monotonic()
        with self._lock:
            self._update_boiler_rate(boiler_bar, now)
            candidate = self._classify(head_bar, boiler_bar, flow_mls)

            if candidate != self._candidate:
                self._candidate = candidate
                self._candidate_since = now

            # Publish a new state only once the candidate has held long enough.
            # Brewing is published immediately: a shot timer that starts a
            # second late is worse than an occasional false start.
            promote = candidate == MachineState.BREWING or (
                now - self._candidate_since >= self.dwell_seconds
            )
            if promote and candidate != self.state:
                self._on_state_change(candidate, now, total_ml)

            # Tracked against the candidate, not the published state: once the
            # pump stops, BREWING is still published for dwell_seconds, and
            # counting through that window would inflate every recorded shot.
            if candidate == MachineState.BREWING and self._shot_start is not None:
                self._shot_seconds = now - self._shot_start
                self._shot_ml = max(total_ml - self._shot_start_ml, 0.0)

            return self.state

    def _update_boiler_rate(self, boiler_bar: Optional[float], now: float) -> None:
        """Track the boiler's rate of change (bar/s) with light smoothing."""
        if boiler_bar is None:
            return
        if self._last_boiler_time is not None:
            elapsed = now - self._last_boiler_time
            if elapsed >= 0.25:  # ignore sub-quarter-second gaps: too noisy
                rate = (boiler_bar - self._last_boiler) / elapsed
                self._boiler_rate += (rate - self._boiler_rate) * 0.4
                self._last_boiler = boiler_bar
                self._last_boiler_time = now
        else:
            self._last_boiler = boiler_bar
            self._last_boiler_time = now

    def _classify(
        self, head_bar: Optional[float], boiler_bar: Optional[float], flow_mls: float
    ) -> str:
        if head_bar is None and boiler_bar is None:
            return MachineState.UNKNOWN

        if flow_mls >= self.brew_flow_mls or (
            head_bar is not None and head_bar >= self.brew_head_bar
        ):
            return MachineState.BREWING

        if boiler_bar is None:
            return MachineState.READY

        if boiler_bar < self.off_bar:
            return MachineState.OFF

        if self._boiler_rate <= -self.steam_drop_bar_per_s:
            return MachineState.STEAMING

        if boiler_bar < self.ready_min_bar:
            return MachineState.HEATING

        if boiler_bar > self.ready_max_bar:
            return MachineState.OVERPRESSURE

        return MachineState.READY

    def _on_state_change(self, new_state: str, now: float, total_ml: float) -> None:
        """Enter a new state, opening or closing the shot timer as needed."""
        if new_state == MachineState.BREWING:
            self._shot_start = now
            self._shot_start_ml = total_ml
            self._shot_seconds = 0.0
            self._shot_ml = 0.0
        elif self.state == MachineState.BREWING:
            # Very short blips are pump priming or a noisy sample, not a shot.
            if self._shot_seconds >= 2.0:
                self._last_shot = {
                    "seconds": round(self._shot_seconds, 1),
                    "ml": round(self._shot_ml, 1),
                }
            self._shot_start = None

        self.state = new_state
        self._state_since = now

    @property
    def label(self) -> str:
        return MachineState.LABELS[self.state]

    @property
    def severity(self) -> str:
        return MachineState.SEVERITY[self.state]

    @property
    def since_seconds(self) -> float:
        """Seconds the machine has been in the current state."""
        return monotonic() - self._state_since

    @property
    def boiler_rate(self) -> float:
        """Smoothed boiler pressure change in bar per second."""
        return self._boiler_rate

    def snapshot(self) -> dict:
        """
        Serialisable view of the current state for the web API.

        Built under the lock and from a single read of each field, so the
        state, its age and the shot always describe the same moment.
        """
        with self._lock:
            state = self.state
            shot = None
            if state == MachineState.BREWING and self._shot_start is not None:
                shot = {
                    "seconds": round(self._shot_seconds, 1),
                    "ml": round(self._shot_ml, 1),
                }
            return {
                "state": state,
                "label": MachineState.LABELS[state],
                "severity": MachineState.SEVERITY[state],
                "since_seconds": round(monotonic() - self._state_since, 1),
                "boiler_rate": round(self._boiler_rate, 3),
                "shot": shot,
                "last_shot": self._last_shot,
            }
