"""Water-flow / shot-volume sensor support.

Hall-effect water-flow meters (e.g. YF-S201, mini DC3-24V flow sensors) emit a
square-wave pulse train on a single signal line. The pulse *frequency* is
proportional to the instantaneous flow rate and the accumulated pulse *count*
is proportional to the total volume that has passed through the sensor.

Unlike the pressure sensors, this is a **digital pulse source, not an analog
voltage**, so it does not go through an ADC — the signal wire connects
directly to a GPIO pin on the Raspberry Pi.
"""
import threading

from homeassistant_api import Client, State

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor

try:  # pragma: no cover - hardware library only present on a Pi
    from gpiozero import Button
except ImportError:  # pragma: no cover
    Button = None


class WaterFlowAnalogSensor(AnalogSensor):
    """Abstract base for water-flow sensors.

    Subclasses only have to provide :pyattr:`liter` (the accumulated volume in
    litres). Everything else — display message, Home Assistant entity and the
    normalized value — is derived from it.
    """

    def __init__(self, adc, name):
        super().__init__(adc, name)

    @property
    def liter(self):
        raise NotImplementedError

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
        client.set_state(
            State(
                entity_id=f"sensor.espresso_machine_{self.name.lower()}_flow",
                state=str(round(self.liter, 2)),
                attributes={
                    "unit_of_measurement": self.unit_of_measurement(),
                    "friendly_name": f"{self.name} Flow",
                    "device_class": "volume",
                },
            )
        )


class GPIOWaterFlowSensor(WaterFlowAnalogSensor):
    """Hall-effect water-flow meter read via GPIO pulse counting.

    The sensor's signal wire is connected to a GPIO pin; every rotation of the
    internal turbine produces a fixed number of pulses. Counting pulses gives
    the total volume, and the number of pulses per litre (a per-model constant
    that you calibrate) converts the count into litres.

    Because this is a pulse source and not an analog voltage, it does **not**
    use an ADC — :pyattr:`adc` is always ``None`` and :pymeth:`read` is
    overridden so the shared render loop never tries to poll a (missing) ADC.

    Args:
        name: Sensor name for display and Home Assistant (e.g. ``"Brew"``).
        gpio_pin: BCM GPIO pin the sensor signal is wired to (default 17).
        pulses_per_litre: Pulses emitted per litre of water. Calibrate this for
            your specific sensor; must be a positive number.
        pull_up: Whether to enable the internal pull-up resistor on the pin
            (default ``True``, matching open-collector hall-effect outputs).
        pulse_source: Optional pre-built pulse source (anything exposing a
            ``when_pressed`` attribute, e.g. a ``gpiozero.Button``). Mainly used
            to inject a fake in tests; when omitted a ``gpiozero.Button`` is
            created for ``gpio_pin``.
    """

    def __init__(
        self,
        name: str,
        gpio_pin: int = 17,
        pulses_per_litre: float = 5880.0,
        pull_up: bool = True,
        pulse_source=None,
    ):
        # --- validate configuration up front so misconfiguration fails loudly
        #     at construction time rather than producing silent garbage volumes.
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if not isinstance(gpio_pin, int) or isinstance(gpio_pin, bool) or gpio_pin < 0:
            raise ValueError(f"Invalid gpio_pin {gpio_pin!r}. Must be a non-negative integer (BCM numbering)")
        if not isinstance(pulses_per_litre, (int, float)) or isinstance(pulses_per_litre, bool):
            raise TypeError("pulses_per_litre must be a number")
        if pulses_per_litre <= 0:
            raise ValueError(f"pulses_per_litre must be positive, got {pulses_per_litre}")

        # A flow sensor has no ADC; the base AnalogSensor still stores it.
        super().__init__(adc=None, name=name)

        self.gpio_pin = gpio_pin
        self.pulses_per_litre = float(pulses_per_litre)

        # Pulse count is mutated from a background GPIO callback thread and read
        # from the main render loop, so guard it with a lock.
        self._lock = threading.Lock()
        self._pulses = 0

        if pulse_source is None:
            if Button is None:
                raise ImportError(
                    "gpiozero is required for GPIOWaterFlowSensor. Install it with "
                    "'pip3 install gpiozero', or pass a pulse_source for testing."
                )
            pulse_source = Button(gpio_pin, pull_up=pull_up)

        self._pulse_source = pulse_source
        # Count a pulse on every rising edge of the hall-effect signal.
        self._pulse_source.when_pressed = self._count_pulse

    def _count_pulse(self):
        """GPIO callback: increment the pulse counter (runs off-thread)."""
        with self._lock:
            self._pulses += 1

    @property
    def pulses(self) -> int:
        """Total pulses counted since construction (or the last :pymeth:`reset`)."""
        with self._lock:
            return self._pulses

    @property
    def liter(self) -> float:
        """Accumulated volume in litres."""
        return self.pulses / self.pulses_per_litre

    def read(self):
        """Snapshot the accumulated volume.

        Overrides :pymeth:`AnalogSensor.read`, which would otherwise call
        ``self.adc.read()`` — there is no ADC for a pulse sensor. Returning the
        litre reading keeps the shared render loop's ``sensor.read()`` call
        working without special-casing this sensor type.
        """
        self._value = self.liter
        return self._value

    def reset(self):
        """Zero the pulse counter, e.g. at the start of a new shot."""
        with self._lock:
            self._pulses = 0
        self._value = None
