import math
import os
from typing import Optional

from smart_espresso.analog_sensor.analog_sensor import ADCInterface, AnalogSensor
from smart_espresso.json_store import atomic_write_json, load_with_backup
from smart_espresso.machine_state import MachineState, MachineStateClassifier


class PressureAnalogSensor(AnalogSensor):
    """
    Pressure sensor implementation supporting multiple ADC types.

    Works with pressure sensors that output 0.5-4.5V signals (4V usable range).
    Compatible with any ADC that implements ADCInterface (MCP3008ADC, ADS1115ADC, etc.).

    Sensor voltage mapping:
    - 0.5V = 0 pressure
    - 4.5V = max_pressure_mpa
    - Linear mapping in between
    """

    # Voltage offset for zero pressure (sensors output 0.5V at 0 pressure)
    SENSOR_MIN_VOLTAGE = 0.5
    SENSOR_MAX_VOLTAGE = 4.5
    SENSOR_VOLTAGE_RANGE = SENSOR_MAX_VOLTAGE - SENSOR_MIN_VOLTAGE  # 4.0V

    # Below this the wire is open, not a pressure. It is a fault, and it
    # must not move the zero.
    DISCONNECT_VOLTAGE = 0.2
    # ±3% of the 4 V span (0.5–4.5 V). The span itself stays fixed (#52).
    ZERO_LIMIT_FRACTION = 0.03
    # How long pressure must be a known zero before the median may move
    # the offset. Head: vented and no flow. Boiler: machine off.
    ZERO_WINDOW_SECONDS = 10.0
    # A median needs more than a glitch. Five was the old confirm count.
    ZERO_MIN_SAMPLES = 5

    def __init__(
        self,
        adc: ADCInterface,
        name: str,
        max_pressure_mpa: float,
        calibration_samples: int = ZERO_MIN_SAMPLES,
        zero_window_seconds: float = ZERO_WINDOW_SECONDS,
        state_path: str | None = None,
        now=None,
    ):
        """
        Initialize pressure sensor.

        Args:
            adc: An ADC instance implementing ADCInterface (MCP3008ADC or ADS1115ADC)
            name: Name of the sensor (e.g., "Head", "Boiler")
            max_pressure_mpa: Maximum pressure rating of the sensor in MPa
                            (e.g., 2.0 for 0-2MPa sensor, 0.5 for 0-0.5MPa sensor)
            calibration_samples: Minimum samples in a zero window before the
                                 median may move the offset.
            zero_window_seconds: How long a known zero must last before the
                                 median is applied.
            state_path: File the learned offset is persisted to. None keeps
                        it in memory (tests). A leading ``~`` is expanded.
            now: Monotonic clock for the zero window. Defaults to
                 ``time.monotonic``.
        """
        super().__init__(adc, name)
        if zero_window_seconds <= 0:
            raise ValueError(
                f"zero_window_seconds must be positive (got {zero_window_seconds})"
            )
        if calibration_samples < 1:
            raise ValueError(
                f"calibration_samples must be at least 1 (got {calibration_samples})"
            )
        self.max_pressure_mpa = max_pressure_mpa
        self.calibration_samples = calibration_samples
        self.zero_window_seconds = zero_window_seconds
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self._now = now or _monotonic
        self.offset_voltage = 0.0
        self._disconnected = False
        self._trusted_since: Optional[float] = None
        self._zero_samples: list[float] = []
        self._load_offset()

    def read(self):
        """Read the ADC. An open wire raises; it is not a zero sample."""
        value = super().read()
        voltage = self.adc.voltage
        if voltage < self.DISCONNECT_VOLTAGE:
            self._disconnected = True
            self._drop_zero_window()
            raise RuntimeError("sensor disconnected")
        self._disconnected = False
        return value

    def tracks_zero(self, machine_state: str | None, flow_mls: float) -> bool:
        """True when this sample is a known zero and may train the offset.

        The boiler is at zero only while the machine is off. The head is at
        zero when it has vented: under 0.5 bar and no flow. An open wire is
        neither.
        """
        if self._disconnected:
            return False
        name = self.name.lower()
        if "boiler" in name or "steam" in name:
            return machine_state == MachineState.OFF
        if flow_mls >= MachineStateClassifier.BREW_FLOW_MLS:
            return False
        return self.bar < MachineStateClassifier.SHOT_VENT_BAR

    def observe_zero(self, trusted: bool) -> None:
        """Fold the current voltage into the zero median when ``trusted``."""
        if self._disconnected or not trusted:
            self._drop_zero_window()
            return
        voltage = self.adc.voltage
        if voltage < self.DISCONNECT_VOLTAGE:
            self._drop_zero_window()
            return
        now = self._now()
        since = self._trusted_since
        if since is None:
            self._trusted_since = now
            self._zero_samples = []
            since = now
        self._zero_samples.append(voltage)
        if now - since < self.zero_window_seconds:
            return
        if len(self._zero_samples) < self.calibration_samples:
            return
        self._apply_median()
        self._zero_samples = []
        self._trusted_since = now

    def _drop_zero_window(self) -> None:
        self._trusted_since = None
        self._zero_samples = []

    def _apply_median(self) -> None:
        """Move the offset to the window median, inside ±3% of full scale.

        The median is the slow part: a dip shorter than half the window
        cannot steer it, and it is free to move up or down.
        """
        target = _median(self._zero_samples) - self.SENSOR_MIN_VOLTAGE
        target = self._clamp_offset(target)
        if abs(target - self.offset_voltage) < 1e-6:
            return
        self.offset_voltage = target
        print(
            f"{self.name} - zero offset {self.offset_voltage:.4f} V "
            f"from {len(self._zero_samples)} samples"
        )
        self._save_offset()

    def _clamp_offset(self, offset: float) -> float:
        limit = self.SENSOR_VOLTAGE_RANGE * self.ZERO_LIMIT_FRACTION
        return max(-limit, min(limit, offset))

    def _load_offset(self) -> None:
        if not self.state_path:
            return

        def parse(payload):
            if not isinstance(payload, dict):
                raise ValueError(f"expected an object, got {type(payload).__name__}")
            offset = float(payload["offset_voltage"])
            if not math.isfinite(offset):
                raise ValueError(f"offset_voltage is not finite: {offset}")
            return offset

        loaded = load_with_backup(self.state_path, parse)
        if loaded is None:
            return
        self.offset_voltage = self._clamp_offset(loaded)

    def _save_offset(self) -> None:
        if not self.state_path:
            return
        try:
            atomic_write_json(self.state_path, {"offset_voltage": self.offset_voltage})
        except OSError as error:
            print(f"Could not save pressure zero to {self.state_path}: {error}")

    @property
    def mpa(self):
        """
        Convert ADC reading to pressure in MPa.

        The sensor outputs 0.5V at 0 pressure and 4.5V at max pressure.
        ADC returns normalized value (0.0-1.0) and voltage.

        Formula:
        1. Get actual voltage from ADC
        2. Subtract minimum sensor voltage (0.5V) and offset calibration
        3. Divide by sensor voltage range (4.0V)
        4. Multiply by sensor's max pressure rating
        """
        voltage = self.adc.voltage

        # Calculate pressure from voltage
        # voltage_adjusted = actual voltage - sensor's zero voltage - calibration offset
        voltage_adjusted = voltage - self.SENSOR_MIN_VOLTAGE - self.offset_voltage

        # Clamp to prevent negative values
        if voltage_adjusted < 0:
            voltage_adjusted = 0

        # Convert to MPa: (adjusted_voltage / 4V_range) * max_pressure
        pressure_mpa = (
            voltage_adjusted / self.SENSOR_VOLTAGE_RANGE
        ) * self.max_pressure_mpa

        return pressure_mpa

    @property
    def bar(self):
        return self.mpa * 10

    @property
    def message_mpa(self):
        return f"{self.name}: {round(self.mpa, 4)} MPa"

    @property
    def message_bar(self):
        return f"{self.name}: {round(self.bar, 2)} Bar"

    @staticmethod
    def unit_of_measurement():
        return "Bar"

    @property
    def message(self):
        return self.message_bar

    @property
    def normalized_value(self):
        return self.bar

    def ha_states(self):
        from smart_espresso.home_assistant import State

        return [
            State(
                entity_id=f"sensor.espresso_machine_{self.name.lower()}_pressure",
                state=str(round(self.bar, 2)),
                attributes={
                    "unit_of_measurement": self.unit_of_measurement(),
                    "friendly_name": f"{self.name} Pressure",
                },
            )
        ]

    def update_home_assistant(self, client):
        for state in self.ha_states():
            client.set_state(state)


def _monotonic() -> float:
    from time import monotonic

    return monotonic()


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
