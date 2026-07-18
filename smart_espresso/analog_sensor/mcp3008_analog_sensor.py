from gpiozero import MCP3008

from smart_espresso.analog_sensor.analog_sensor import ADCInterface


class MCP3008ADC(ADCInterface):
    """
    MCP3008 Analog-to-Digital Converter implementation.

    10-bit ADC with SPI interface.
    8 channels (0-7).
    """

    def __init__(self, pin: int):
        """
        Initialize MCP3008 ADC.

        Args:
            pin: Channel number (0-7) on the MCP3008
        """
        if not 0 <= pin <= 7:
            raise ValueError(f"Invalid pin {pin}. Must be 0-7 for MCP3008")

        self.pin = pin
        self.pot = MCP3008(self.pin)

        # Cached from the last read() call. gpiozero's .value and .voltage
        # each trigger their own SPI transaction, so reading both separately
        # doubles the bus traffic per sample; we derive voltage from a single
        # cached .value read instead.
        self._cached_voltage = None

    def read(self):
        """Read normalized value (0.0 to 1.0) from MCP3008."""
        value = self.pot.value
        self._cached_voltage = value * self.pot.max_voltage
        return value

    @property
    def voltage(self):
        """Get the voltage from the most recent read(); triggers a hardware read on first use."""
        if self._cached_voltage is None:
            self._cached_voltage = self.pot.voltage
        return self._cached_voltage
