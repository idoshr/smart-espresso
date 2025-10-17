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

    def read(self):
        """Read normalized value (0.0 to 1.0) from MCP3008."""
        return self.pot.value

    @property
    def voltage(self):
        """Get the actual voltage reading (assuming 3.3V reference)."""
        return self.pot.voltage
