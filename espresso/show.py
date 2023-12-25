from time import sleep

from luma.oled.device import sh1106
from luma.core.render import canvas
from homeassistant_api import Client

from espresso.analog_sensor.perssure_analog_sensor import MCP3008AnalogSensor
from espresso.utils import font


def fetch_and_show_data(analog_devices: list[MCP3008AnalogSensor], client_ha: Client, device: sh1106):
    while True:

        for analog_device in analog_devices:
            analog_device.read()

        if client_ha:
            for analog_device in analog_devices:
                analog_device.read()

        # NB this will only send the data to the display after this "with" block is complete.
        # see https://github.com/rm-hull/luma.core/blob/master/luma/core/render.py
        with canvas(device, dither=True) as draw:
            position_hight = 15
            for analog_device in analog_devices:
                draw.text((5, position_hight), analog_device.message, fill='white', font=font)
                position_hight += 15

        sleep(0.1)

    # NB the display will be turn off after we exit this application.

