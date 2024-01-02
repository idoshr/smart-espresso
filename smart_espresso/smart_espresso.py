from time import sleep

from luma.oled.device import sh1106
from luma.core.render import canvas
from homeassistant_api import Client

from smart_espresso.analog_sensor.perssure_analog_sensor import MCP3008AnalogSensor
from smart_espresso.utils import font


class SmartEspresso:
    def __init__(self, analog_devices: list[MCP3008AnalogSensor], client_ha: Client, display: sh1106,
                 render_interval: float = 0.1):
        self.analog_devices: list[MCP3008AnalogSensor] = analog_devices
        self.client_ha: Client = client_ha
        self.display: sh1106 = display
        self.render_interval: float = render_interval

    def run(self):
        if not self.analog_devices:
            raise ValueError('No analog devices to read')

        while True:
            for analog_device in self.analog_devices:
                analog_device.read()

            if self.client_ha:
                for analog_device in self.analog_devices:
                    analog_device.read()

            if self.display:
                # NB this will only send the data to the display after this "with" block is complete.
                # see https://github.com/rm-hull/luma.core/blob/master/luma/core/render.py
                with canvas(self.display, dither=True) as draw:
                    position_height = 15
                    for analog_device in self.analog_devices:
                        draw.text((5, position_height), analog_device.message, fill='white', font=font)
                        position_height += 15

            sleep(self.render_interval)

        # NB the display will be turn off after we exit this application.

