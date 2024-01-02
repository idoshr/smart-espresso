from homeassistant_api import Client, State

from smart_espresso.analog_sensor.mcp3008_analog_sensor import MCP3008AnalogSensor


class WaterFlowAnalogSensor(MCP3008AnalogSensor):

    def __init__(self, pin, name):
        super().__init__(pin, name)

    @property
    def liter(self):
        raise NotImplemented

    @property
    def message_liter(self):
        return f'{self.name}: {round(self.liter, 4)} L'

    @staticmethod
    def unit_of_measurement():
        return 'L'

    @property
    def message(self):
        return self.message_liter

    @property
    def normalized_value(self):
        return self.liter

    def update_home_assistant(self, client: Client):
        client.set_state(State(entity_id=f'sensor.espresso_machine_{self.name.lower()}_pressure',
                               state=round(self.liter, 2),
                               attributes={"unit_of_measurement": self.unit_of_measurement(),
                                           "friendly_name": f"{self.name} Pressure"}))

