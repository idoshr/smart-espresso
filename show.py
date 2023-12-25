import os
from time import sleep

from mcp3008_analog_sensor import MCP3008AnalogSensor
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from PIL import ImageFont
from datetime import datetime
from homeassistant_api import Client, State

# You can also initialize Client before you use it.

client = Client("http://192.168.68.56:8123/api",
                os.environ.get("HA_TOKEN"),
                verify_ssl=False)


# NB ssd1306 devices are monochromatic; a pixel is enabled with
#    white and disabled with black.
# NB the ssd1306 class has no way of knowing the device resolution/size.
device = sh1106(i2c(port=1, address=0x3c), width=128, height=64, rotate=0)

# set the contrast to minimum.
device.contrast(1)


font = ImageFont.truetype("Roboto-Regular.ttf", 12)

# show some info.
print(f'device size {device.size}')
print(f'device mode {device.mode}')

# NB this will only send the data to the display after this "with" block is complete.
# NB the draw variable is-a PIL.ImageDraw.Draw (https://pillow.readthedocs.io/en/3.1.x/reference/ImageDraw.html).
# see https://github.com/rm-hull/luma.core/blob/master/luma/core/render.py


head_p = MCP3008AnalogSensor(0, 'Head')     # Head Pressure
boiler_p = MCP3008AnalogSensor(3, 'Boiler')     # Boiler Pressure
while True:
    # value = pot.voltage
    # if value < OFFSET_VOLTAGE:
    #     print(f'Lowest value {value}')
    # message = f'MPa: {round(((value - OFFSET_VOLTAGE) * 3.333 / 1024) * 250, 2)}'
    head_p.read()
    boiler_p.read()
    client.set_state(State(entity_id='sensor.espresso_machine_boiler_pressure',
                           state=round(boiler_p.bar, 2),
                           attributes={"unit_of_measurement": "Bar",
                                       "friendly_name": "Boiler Pressure"}))

    client.set_state(State(entity_id='sensor.espresso_machine_head_pressure',
                           state=round(head_p.bar, 2),
                           attributes={"unit_of_measurement": "Bar",
                                       "friendly_name": "Head Pressure"}))
    with canvas(device, dither=True) as draw:
        draw.text((5, 15), boiler_p.message_mpa, fill='white', font=font)
        draw.text((5, 30), boiler_p.message_bar, fill='white', font=font)
        draw.text((5, 45), head_p.message_bar, fill='white', font=font)

    sleep(0.1)

# NB the display will be turn off after we exit this application.

