from gpiozero import MCP3008
from homeassistant_api import Client, State

class MCP3008AnalogSensor:
    OFFSET_VOLTAGE = 0.32725940400586195
    OFFSET = 0.09721543722520765

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

    @property
    def mpa(self):
        if self.value < self.OFFSET:
            self.OFFSET = self.value
            print(f'Lowest value {self.value}')

        return (self.value - self.OFFSET) / 2

    @property
    def bar(self):
        return self.mpa * 10

    @property
    def message_mpa(self):
        return f'{self.name} MPa: {round(self.mpa, 4)}'

    @property
    def message_bar(self):
        return f'{self.name} Bar: {round(self.bar, 2)}'

    def update_home_assistant(self, client: Client):
        client.set_state(State(entity_id=f'sensor.espresso_machine_{self.name.lower()}_pressure',
                               state=round(self.bar, 2),
                               attributes={"unit_of_measurement": "Bar",
                                           "friendly_name": f"{self.name} Pressure"}))



    # value = pot.voltage
    # if value < OFFSET_VOLTAGE:
    #     print(f'Lowest value {value}')
    # message = f'MPa: {round(((value - OFFSET_VOLTAGE) * 3.333 / 1024) * 250, 2)}'