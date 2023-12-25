from gpiozero import PWMLED, MCP3008


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
