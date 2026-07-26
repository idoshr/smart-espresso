from time import monotonic

from homeassistant_api import Client, State

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor, ADCInterface


class WaterFlowAnalogSensor(AnalogSensor):
    """
    Hall-effect water flow / shot-volume meter read through an analog ADC channel.

    A hall-effect flow sensor outputs a square-wave pulse train whose frequency
    is proportional to the flow rate; each pulse represents a fixed volume of
    water. When the pulse output is wired to an ADC channel (e.g. the ADS1115
    A2 input) instead of a GPIO edge-interrupt pin, we recover the pulses by
    sampling the channel voltage on every read() and counting low->high
    transitions.

    Edge detection uses a two-level (Schmitt-trigger style) hysteresis so a
    single, possibly noisy, pulse is counted exactly once:
      * a pulse is counted when the voltage rises above ``high_threshold``
      * the detector re-arms only once the voltage falls below ``low_threshold``

    Accuracy is bounded by the sampling rate (the SmartEspresso render loop, by
    default ~10 Hz): to avoid missing pulses the sampling rate must comfortably
    exceed the sensor's maximum pulse frequency. For high-flow applications a
    GPIO edge-interrupt wiring is more accurate, but ADC sampling is sufficient
    for espresso-scale shot metering.
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
    ):
        """
        Initialize the water flow sensor.

        Args:
            adc: An ADC instance implementing ADCInterface (ADS1115ADC, MCP3008ADC).
            name: Sensor name for display and Home Assistant (e.g. "Brew").
            pulses_per_liter: Calibration constant for the sensor.
            high_threshold: Voltage at/above which a rising edge is counted.
            low_threshold: Voltage at/below which the detector re-arms.
            flow_interval: Window in seconds over which the flow rate (L/min)
                is recomputed. Kept above the sampling period so the rate is
                averaged over several samples rather than jittering per tick.
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

        self._pulses = 0
        self._above = False  # current hysteresis state (True once above high)
        self._flow_rate_lpm = 0.0
        self._flow_window_start = monotonic()
        self._flow_window_pulses = 0

    def read(self):
        """
        Sample the ADC channel, count any pulse edge, and update the flow rate.

        Returns the ADC's normalized value (0.0-1.0) for consistency with the
        base class, though callers typically use ``liter`` / ``flow_rate``.
        """
        value = super().read()  # populates self._value and caches the voltage
        voltage = self.adc.voltage
        now = monotonic()

        # Rising-edge detection with hysteresis: count once per pulse.
        if not self._above and voltage >= self.high_threshold:
            self._above = True
            self._pulses += 1
        elif self._above and voltage <= self.low_threshold:
            self._above = False

        # Recompute the flow rate over a fixed window to keep it stable.
        elapsed = now - self._flow_window_start
        if elapsed >= self.flow_interval:
            pulses_in_window = self._pulses - self._flow_window_pulses
            liters_in_window = pulses_in_window / self.pulses_per_liter
            self._flow_rate_lpm = (liters_in_window / elapsed) * 60.0
            self._flow_window_start = now
            self._flow_window_pulses = self._pulses

        return value

    def reset(self):
        """Zero the accumulated volume (e.g. at the start of a new shot)."""
        self._pulses = 0
        self._above = False
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
