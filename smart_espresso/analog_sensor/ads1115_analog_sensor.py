import threading

import adafruit_ads1x15.ads1115 as ADS
import board
import busio
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
        data_rate: Conversion rate in samples per second (default=860, the
                   maximum). The driver busy-polls the I2C bus under a global
                   lock for the whole conversion, so at the 128 SPS default
                   each read stalls every other reader for ~8ms; with three
                   channels read at 10 Hz plus the flow sampler at 200 Hz
                   the bus is busy for roughly a quarter of the time.
                   860 SPS cuts a conversion to ~1.2ms.
    """

    # One ADS1115 per address is shared by every channel; this lock
    # serialises conversions across threads (the flow sensor samples on a
    # background thread) because the I2C device and its scratch buffer
    # are not safe to use concurrently.
    _bus_lock = threading.Lock()

    # Class-level I2C and ADS1115 instances to share across sensors
    _i2c = None
    _ads_instances = {}

    def __init__(
        self, pin: int, gain: float = 1, i2c_address: int = 0x48, data_rate: int = 860
    ):
        if not 0 <= pin <= 3:
            raise ValueError(f"Invalid pin {pin}. Must be 0-3 for ADS1115")

        self.pin = pin
        self.gain = gain
        self.i2c_address = i2c_address
        self.data_rate = data_rate

        # Initialize I2C bus (shared across all instances)
        if ADS1115ADC._i2c is None:
            ADS1115ADC._i2c = busio.I2C(board.SCL, board.SDA)

        # Get or create ADS1115 instance for this address
        if i2c_address not in ADS1115ADC._ads_instances:
            ADS1115ADC._ads_instances[i2c_address] = ADS.ADS1115(
                ADS1115ADC._i2c, address=i2c_address, gain=gain, data_rate=data_rate
            )

        self.ads = ADS1115ADC._ads_instances[i2c_address]

        # Map pin number to ADS1115 channel
        channel_map = {0: ADS.P0, 1: ADS.P1, 2: ADS.P2, 3: ADS.P3}

        # Create analog input channel
        self.channel = AnalogIn(self.ads, channel_map[pin])

        # Calculate max voltage based on gain
        # For gain=1, max voltage is 4.096V
        # For gain=2/3, max voltage is 6.144V (suitable for full 5V range)
        self.max_voltage = 4.096 / self.gain if self.gain >= 1 else 6.144

        # Cached from the last read() call so that voltage can be read
        # repeatedly within a loop iteration without re-triggering an I2C
        # conversion (at 860 SPS each conversion takes ~1.2ms).
        self._cached_voltage = None

    def _locked_voltage(self) -> float:
        """Read the channel voltage, applying this channel's gain first.

        The ADS1115 instance is shared per I2C address, so without this the
        first channel's gain would silently win for every channel on the
        device: the driver builds its conversion config from the current
        ``gain`` attribute at read time. The same applies to ``data_rate``.
        """
        self.ads.gain = self.gain
        self.ads.data_rate = self.data_rate
        return self.channel.voltage

    def read(self):
        """
        Read the normalized value from the ADS1115.
        Returns a value between 0.0 and 1.0 normalized to the voltage range.
        For gain=1 (±4.096V range), 5V sensor will read close to 1.0 at max.
        """
        with ADS1115ADC._bus_lock:
            self._cached_voltage = self._locked_voltage()
        # Clamp both ends: electrical noise can produce a small negative
        # sample on an idle sensor, which would read as negative pressure.
        return max(0.0, min(self._cached_voltage / self.max_voltage, 1.0))

    @property
    def voltage(self):
        """Get the voltage from the most recent read(); triggers a hardware read on first use."""
        if self._cached_voltage is None:
            with ADS1115ADC._bus_lock:
                self._cached_voltage = self._locked_voltage()
        return self._cached_voltage

    @property
    def raw_value(self):
        """Get the raw ADC value (16-bit)."""
        with ADS1115ADC._bus_lock:
            self.ads.gain = self.gain
            self.ads.data_rate = self.data_rate
            return self.channel.value
