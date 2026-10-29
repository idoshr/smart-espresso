"""Classify what the espresso machine is doing from its live sensor readings."""
import threading
from time import localtime, monotonic
from typing import Any, Optional

from smart_espresso.shot_history import ShotHistory


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
    # When a flow meter is attached, "water is moving" is this many pulses
    # inside the window — not a ml/s rate, so a calibration constant cannot
    # turn one noise edge into a shot. The 1 s block average stays on the
    # chart and in Home Assistant.
    BREW_PULSE_COUNT = 2
    BREW_PULSE_WINDOW = 1.0
    # Gaps longer than this are pauses: they stay out of the recorded seconds.
    FLOW_GAP_SECONDS = 1.0
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
    # Recent pulls kept for the dashboard's duration strip. The persisted
    # log (shot_history.ShotHistory) is a separate, longer record.
    SHOT_HISTORY_MAX = 12
    # A saved shot has to have moved at least this much water. A backflush
    # and a short rinse fall under it.
    MIN_SHOT_ML = 5.0
    # Head pressure at or below this means the group has vented. A new pull
    # after that is a new shot, not the rest of the previous one.
    SHOT_VENT_BAR = 0.5
    # How long a pause may last, after brewing has already ended, and still
    # be the same shot, as long as the head never vented. Covers a bloom
    # that outlasts the pressure-hold window.
    SHOT_MERGE_SECONDS = 10.0

    def __init__(
        self,
        off_bar: float = OFF_BAR,
        brew_head_bar: float = BREW_HEAD_BAR,
        pump_head_bar: float = PUMP_HEAD_BAR,
        brew_flow_mls: float = BREW_FLOW_MLS,
        brew_pulse_count: int = BREW_PULSE_COUNT,
        brew_pulse_window: float = BREW_PULSE_WINDOW,
        flow_gap_seconds: float = FLOW_GAP_SECONDS,
        ready_min_bar: float = READY_MIN_BAR,
        ready_max_bar: float = READY_MAX_BAR,
        steam_drop_bar_per_s: float = STEAM_DROP_BAR_PER_S,
        dwell_seconds: float = 0.8,
        pressure_hold_seconds: float = PRESSURE_HOLD_SECONDS,
        max_flowless_brew_seconds: float = MAX_FLOWLESS_BREW_SECONDS,
        shot_history: Optional[ShotHistory] = None,
        min_shot_ml: float = MIN_SHOT_ML,
        shot_vent_bar: float = SHOT_VENT_BAR,
        shot_merge_seconds: float = SHOT_MERGE_SECONDS,
    ):
        """
        Initialize the classifier.

        Args:
            off_bar: Boiler pressure below which the machine counts as off.
            brew_head_bar: Head pressure at/above which a shot may be in
                progress (needs flow, or very recent flow, to count).
            pump_head_bar: Head pressure only a running pump produces; a
                shot even with no flow, until it proves to be a stuck sensor.
            brew_flow_mls: Flow rate (ml/s) at/above which a shot is in
                progress when no flow meter is passing pulse times.
            brew_pulse_count: With a flow meter, pulses inside
                ``brew_pulse_window`` that mean water is moving.
            brew_pulse_window: Seconds those pulses are counted over.
            flow_gap_seconds: A gap between pulses longer than this is a
                pause and is left out of the recorded shot time.
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
            shot_history: Optional log that receives each completed pull,
                including the pressure and flow curves sampled while it ran.
                None keeps the in-memory strip only. The log is updated
                outside this object's lock so a disk write cannot stall a
                reader of :meth:`snapshot`.
            min_shot_ml: Volume below which a pull is not saved. A flush can
                exceed this and still be dropped when the head never reached
                brew pressure.
            shot_vent_bar: Head pressure at or below which the group counts
                as vented, so the next flow starts a new shot.
            shot_merge_seconds: After brewing ends, how long flow may stay
                away and still continue the same shot, if the head has not
                vented.
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
        self.brew_pulse_count = brew_pulse_count
        self.brew_pulse_window = brew_pulse_window
        self.flow_gap_seconds = flow_gap_seconds
        self.ready_min_bar = ready_min_bar
        self.ready_max_bar = ready_max_bar
        self.steam_drop_bar_per_s = steam_drop_bar_per_s
        self.dwell_seconds = dwell_seconds
        self.pressure_hold_seconds = pressure_hold_seconds
        self.max_flowless_brew_seconds = max_flowless_brew_seconds
        self.shot_history = shot_history
        self.min_shot_ml = min_shot_ml
        self.shot_vent_bar = shot_vent_bar
        self.shot_merge_seconds = shot_merge_seconds

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
        self._shot_peak_head: Optional[float] = None
        # Set when brewing ends but the head is still charged, so a bloom
        # can continue this same pull instead of starting another.
        self._shot_suspended_at: Optional[float] = None
        self._shot_head_vented = False
        # open() runs after a commit of the pull that just closed, so it
        # cannot wipe the record it is about to save. The usual case opens
        # immediately, before the first curve sample.
        self._open_after = False
        self._deferred_sample: Optional[tuple] = None
        # Set when a flow meter is feeding this classifier. Shot seconds and
        # millilitres then come from pulse timestamps, not the 1 s average.
        self._flow_sensor: Any = None
        self._shot_pulse_from: Optional[float] = None
        self._last_shot: Optional[dict] = None
        # Newest last; a short memory of recent pulls so the dashboard can
        # show whether the machine (or the grinder) is drifting.
        self._shot_history: list[dict] = []

        # Daily tally behind the dashboard's "N pulls · X ml today" line.
        # Restored from the shot log so a restart does not zero a day that
        # already has pulls. Local midnight still starts a fresh tally.
        self._today_stamp = localtime()[:3]
        self._shots_today = 0
        self._ml_today = 0.0
        self._restore_today_from_history()

    def update(
        self,
        head_bar: Optional[float],
        boiler_bar: Optional[float],
        flow_mls: Optional[float] = 0.0,
        total_ml: Optional[float] = 0.0,
        flow_sensor=None,
    ) -> str:
        """
        Feed one sample and return the current published state.

        Args:
            head_bar: Brew head pressure in bar, or None if not fitted.
            boiler_bar: Boiler pressure in bar, or None if not fitted.
            flow_mls: Current flow rate in millilitres per second, or None
                when that read failed.
            total_ml: Lifetime volume in millilitres, used for the shot timer
                when no flow meter is attached, or None when that read failed.
            flow_sensor: Flow meter whose pulse timestamps time the shot.
                The 1 s ``flow_mls`` value is still sampled onto the curve.
        """
        now = monotonic()
        if self.shot_history is not None and self.shot_history.observe_clock():
            shots, ml = self.shot_history.totals_on_local_day(localtime()[:3])
            with self._lock:
                self._today_stamp = localtime()[:3]
                self._shots_today = shots
                self._ml_today = ml
        reported_flow = flow_mls
        if flow_sensor is not None:
            self._flow_sensor = flow_sensor
            flow_mls = self._gate_flow(now, flow_mls)
        # "commit" / "abandon" once a pull closes, applied after the lock so
        # the history file is not written while readers wait on it.
        outcome = None
        flow_known = flow_mls is not None and total_ml is not None
        with self._lock:
            self._open_after = False
            self._deferred_sample = None
            # A failed flow read must not look like a lifetime total of 0:
            # the next good sample would then count the whole shot again.
            if flow_mls is None:
                flow_mls = 0.0
            if total_ml is None:
                total_ml = self._shot_prev_ml
            self._roll_day_if_needed()
            self._update_boiler_rate(boiler_bar, now)
            self._note_shot_head(head_bar)
            if self._suspension_ended(now, flow_mls, flow_known):
                outcome = self._take_shot_outcome(now)
                self._clear_open_shot()
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
                change = self._on_state_change(
                    candidate,
                    now,
                    total_ml,
                    head_bar,
                    flow_mls,
                    flow_known,
                    defer_open=outcome is not None,
                )
                if change is not None:
                    outcome = change

            # Tracked against the candidate, not the published state: once the
            # pump stops, BREWING is still published while the line drains,
            # and counting through that window would inflate every recorded
            # shot. Flowing time is accumulated from sample to sample rather
            # than subtracted from a fixed start, so a pause with the line
            # still charged (flow resumes within the hold window) adds no
            # phantom seconds or volume either.
            if candidate == MachineState.BREWING and self._shot_start is not None:
                if (
                    flow_known
                    and flow_mls >= self.brew_flow_mls
                    and self._shot_prev is not None
                ):
                    self._shot_seconds += now - self._shot_prev
                    self._shot_ml = max(
                        self._shot_ml + (total_ml - self._shot_prev_ml), 0.0
                    )
                if flow_known:
                    self._shot_prev = now
                    self._shot_prev_ml = total_ml
                elif self._shot_prev is not None:
                    # Keep the volume baseline. Advance the clock so the
                    # outage is not later counted as flowing time.
                    self._shot_prev = now
                self._sample_open_shot(
                    now,
                    head_bar,
                    boiler_bar,
                    reported_flow if flow_known and reported_flow is not None else 0.0,
                )
            elif (
                self._shot_start is not None
                and flow_known
                and self._shot_prev is not None
            ):
                # Brewing has ended (or is about to) and the pull is still
                # open. Advance the baseline so the pause is not counted as
                # flowing time when water moves again.
                self._shot_prev = now
                self._shot_prev_ml = total_ml

            if self._shot_pulse_from is not None:
                self._shot_seconds, self._shot_ml = self._measure_pulses(now)

            state = self.state

        self._close_recorded_shot(outcome)
        if self._open_after and self.shot_history is not None:
            self.shot_history.open()
            if self._deferred_sample is not None:
                elapsed, head, boiler, flow = self._deferred_sample
                self.shot_history.sample(elapsed, head, boiler, flow)
        return state

    def _restore_today_from_history(self) -> None:
        """Set the daily tally from shots stamped on the current local day."""
        if self.shot_history is None:
            return
        shots, ml = self.shot_history.totals_on_local_day(self._today_stamp)
        self._shots_today = shots
        self._ml_today = ml

    def close_open_pull(self) -> None:
        """Commit a qualifying open pull, or abandon one that is not a shot.

        Called on shutdown so a pull in progress is not dropped when the
        process exits.
        """
        now = monotonic()
        with self._lock:
            if self._shot_start is None:
                outcome = None
            else:
                outcome = self._take_shot_outcome(now)
                self._clear_open_shot()
        self._close_recorded_shot(outcome)

    def _roll_day_if_needed(self) -> None:
        """Reset the daily tally once the wall clock crosses local midnight."""
        today = localtime()[:3]
        if today != self._today_stamp:
            self._today_stamp = today
            self._shots_today = 0
            self._ml_today = 0.0

    def _update_boiler_rate(self, boiler_bar: Optional[float], now: float) -> None:
        """Track the boiler's rate of change (bar/s) with light smoothing."""
        if boiler_bar is None:
            return
        last_boiler = self._last_boiler
        last_time = self._last_boiler_time
        if last_time is not None and last_boiler is not None:
            elapsed = now - last_time
            if elapsed >= 0.25:  # ignore sub-quarter-second gaps: too noisy
                rate = (boiler_bar - last_boiler) / elapsed
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
                elif now - self._pump_pressure_since >= self.max_flowless_brew_seconds:
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

    def _note_shot_head(self, head_bar: Optional[float]) -> None:
        """Track the peak, and whether the group has vented, for the open pull."""
        if self._shot_start is None:
            return
        if head_bar is None or head_bar <= self.shot_vent_bar:
            self._shot_head_vented = True
            return
        if self._shot_peak_head is None or head_bar > self._shot_peak_head:
            self._shot_peak_head = head_bar

    def _resume_continues(self, now: float, flow_mls: float, flow_known: bool) -> bool:
        """True when this sample is the rest of a pull that has not vented."""
        if self._shot_suspended_at is None or self._shot_start is None:
            return False
        if self._shot_head_vented:
            return False
        if now - self._shot_suspended_at > self.shot_merge_seconds:
            return False
        return bool(flow_known and flow_mls >= self.brew_flow_mls)

    def _suspension_ended(self, now: float, flow_mls: float, flow_known: bool) -> bool:
        """True when a paused pull should be saved or dropped on this sample."""
        if self._shot_suspended_at is None:
            return False
        if self._resume_continues(now, flow_mls, flow_known):
            return False
        if self._shot_head_vented:
            return True
        return now - self._shot_suspended_at > self.shot_merge_seconds

    def _can_defer_close(self, head_bar: Optional[float]) -> bool:
        """Keep the pull open across a pause that has not vented the head."""
        return (
            self._shot_start is not None
            and not self._shot_head_vented
            and head_bar is not None
            and head_bar > self.shot_vent_bar
        )

    def _begin_shot(
        self, now: float, total_ml: float, head_bar: Optional[float]
    ) -> None:
        self._shot_start = now
        self._shot_seconds = 0.0
        self._shot_ml = 0.0
        self._shot_prev = now
        self._shot_prev_ml = total_ml
        self._shot_peak_head = head_bar
        self._shot_head_vented = head_bar is None or (head_bar <= self.shot_vent_bar)
        self._shot_suspended_at = None
        self._shot_pulse_from = None
        if self._flow_sensor is not None:
            # The pulses that opened the gate are already behind this sample.
            self._shot_pulse_from = now - self.brew_pulse_window

    def _clear_open_shot(self) -> None:
        self._shot_start = None
        self._shot_prev = None
        self._shot_seconds = 0.0
        self._shot_ml = 0.0
        self._shot_peak_head = None
        self._shot_head_vented = False
        self._shot_suspended_at = None
        self._shot_pulse_from = None

    def _gate_flow(self, now: float, flow_mls: Optional[float]) -> Optional[float]:
        """Turn the pulse window into the boolean the state machine already uses.

        Enough pulses count as flow even while the 1 s average is still
        zero. Once the pulses stop, the average is ignored so it cannot
        keep the timer running.
        """
        sensor = self._flow_sensor
        if sensor is None:
            return flow_mls
        flowing = (
            sensor.pulses_within(now, self.brew_pulse_window) >= self.brew_pulse_count
        )
        if not flowing:
            return 0.0 if flow_mls is not None else None
        if flow_mls is None:
            return self.brew_flow_mls
        return max(flow_mls, self.brew_flow_mls)

    def _measure_pulses(self, now: float) -> tuple[float, float]:
        sensor = self._flow_sensor
        start = self._shot_pulse_from
        if sensor is None or start is None:
            return self._shot_seconds, self._shot_ml
        count = sensor.pulses - sensor.count_before(start)
        if count < 0:
            count = 0
        ml = count * 1000.0 / sensor.pulses_per_liter
        seconds = sensor.flowing_seconds(start, now, self.flow_gap_seconds)
        return seconds, ml

    def _take_shot_outcome(self, now: float) -> tuple:
        """Decide whether the open pull is a shot. Does not clear it."""
        if self._shot_pulse_from is not None:
            self._shot_seconds, self._shot_ml = self._measure_pulses(now)
        peak = self._shot_peak_head
        qualifies = (
            self._shot_seconds >= 2.0
            and self._shot_ml >= self.min_shot_ml
            and peak is not None
            and peak >= self.brew_head_bar
        )
        if not qualifies:
            return ("abandon",)
        self._last_shot = {
            "seconds": round(self._shot_seconds, 1),
            "ml": round(self._shot_ml, 1),
        }
        self._shot_history.append(self._last_shot)
        if len(self._shot_history) > self.SHOT_HISTORY_MAX:
            del self._shot_history[: len(self._shot_history) - self.SHOT_HISTORY_MAX]
        self._shots_today += 1
        self._ml_today += self._shot_ml
        return ("commit", self._last_shot["seconds"], self._last_shot["ml"])

    def _on_state_change(
        self,
        new_state: str,
        now: float,
        total_ml: float,
        head_bar: Optional[float],
        flow_mls: float,
        flow_known: bool,
        defer_open: bool = False,
    ) -> Optional[tuple]:
        """Enter a new state, opening or closing the shot timer as needed.

        Returns ``("commit", seconds, ml)`` or ``("abandon",)`` when a pull
        closes, so the caller can persist it after releasing the lock.
        A pause that leaves the head charged does not close yet: flow
        returning inside the merge window continues the same pull.
        """
        outcome = None
        if new_state == MachineState.BREWING:
            if self._resume_continues(now, flow_mls, flow_known):
                self._shot_suspended_at = None
            else:
                self._begin_shot(now, total_ml, head_bar)
                if defer_open:
                    # The previous pull is committed after this lock. Opening
                    # now would make that commit save the new pull instead.
                    self._open_after = self.shot_history is not None
                elif self.shot_history is not None:
                    self.shot_history.open()
        elif self.state == MachineState.BREWING:
            if self._can_defer_close(head_bar):
                self._shot_suspended_at = now
            else:
                outcome = self._take_shot_outcome(now)
                self._clear_open_shot()

        self.state = new_state
        self._state_since = now
        return outcome

    def _sample_open_shot(
        self,
        now: float,
        head_bar: Optional[float],
        boiler_bar: Optional[float],
        flow_mls: float,
    ) -> None:
        """Hand the current reading to the log, if a pull is open.

        Elapsed time is wall time from the start of the pull, pauses
        included, so the curve shows a real pressure profile. The recorded
        duration stays the flowing time only.
        """
        if self.shot_history is None or self._shot_start is None:
            return
        elapsed = now - self._shot_start
        if self._open_after:
            self._deferred_sample = (elapsed, head_bar, boiler_bar, flow_mls)
            return
        self.shot_history.sample(elapsed, head_bar, boiler_bar, flow_mls)

    def _close_recorded_shot(self, outcome: Optional[tuple]) -> None:
        """Persist or drop the pull that just closed. No-op otherwise."""
        if self.shot_history is None or not outcome:
            return
        if outcome[0] == "commit":
            # The log may be a couple of megabytes. Writing it here would
            # freeze the shot timer on the committing sample.
            self.shot_history.commit(outcome[1], outcome[2], wait=False)
        else:
            self.shot_history.abandon()

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
            # The tally rolls in update() while samples flow, but a stalled
            # render loop (or a quiet machine) must not serve yesterday's
            # count after midnight — roll on read as well.
            self._roll_day_if_needed()
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
                "today": {
                    "shots": self._shots_today,
                    "ml": round(self._ml_today, 1),
                },
            }
