from homeassistant_api import Client, State

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor, ADCInterface


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

    # How many consecutive below-zero samples must be seen before the
    # auto-calibration moves the offset. A single noise glitch must not
    # count, otherwise one spike permanently skews every later reading.
    CALIBRATION_CONFIRM_SAMPLES = 5

    def __init__(
        self,
        adc: ADCInterface,
        name: str,
        max_pressure_mpa: float,
        calibration_samples: int = CALIBRATION_CONFIRM_SAMPLES,
    ):
        """
        Initialize pressure sensor.

        Args:
            adc: An ADC instance implementing ADCInterface (MCP3008ADC or ADS1115ADC)
            name: Name of the sensor (e.g., "Head", "Boiler")
            max_pressure_mpa: Maximum pressure rating of the sensor in MPa
                            (e.g., 2.0 for 0-2MPa sensor, 0.5 for 0-0.5MPa sensor)
            calibration_samples: Consecutive low samples required before the
                                 zero offset is re-calibrated.
        """
        super().__init__(adc, name)
        self.max_pressure_mpa = max_pressure_mpa
        self.offset_voltage = 0.0  # Auto-calibrated offset for fine-tuning
        self.calibration_samples = calibration_samples
        self._low_streak = 0

    def read(self):
        """Read the ADC and update the zero offset from confirmed samples only."""
        value = super().read()
        self._auto_calibrate(self.adc.voltage)
        return value

    def _auto_calibrate(self, voltage: float) -> None:
        """Track the zero-pressure offset, ignoring noise glitches.

        The offset may only move after ``calibration_samples`` consecutive
        below-zero readings — a sustained drift, not a spike. Moving it on a
        single glitched sample would add a phantom baseline (with the -0.1V
        cap, up to +0.5 Bar on a 0-2MPa sensor) to every later reading.
        """
        if voltage < self.SENSOR_MIN_VOLTAGE + self.offset_voltage:
            self._low_streak += 1
            if self._low_streak >= self.calibration_samples:
                new_offset = max(voltage - self.SENSOR_MIN_VOLTAGE, -0.1)
                if new_offset < self.offset_voltage:
                    self.offset_voltage = new_offset
                    print(
                        f"{self.name} - Calibrating offset: "
                        f"{self.offset_voltage:.4f}V (voltage: {voltage:.4f}V)"
                    )
                self._low_streak = 0
        else:
            self._low_streak = 0

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
        pressure_mpa = (voltage_adjusted / self.SENSOR_VOLTAGE_RANGE) * self.max_pressure_mpa

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

    def update_home_assistant(self, client: Client):
        return client.set_state(
            State(
                entity_id=f"sensor.espresso_machine_{self.name.lower()}_pressure",
                state=str(round(self.bar, 2)),
                attributes={
                    "unit_of_measurement": self.unit_of_measurement(),
                    "friendly_name": f"{self.name} Pressure",
                },
            )
        )

    # value = pot.voltage
    # if value < OFFSET_VOLTAGE:
    #     print(f'Lowest value {value}')
    # message = f'MPa: {round(((value - OFFSET_VOLTAGE) * 3.333 / 1024) * 250, 2)}'
