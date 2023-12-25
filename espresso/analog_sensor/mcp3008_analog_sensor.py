from gpiozero import MCP3008
from homeassistant_api import Client
from abc import ABC, abstractmethod


class MCP3008AnalogSensor(ABC):

    def __init__(self, pin, name):
        self.name = name
        self.pin = pin

        # Load MCP3008
        self.pot = MCP3008(self.pin)
        self._value = None

    def read(self):
        self._value = self.pot.value
        return self._value

    @property
    def value(self):
        if self._value is not None:
            return self._value
        else:
            return self.read()

    @abstractmethod
    def update_home_assistant(self, client: Client):
        raise NotImplemented

    @property
    @abstractmethod
    def message(self):
        raise NotImplemented

    @property
    @abstractmethod
    def normalized_value(self):
        raise NotImplemented

    @staticmethod
    @abstractmethod
    def unit_of_measurement():
        raise NotImplemented

