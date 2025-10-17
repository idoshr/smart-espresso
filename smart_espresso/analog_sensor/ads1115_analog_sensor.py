import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

from smart_espresso.analog_sensor.analog_sensor import ADCInterface


class ADS1115ADC(ADCInterface):
    """
    ADS1115 Analog-to-Digital Converter implementation.

    16-bit ADC with programmable gain amplifier and I2C interface.
    Works well with 5V analog sensors when configured with appropriate gain.
    4 channels (0-3).

    Args:
        pin: Channel number (0-3) on the ADS1115
        gain: Gain setting (default=1 for ±4.096V range, suitable for 5V sensors)
              Possible values: 2/3 (±6.144V), 1 (±4.096V), 2 (±2.048V),
                               4 (±1.024V), 8 (±0.512V), 16 (±0.256V)
        i2c_address: I2C address of the ADS1115 (default=0x48)
    """

    # Class-level I2C and ADS1115 instances to share across sensors
    _i2c = None
    _ads_instances = {}

    def __init__(self, pin: int, gain: float = 1, i2c_address: int = 0x48):
        if not 0 <= pin <= 3:
            raise ValueError(f"Invalid pin {pin}. Must be 0-3 for ADS1115")

        self.pin = pin
        self.gain = gain
        self.i2c_address = i2c_address

        # Initialize I2C bus (shared across all instances)
        if ADS1115ADC._i2c is None:
            ADS1115ADC._i2c = busio.I2C(board.SCL, board.SDA)

        # Get or create ADS1115 instance for this address
        if i2c_address not in ADS1115ADC._ads_instances:
            ADS1115ADC._ads_instances[i2c_address] = ADS.ADS1115(
                ADS1115ADC._i2c,
                address=i2c_address,
                gain=gain
            )

        self.ads = ADS1115ADC._ads_instances[i2c_address]

        # Map pin number to ADS1115 channel
        channel_map = {
            0: ADS.P0,
            1: ADS.P1,
            2: ADS.P2,
            3: ADS.P3
        }

        # Create analog input channel
        self.channel = AnalogIn(self.ads, channel_map[pin])

        # Calculate max voltage based on gain
        # For gain=1, max voltage is 4.096V
        # For gain=2/3, max voltage is 6.144V (suitable for full 5V range)
        self.max_voltage = 4.096 / self.gain if self.gain >= 1 else 6.144

    def read(self):
        """
        Read the normalized value from the ADS1115.
        Returns a value between 0.0 and 1.0 normalized to the voltage range.
        For gain=1 (±4.096V range), 5V sensor will read close to 1.0 at max.
        """
        voltage = self.channel.voltage
        return min(voltage / self.max_voltage, 1.0)  # Cap at 1.0

    @property
    def voltage(self):
        """Get the actual voltage reading."""
        return self.channel.voltage

    @property
    def raw_value(self):
        """Get the raw ADC value (16-bit)."""
        return self.channel.value
