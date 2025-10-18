"""DHT22 (AM2302) Temperature and Humidity Sensor implementation."""
import time
from typing import Optional

try:
    import Adafruit_DHT
except ImportError:
    Adafruit_DHT = None

from homeassistant_api import Client


class DHT22Sensor:
    """
    DHT22 (AM2302) digital temperature and humidity sensor.

    Note: This is a digital sensor, not analog, so it doesn't use the ADCInterface.
    """

    def __init__(self, pin: int, name: str = "DHT22", use_fahrenheit: bool = False):
        """
        Initialize DHT22 sensor.

        Args:
            pin: GPIO pin number (BCM numbering, e.g., 4 for GPIO4)
            name: Sensor name for display and Home Assistant
            use_fahrenheit: If True, return temperature in Fahrenheit instead of Celsius
        """
        if Adafruit_DHT is None:
            raise ImportError(
                "Adafruit_DHT library not found. Install with: "
                "sudo pip3 install Adafruit_DHT"
            )

        self.name = name
        self.pin = pin
        self.use_fahrenheit = use_fahrenheit
        self._temperature: Optional[float] = None
        self._humidity: Optional[float] = None
        self._last_read_time = 0
        self._min_read_interval = 2.0  # DHT22 max sampling rate is 0.5Hz (every 2 seconds)
        self.sensor_type = Adafruit_DHT.DHT22

    def read(self) -> tuple[Optional[float], Optional[float]]:
        """
        Read temperature and humidity from sensor.

        Returns:
            Tuple of (humidity, temperature) or (None, None) if read failed

        Note: DHT22 can only be read every 2 seconds maximum
        """
        current_time = time.time()

        # Enforce minimum read interval to avoid sensor errors
        if current_time - self._last_read_time < self._min_read_interval:
            return self._humidity, self._temperature

        # Read with retry for reliability
        humidity, temperature = Adafruit_DHT.read_retry(self.sensor_type, self.pin)

        if humidity is not None and temperature is not None:
            self._humidity = humidity
            self._temperature = temperature
            self._last_read_time = current_time

            # Convert to Fahrenheit if requested
            if self.use_fahrenheit and temperature is not None:
                self._temperature = temperature * 9.0 / 5.0 + 32.0

        return self._humidity, self._temperature

    @property
    def temperature(self) -> Optional[float]:
        """Get the last temperature reading."""
        return self._temperature

    @property
    def humidity(self) -> Optional[float]:
        """Get the last humidity reading."""
        return self._humidity

    @property
    def temperature_unit(self) -> str:
        """Get temperature unit symbol."""
        return "°F" if self.use_fahrenheit else "°C"

    @property
    def message(self) -> str:
        """
        Get formatted message for display.

        Returns:
            Formatted string with temperature and humidity
        """
        if self._temperature is None or self._humidity is None:
            return f"{self.name}: No data"

        temp_str = f"{self._temperature:.1f}{self.temperature_unit}"
        humidity_str = f"{self._humidity:.1f}%"
        return f"{self.name}: {temp_str} {humidity_str}"

    def update_home_assistant(self, client: Client):
        """
        Update Home Assistant with current sensor readings.

        Args:
            client: Home Assistant API client
        """
        if client is None:
            return

        # Create entity IDs based on sensor name
        temp_entity_id = f"sensor.{self.name.lower().replace(' ', '_')}_temperature"
        humidity_entity_id = f"sensor.{self.name.lower().replace(' ', '_')}_humidity"

        try:
            # Update temperature sensor
            if self._temperature is not None:
                client.set_state(
                    entity_id=temp_entity_id,
                    state=round(self._temperature, 1),
                    attributes={
                        "unit_of_measurement": self.temperature_unit,
                        "friendly_name": f"{self.name} Temperature",
                        "device_class": "temperature",
                    },
                )

            # Update humidity sensor
            if self._humidity is not None:
                client.set_state(
                    entity_id=humidity_entity_id,
                    state=round(self._humidity, 1),
                    attributes={
                        "unit_of_measurement": "%",
                        "friendly_name": f"{self.name} Humidity",
                        "device_class": "humidity",
                    },
                )
        except Exception as e:
            print(f"Failed to update Home Assistant for {self.name}: {e}")

    def __repr__(self) -> str:
        return f"DHT22Sensor(pin={self.pin}, name='{self.name}', temp={self._temperature}, humidity={self._humidity})"

