from threading import Event, Thread
from time import monotonic
from typing import Optional

from homeassistant_api import Client, State

from smart_espresso.analog_sensor.analog_sensor import ADCInterface, AnalogSensor


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
    background thread that runs several times faster; espresso-scale flow
    rates (~30 pulses/s) need ~100 Hz to be captured reliably. For high-flow
    applications a GPIO edge-interrupt wiring is still more accurate.
    """

    # Pulses emitted per litre. Typical mini DC3-24V coffee-machine flow
    # sensors are around this figure; calibrate for your specific unit.
    DEFAULT_PULSES_PER_LITER = 5880

    # Voltage thresholds for pulse edge detection. Defaults suit 3.3V or 5V
    # logic-level pulse outputs: a pulse peak comfortably clears 2.5V and its
    # idle level sits below 1.0V.
    DEFAULT_HIGH_THRESHOLD = 2.5
    DEFAULT_LOW_THRESHOLD = 1.0

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

        super().__init__(adc, name)
        self.pulses_per_liter = pulses_per_liter
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.flow_interval = flow_interval
        self.sample_interval = sample_interval

        self._pulses = 0
        self._above = False  # current hysteresis state (True once above high)
        self._synced = False  # idle level latched yet?
        self._inverted = False  # True when the sensor idles high
        self._flow_rate_lpm = 0.0
        self._flow_window_start = monotonic()
        self._flow_window_pulses = 0

        self._stop_event = Event()
        self._thread = None
        if sample_interval is not None:
            self._thread = Thread(target=self._sample_loop, daemon=True)
            self._thread.start()

    def _count_edge(self, voltage: float) -> None:
        """Update the pulse counter from one voltage sample."""
        if not self._synced:
            # Latch the idle level without counting it, otherwise an idle-high
            # sensor registers a phantom pulse on the very first sample.
            self._synced = True
            self._above = voltage >= self.high_threshold
            self._inverted = self._above
            return

        if self._inverted:
            # Idle-high sensor: each pulse is one drop below the low threshold.
            if self._above and voltage <= self.low_threshold:
                self._above = False
                self._pulses += 1
            elif not self._above and voltage >= self.high_threshold:
                self._above = True
        else:
            # Idle-low sensor: each pulse is one rise above the high threshold.
            if not self._above and voltage >= self.high_threshold:
                self._above = True
                self._pulses += 1
            elif self._above and voltage <= self.low_threshold:
                self._above = False

    def _update_rate(self, now: float) -> None:
        """Recompute the flow rate over a fixed window to keep it stable."""
        elapsed = now - self._flow_window_start
        if elapsed >= self.flow_interval:
            pulses_in_window = self._pulses - self._flow_window_pulses
            liters_in_window = pulses_in_window / self.pulses_per_liter
            self._flow_rate_lpm = (liters_in_window / elapsed) * 60.0
            self._flow_window_start = now
            self._flow_window_pulses = self._pulses

    def _sample(self) -> None:
        """Take one ADC sample and update counters and cached value."""
        self._value = self.adc.read()
        self._count_edge(self.adc.voltage)
        self._update_rate(monotonic())

    def _sample_loop(self) -> None:
        """Sample the ADC on a background thread, faster than the render loop."""
        last_error_log = 0.0
        while not self._stop_event.wait(self.sample_interval):
            try:
                self._sample()
            except Exception as error:
                # Sample errors must not kill the thread; log them, throttled,
                # so a broken bus stays visible without flooding the console.
                if monotonic() - last_error_log >= 30.0:
                    last_error_log = monotonic()
                    print(f"{self.name} flow sensor sampling failed: {error}")

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
        self._pulses = 0
        self._flow_rate_lpm = 0.0
        self._flow_window_start = monotonic()
        self._flow_window_pulses = 0

    @property
    def pulses(self) -> int:
        """Total pulses counted since the last reset."""
        return self._pulses

    @property
    def liter(self) -> float:
        """Total volume of water measured, in litres."""
        return self._pulses / self.pulses_per_liter

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

    def update_home_assistant(self, client: Client):
        return client.set_state(
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
        )
