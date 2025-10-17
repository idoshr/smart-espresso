from abc import ABC, abstractmethod

from homeassistant_api import Client


class ADCInterface(ABC):
    """Abstract interface for Analog-to-Digital Converters."""

    @abstractmethod
    def read(self):
        """
        Read normalized value from ADC.
        Returns a float between 0.0 and 1.0.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def voltage(self):
        """Get the actual voltage reading."""
        raise NotImplementedError


class AnalogSensor(ABC):
    """Abstract base class for analog sensors (pressure, flow, temperature, etc.)."""

    def __init__(self, adc: ADCInterface, name: str):
        self.name = name
        self.adc = adc
        self._value = None

    def read(self):
        """Read the raw value from the ADC."""
        self._value = self.adc.read()
        return self._value

    @property
    def value(self):
        if self._value is not None:
            return self._value
        else:
            return self.read()

    @abstractmethod
    def update_home_assistant(self, client: Client):
        raise NotImplementedError

    @property
    @abstractmethod
    def message(self):
        raise NotImplementedError

    @property
    @abstractmethod
    def normalized_value(self):
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def unit_of_measurement():
        raise NotImplementedError
