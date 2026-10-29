from collections import deque
from threading import Event, Lock, Thread
from time import monotonic
from typing import Optional

from smart_espresso.analog_sensor.analog_sensor import (
    ADCInterface,
    AnalogSensor,
    MedianFilterADC,
)


def _reject_median_filter(adc) -> None:
    """Refuse a median filter on the pulse channel.

    A window of 3 drops any high or low phase that lasts only one sample,
    which is what a flow pulse looks like near the sampling limit. The
    Schmitt thresholds already reject noise between the two levels.
    """
    current = adc
    seen = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, MedianFilterADC):
            raise ValueError(
                "WaterFlowAnalogSensor cannot count pulses through a "
                "MedianFilterADC: a median window drops pulses that are "
                "high or low for a single sample, so shot volume, live flow "
                "and the tank counter under-read. Leave this channel "
                "unfiltered."
            )
        seen.add(id(current))
        current = getattr(current, "adc", None)


class WaterFlowAnalogSensor(AnalogSensor):
    """
    Hall-effect water flow / shot-volume meter read through an analog ADC channel.

    A hall-effect flow sensor outputs a square-wave pulse train whose frequency
    is proportional to the flow rate; each pulse represents a fixed volume of
    water. When the pulse output is wired to an ADC channel (e.g. the ADS1115
    A2 input) instead of a GPIO edge-interrupt pin, we recover the pulses by
    sampling the channel voltage and counting transitions.

    Edge detection uses a two-level (Schmitt-trigger style) hysteresis so a
    single, possibly noisy, pulse is counted exactly once:
      * a pulse is counted when the voltage rises above ``high_threshold``
      * the detector re-arms only once the voltage falls below ``low_threshold``

    Two sensor wirings exist in the wild and both are supported. The first
    sample latches the idle level (without counting it) and decides the
    polarity:
      * idle-low sensors pulse high (rising edges are counted)
      * idle-high sensors (e.g. open-collector outputs held up by a pull-up)
        pulse low (falling edges are counted)

    Accuracy is bounded by the sampling rate: to avoid missing pulses the
    sampling rate must comfortably exceed the sensor's maximum pulse
    frequency. The render loop samples at roughly 10 Hz, which is sufficient
    for slow flows only. Pass ``sample_interval`` to move sampling onto a
    background thread. The default period is 5 ms (200 Hz), which keeps a
    square wave from 0.5 to 6 ml/s inside ±1% at duty cycles of 30–70%.
    A median filter must not sit on this channel: it deletes pulses that
    are only one sample wide. For flows above that, a GPIO edge-interrupt
    wiring is still more accurate.
    """

    # Pulses emitted per litre. Typical mini DC3-24V coffee-machine flow
    # sensors are around this figure; calibrate for your specific unit.
    DEFAULT_PULSES_PER_LITER = 5880

    # Voltage thresholds for pulse edge detection. Defaults suit 3.3V or 5V
    # logic-level pulse outputs: a pulse peak comfortably clears 2.5V and its
    # idle level sits below 1.0V.
    DEFAULT_HIGH_THRESHOLD = 2.5
    DEFAULT_LOW_THRESHOLD = 1.0

    # 200 Hz. A 6 ml/s pour at 30% or 70% duty has an 8.5 ms short phase;
    # a 10 ms period (the old 100 Hz default) misses that phase and the
    # shot volume under-reads. 5 ms keeps the short phase on the grid.
    # The sampler folds ADC conversion time into this period, and the
    # measured rate is what ``sample_hz`` reports.
    DEFAULT_SAMPLE_INTERVAL = 0.005

    # How long a run of samples is averaged before ``sample_hz`` updates.
    SAMPLE_RATE_WINDOW_SECONDS = 1.0
    # pulse_hz > sample_hz / 2.5 means a non-50% duty cycle can hide a
    # phase between samples. Nyquist (sample_hz / 2) is not enough margin.
    ALIAS_RATE_FACTOR = 2.5
    ALIAS_WARN_INTERVAL = 30.0
    RATE_LOG_INTERVAL = 60.0
    # Pulse timestamps kept for the shot timer. The 1 s block average above
    # stays what the chart and Home Assistant show; the shot uses the edges.
    PULSE_HISTORY_SECONDS = 600.0

    def __init__(
        self,
        adc: ADCInterface,
        name: str,
        pulses_per_liter: float = DEFAULT_PULSES_PER_LITER,
        high_threshold: float = DEFAULT_HIGH_THRESHOLD,
        low_threshold: float = DEFAULT_LOW_THRESHOLD,
        flow_interval: float = 1.0,
        sample_interval: Optional[float] = None,
    ):
        """
        Initialize the water flow sensor.

        Args:
            adc: An ADC instance implementing ADCInterface (ADS1115ADC, MCP3008ADC).
            name: Sensor name for display and Home Assistant (e.g. "Brew").
            pulses_per_liter: Calibration constant for the sensor.
            high_threshold: Voltage at/above which a pulse edge is counted.
            low_threshold: Voltage at/below which the detector re-arms.
            flow_interval: Window in seconds over which the flow rate (L/min)
                is recomputed. Kept above the sampling period so the rate is
                averaged over several samples rather than jittering per tick.
            sample_interval: Seconds between ADC samples on a background
                thread. None (default) samples on the render loop instead,
                which caps the pulse frequency the sensor can capture.
        """
        if low_threshold >= high_threshold:
            raise ValueError(
                "low_threshold must be below high_threshold "
                f"(got low={low_threshold}, high={high_threshold})"
            )
        _reject_median_filter(adc)

        super().__init__(adc, name)
        self.pulses_per_liter = pulses_per_liter
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.flow_interval = flow_interval
        self.sample_interval = sample_interval

        self._pulses = 0
        self._pulse_times: deque[float] = deque()
        self._pulse_lock = Lock()
        self._above = False  # current hysteresis state (True once above high)
        self._synced = False  # idle level latched yet?
        self._inverted = False  # True when the sensor idles high
        self._flow_rate_lpm = 0.0
        self._flow_window_start = monotonic()
        self._flow_window_pulses = 0

        self._sample_hz: Optional[float] = None
        self._hz_window_start: Optional[float] = None
        self._hz_window_samples = 0
        # None, not 0: a test clock (and a monotonic clock) both start near
        # zero, and "last warned at 0" would swallow the first half minute.
        self._last_alias_warn: Optional[float] = None
        self._last_rate_log: Optional[float] = None

        self._stop_event = Event()
        self._thread = None
        if sample_interval is not None:
            # Warm the cache so the first read() returns a float; a
            # disconnected harness must not break construction — the
            # sampler thread keeps retrying and reports failures itself.
            try:
                self._sample()
            except Exception:
                pass
            self._thread = Thread(target=self._sample_loop, daemon=True)
            self._thread.start()

    def _count_edge(self, voltage: float, now: float) -> None:
        """Update the pulse counter from one voltage sample."""
        if not self._synced:
            # Latch the idle level without counting it, otherwise an idle-high
            # sensor registers a phantom pulse on the very first sample.
            self._synced = True
            self._above = voltage >= self.high_threshold
            self._inverted = self._above
            return

        counted = False
        if self._inverted:
            # Idle-high sensor: each pulse is one drop below the low threshold.
            if self._above and voltage <= self.low_threshold:
                self._above = False
                counted = True
            elif not self._above and voltage >= self.high_threshold:
                self._above = True
        else:
            # Idle-low sensor: each pulse is one rise above the high threshold.
            if not self._above and voltage >= self.high_threshold:
                self._above = True
                counted = True
            elif self._above and voltage <= self.low_threshold:
                self._above = False
        if counted:
            self._note_pulse(now)

    def _note_pulse(self, now: float) -> None:
        cutoff = now - self.PULSE_HISTORY_SECONDS
        with self._pulse_lock:
            self._pulses += 1
            self._pulse_times.append(now)
            while self._pulse_times and self._pulse_times[0] < cutoff:
                self._pulse_times.popleft()

    def pulses_within(self, now: float, window: float) -> int:
        """How many pulses landed in ``(now - window, now]``."""
        cutoff = now - window
        with self._pulse_lock:
            count = 0
            for ts in reversed(self._pulse_times):
                if ts <= cutoff:
                    break
                if ts <= now:
                    count += 1
            return count

    def count_before(self, instant: float) -> int:
        """Lifetime pulses strictly earlier than ``instant``."""
        with self._pulse_lock:
            after = 0
            for ts in self._pulse_times:
                if ts >= instant:
                    after += 1
            return self._pulses - after

    def flowing_seconds(self, start: float, end: float, gap: float) -> float:
        """Time from the first pulse to the last in ``[start, end]``.

        A gap longer than ``gap`` is a pause and is left out. The 1 s rate
        window is not involved.
        """
        with self._pulse_lock:
            times = [ts for ts in self._pulse_times if start <= ts <= end]
        if len(times) < 2:
            return 0.0
        total = 0.0
        previous = times[0]
        for ts in times[1:]:
            step = ts - previous
            if step <= gap:
                total += step
            previous = ts
        return total

    def _update_rate(self, now: float) -> None:
        """Recompute the flow rate over a fixed window to keep it stable.

        The pulse total and the window baseline share ``_pulse_lock`` with
        ``reset``. Reading one and then the other without the lock lets a
        reset zero the total while the baseline still holds the old count,
        and the rate comes out negative.
        """
        with self._pulse_lock:
            elapsed = now - self._flow_window_start
            if elapsed < self.flow_interval or elapsed <= 0:
                return
            pulses = self._pulses
            pulses_in_window = pulses - self._flow_window_pulses
            if pulses_in_window < 0:
                pulses_in_window = 0
            liters_in_window = pulses_in_window / self.pulses_per_liter
            self._flow_rate_lpm = (liters_in_window / elapsed) * 60.0
            self._flow_window_start = now
            self._flow_window_pulses = pulses

    def _sample(self) -> None:
        """Take one ADC sample and update counters and cached value."""
        self._value = self.adc.read()
        now = monotonic()
        self._count_edge(self.adc.voltage, now)
        self._update_rate(now)
        self._observe_sample_clock(now)

    def _observe_sample_clock(self, now: float) -> None:
        """Update the measured sample rate from the time between samples."""
        if self._hz_window_start is None:
            self._hz_window_start = now
            self._hz_window_samples = 0
            return
        self._hz_window_samples += 1
        elapsed = now - self._hz_window_start
        if elapsed < self.SAMPLE_RATE_WINDOW_SECONDS:
            return
        self._sample_hz = self._hz_window_samples / elapsed
        self._hz_window_start = now
        self._hz_window_samples = 0
        self._log_sample_rate(now)
        self._warn_if_aliasing(now)

    def _log_sample_rate(self, now: float) -> None:
        if (
            self._last_rate_log is not None
            and now - self._last_rate_log < self.RATE_LOG_INTERVAL
        ):
            return
        self._last_rate_log = now
        print(f"{self.name} flow sample rate {self._sample_hz:.1f} Hz")

    def _warn_if_aliasing(self, now: float) -> None:
        """Warn when the pulse rate is too fast for the measured sample rate."""
        if not self._sample_hz:
            return
        pulse_hz = (self._flow_rate_lpm / 60.0) * self.pulses_per_liter
        limit = self._sample_hz / self.ALIAS_RATE_FACTOR
        if pulse_hz <= limit:
            return
        if (
            self._last_alias_warn is not None
            and now - self._last_alias_warn < self.ALIAS_WARN_INTERVAL
        ):
            return
        self._last_alias_warn = now
        print(
            f"{self.name} flow pulse rate {pulse_hz:.1f} Hz exceeds "
            f"{limit:.1f} Hz (sample rate {self._sample_hz:.1f} Hz / "
            f"{self.ALIAS_RATE_FACTOR}); pulses are being missed"
        )

    def _sample_loop(self) -> None:
        """Sample the ADC on a background thread, faster than the render loop.

        The wait is the remainder of ``sample_interval`` after the conversion,
        so I2C time does not stretch the period. If a conversion overruns the
        period the loop runs as fast as the ADC allows and ``sample_hz``
        shows that slower rate.
        """
        last_error_log = 0.0
        while not self._stop_event.is_set():
            started = monotonic()
            try:
                self._sample()
            except Exception as error:
                # Sample errors must not kill the thread; log them, throttled,
                # so a broken bus stays visible without flooding the console.
                now = monotonic()
                if now - last_error_log >= 30.0:
                    last_error_log = now
                    print(f"{self.name} flow sensor sampling failed: {error}")
            interval = self.sample_interval
            if interval is None:
                return
            remaining = interval - (monotonic() - started)
            if remaining > 0 and self._stop_event.wait(remaining):
                return

    def read(self):
        """
        Sample the ADC and update the flow rate, or return the cached value.

        Returns the ADC's normalized value (0.0-1.0) for consistency with the
        base class, though callers typically use ``liter`` / ``flow_rate``.
        With ``sample_interval`` set, sampling happens on a background thread
        and this method only returns the most recent cached reading.
        """
        if self._thread is None:
            self._sample()
        return self._value

    def stop(self) -> None:
        """Stop the background sampling thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def reset(self):
        """Zero the accumulated volume (e.g. at the start of a new shot).

        The latched idle level is kept: re-syncing it would let an idle-high
        sensor count its own idle level as a pulse.
        """
        with self._pulse_lock:
            self._pulses = 0
            self._pulse_times.clear()
            self._flow_rate_lpm = 0.0
            self._flow_window_start = monotonic()
            self._flow_window_pulses = 0

    @property
    def pulses(self) -> int:
        """Total pulses counted since the last reset."""
        with self._pulse_lock:
            return self._pulses

    @property
    def sample_hz(self) -> Optional[float]:
        """Measured ADC sample rate, or None until one window has elapsed."""
        return self._sample_hz

    @property
    def liter(self) -> float:
        """Total volume of water measured, in litres."""
        return self.pulses / self.pulses_per_liter

    @property
    def flow_rate(self) -> float:
        """Current flow rate in litres per minute (averaged over flow_interval)."""
        return self._flow_rate_lpm

    @property
    def message_liter(self):
        return f"{self.name}: {round(self.liter, 4)} L"

    @staticmethod
    def unit_of_measurement():
        return "L"

    @property
    def message(self):
        return self.message_liter

    @property
    def normalized_value(self):
        return self.liter

    def ha_states(self):
        from smart_espresso.home_assistant import State

        return [
            State(
                entity_id=f"sensor.espresso_machine_{self.name.lower()}_flow",
                state=str(round(self.liter, 2)),
                attributes={
                    "unit_of_measurement": self.unit_of_measurement(),
                    "friendly_name": f"{self.name} Flow",
                    "device_class": "volume",
                    "flow_rate": round(self.flow_rate, 3),
                    "flow_rate_unit": "L/min",
                },
            )
        ]

    def update_home_assistant(self, client):
        for state in self.ha_states():
            client.set_state(state)
