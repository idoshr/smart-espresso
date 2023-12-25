from homeassistant_api import Client, State

from espresso.analog_sensor.mcp3008_analog_sensor import MCP3008AnalogSensor


class PressureAnalogSensor(MCP3008AnalogSensor):
    OFFSET_VOLTAGE = 0.32725940400586195
    OFFSET = 0.09721543722520765

    def __init__(self, pin, name):
        super().__init__(pin, name)

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

    @staticmethod
    def unit_of_measurement():
        return 'Bar'

    @property
    def message(self):
        return self.message_bar

    @property
    def normalized_value(self):
        return self.bar

    def update_home_assistant(self, client: Client):
        client.set_state(State(entity_id=f'sensor.espresso_machine_{self.name.lower()}_pressure',
                               state=round(self.bar, 2),
                               attributes={"unit_of_measurement": self.unit_of_measurement(),
                                           "friendly_name": f"{self.name} Pressure"}))



    # value = pot.voltage
    # if value < OFFSET_VOLTAGE:
    #     print(f'Lowest value {value}')
    # message = f'MPa: {round(((value - OFFSET_VOLTAGE) * 3.333 / 1024) * 250, 2)}'