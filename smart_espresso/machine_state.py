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

    * water moving through the group (flow above ``brew_flow_mls``) means a
      shot is being pulled — a real pull always moves water past the inline
      flow meter;
    * head pressure in the brew band without flow is a shot only while the
      line is still charged from recent flow (the pump just stopped, or the
      puck blocked up mid-shot). At idle a heat-exchanger machine holds a
      *static* brew-band pressure (thermal expansion of the HX water, sensor
      zero offset), which is not a shot;
    * head pressure at pump level (``pump_head_bar``, which static pressure
      cannot reach) counts as a shot even without flow — a blocked puck —
      but holding it for ``max_flowless_brew_seconds`` with no water moving
      is a stuck or disconnected sensor, and the state falls back to what
      the boiler reports (or to UNKNOWN when no boiler is fitted, so the
      fault stays visible);
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
    # Beware: an idle heat-exchanger machine can sit just above this on
    # static pressure (HX thermal expansion, sensor zero offset) — brew-band
    # pressure alone is not a shot, see PRESSURE_HOLD_SECONDS.
    BREW_HEAD_BAR = 2.0
    # Head pressure only a running pump can produce against a blocked puck;
    # static/idle pressure never gets here. Qualifies as a shot even without
    # flow (the flow meter may see nothing through a fully clogged puck).
    PUMP_HEAD_BAR = 4.5
    BREW_FLOW_MLS = 0.3
    # How long brew-band head pressure counts as brewing after water stopped
    # moving: the line is still charged right after the pump stops or the
    # puck blocks up. Kept short — once the pressure is also below pump
    # level the pump is genuinely off, and on machines whose static pressure
    # sits in the brew band a long hold would pad every shot with a stale
    # "Pulling shot" tail.
    PRESSURE_HOLD_SECONDS = 5.0
    # Steam boiler idle band for a heat-exchanger machine.
    READY_MIN_BAR = 0.8
    READY_MAX_BAR = 1.6
    # Steam boiler pressure falling this fast (bar/second) means steam is
    # being drawn.
    STEAM_DROP_BAR_PER_S = 0.02
    # Pump-level head pressure held this long with no water moving (counted
    # from when the pump-level pressure first appears) is a stuck or
    # disconnected sensor, not a shot: no real pull does nothing for 5
    # minutes.
    MAX_FLOWLESS_BREW_SECONDS = 300.0
    # Recent pulls kept for the dashboard's history strip.
    SHOT_HISTORY_MAX = 12

    def __init__(
        self,
        off_bar: float = OFF_BAR,
        brew_head_bar: float = BREW_HEAD_BAR,
        pump_head_bar: float = PUMP_HEAD_BAR,
        brew_flow_mls: float = BREW_FLOW_MLS,
        ready_min_bar: float = READY_MIN_BAR,
        ready_max_bar: float = READY_MAX_BAR,
        steam_drop_bar_per_s: float = STEAM_DROP_BAR_PER_S,
        dwell_seconds: float = 0.8,
        pressure_hold_seconds: float = PRESSURE_HOLD_SECONDS,
        max_flowless_brew_seconds: float = MAX_FLOWLESS_BREW_SECONDS,
    ):
        """
        Initialize the classifier.

        Args:
            off_bar: Boiler pressure below which the machine counts as off.
            brew_head_bar: Head pressure at/above which a shot may be in
                progress (needs flow, or very recent flow, to count).
            pump_head_bar: Head pressure only a running pump produces; a
                shot even with no flow, until it proves to be a stuck sensor.
            brew_flow_mls: Flow rate (ml/s) at/above which a shot is in progress.
            ready_min_bar: Bottom of the boiler's ready band.
            ready_max_bar: Top of the boiler's ready band.
            steam_drop_bar_per_s: Boiler fall rate that indicates steam use.
            dwell_seconds: How long a candidate state must hold before it is
                published, to suppress flicker from noisy samples.
            pressure_hold_seconds: How long brew-band head pressure counts
                as brewing after water stopped moving.
            max_flowless_brew_seconds: How long pump-level head pressure
                held with no water moving (counted from when that pressure
                first appears) may read as brewing before it is treated as
                a sensor fault and the state falls back to the boiler's
                state, or to UNKNOWN when no boiler is fitted.
        """
        if pump_head_bar < brew_head_bar:
            raise ValueError(
                "pump_head_bar must be at or above brew_head_bar "
                f"(got brew={brew_head_bar}, pump={pump_head_bar})"
            )

        self.off_bar = off_bar
        self.brew_head_bar = brew_head_bar
        self.pump_head_bar = pump_head_bar
        self.brew_flow_mls = brew_flow_mls
        self.ready_min_bar = ready_min_bar
        self.ready_max_bar = ready_max_bar
        self.steam_drop_bar_per_s = steam_drop_bar_per_s
        self.dwell_seconds = dwell_seconds
        self.pressure_hold_seconds = pressure_hold_seconds
        self.max_flowless_brew_seconds = max_flowless_brew_seconds

        self._lock = threading.Lock()
        self.state: str = MachineState.UNKNOWN
        self._state_since: float = monotonic()
        self._candidate: Optional[str] = None
        self._candidate_since: float = 0.0

        self._last_boiler: Optional[float] = None
        self._last_boiler_time: Optional[float] = None
        self._boiler_rate: float = 0.0

        # Last time real water movement was seen. Starts infinitely in the
        # past: static pressure present at start-up must not read as brewing.
        self._last_flow_at = float("-inf")
        # When pump-level head pressure was first observed (a flowless-shot
        # candidate); reset once water moves again or the pressure drops
        # back below pump level.
        self._pump_pressure_since: Optional[float] = None

        self._shot_start: Optional[float] = None
        self._shot_seconds: float = 0.0
        self._shot_ml: float = 0.0
        # Reference sample for accumulating flowing time/volume into the
        # open shot. Advancing it on non-flowing ticks is what keeps pauses
        # out of the recorded duration.
        self._shot_prev: Optional[float] = None
        self._shot_prev_ml: float = 0.0
        self._last_shot: Optional[dict] = None
        # Newest last; a short memory of recent pulls so the dashboard can
        # show whether the machine (or the grinder) is drifting.
        self._shot_history: list[dict] = []

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
            candidate = self._classify(head_bar, boiler_bar, flow_mls, now)

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
            # pump stops, BREWING is still published while the line drains,
            # and counting through that window would inflate every recorded
            # shot. Flowing time is accumulated from sample to sample rather
            # than subtracted from a fixed start, so a pause with the line
            # still charged (flow resumes within the hold window) adds no
            # phantom seconds or volume either.
            if candidate == MachineState.BREWING and self._shot_start is not None:
                if flow_mls >= self.brew_flow_mls and self._shot_prev is not None:
                    self._shot_seconds += now - self._shot_prev
                    self._shot_ml = max(
                        self._shot_ml + (total_ml - self._shot_prev_ml), 0.0
                    )
                self._shot_prev = now
                self._shot_prev_ml = total_ml

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
        self,
        head_bar: Optional[float],
        boiler_bar: Optional[float],
        flow_mls: float,
        now: float,
    ) -> str:
        if head_bar is None and boiler_bar is None:
            return MachineState.UNKNOWN

        if flow_mls >= self.brew_flow_mls:
            # Real water movement is the primary shot signal.
            self._last_flow_at = now
            self._pump_pressure_since = None
            return MachineState.BREWING

        if head_bar is not None and head_bar >= self.brew_head_bar:
            if head_bar >= self.pump_head_bar:
                # Only a running pump builds this: a shot even without flow
                # (water blocked by the puck). The stuck-sensor clock starts
                # when pump-level pressure first appears — measured from the
                # last flow it would already be expired for a pull started
                # from idle — and a stuck or disconnected sensor would hold
                # this branch forever, so past the limit trust the boiler
                # instead (staying there until the pressure drops).
                if self._pump_pressure_since is None:
                    self._pump_pressure_since = now
                elif (
                    now - self._pump_pressure_since
                    >= self.max_flowless_brew_seconds
                ):
                    return self._thermal(boiler_bar, stuck=True)
                return MachineState.BREWING

            # Below pump level the stuck-sensor clock does not run.
            self._pump_pressure_since = None

            # Brew-band pressure just after water moved: the puck blocked up
            # or the pump stopped and the line is still charged.
            if now - self._last_flow_at <= self.pressure_hold_seconds:
                return MachineState.BREWING

            # Brew-band pressure with no recent flow is the machine idling on
            # static pressure (HX thermal expansion, sensor zero offset), not
            # a shot.
            return self._thermal(boiler_bar)

        self._pump_pressure_since = None
        return self._thermal(boiler_bar)

    def _thermal(self, boiler_bar: Optional[float], stuck: bool = False) -> str:
        """Classify from the boiler alone; the fall-back when no shot is on.

        With ``stuck`` set (a confirmed stuck head sensor) and no boiler
        reading either, there is nothing to fall back on and the fault is
        surfaced as UNKNOWN rather than a confident READY.
        """
        if boiler_bar is None:
            return MachineState.UNKNOWN if stuck else MachineState.READY

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
            self._shot_seconds = 0.0
            self._shot_ml = 0.0
            self._shot_prev = now
            self._shot_prev_ml = total_ml
        elif self.state == MachineState.BREWING:
            # Very short blips are pump priming or a noisy sample, not a shot,
            # and a pressure-only false positive (stuck sensor, static
            # pressure) moves no water at all. A real pull always fills the
            # shot volume, so require both before recording anything.
            if self._shot_seconds >= 2.0 and self._shot_ml > 0:
                self._last_shot = {
                    "seconds": round(self._shot_seconds, 1),
                    "ml": round(self._shot_ml, 1),
                }
                self._shot_history.append(self._last_shot)
                if len(self._shot_history) > self.SHOT_HISTORY_MAX:
                    del self._shot_history[: len(self._shot_history) - self.SHOT_HISTORY_MAX]
            self._shot_start = None
            self._shot_prev = None

        self.state = new_state
        self._state_since = now

    @property
    def label(self) -> str:
        with self._lock:
            return MachineState.LABELS[self.state]

    @property
    def severity(self) -> str:
        with self._lock:
            return MachineState.SEVERITY[self.state]

    @property
    def since_seconds(self) -> float:
        """Seconds the machine has been in the current state."""
        with self._lock:
            return monotonic() - self._state_since

    @property
    def boiler_rate(self) -> float:
        """Smoothed boiler pressure change in bar per second."""
        with self._lock:
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
                "history": [dict(entry) for entry in self._shot_history],
            }
