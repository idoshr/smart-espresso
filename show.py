import os
from time import sleep

from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from homeassistant_api import Client

from mcp3008_analog_sensor import MCP3008AnalogSensor
from utils import strtobool, font


HA_ENABLE = strtobool(os.environ.get("HA_ENABLE") or False)
client_ha = None
if HA_ENABLE:
    HA_URL = os.environ.get("HA_URL")   # e.g. http://192.168.0.123:8123
    HA_TOKEN = os.environ.get("HA_TOKEN")
    HA_VERIFY_SSL = strtobool(os.environ.get("HA_VERIFY_SSL") or True)
    if not HA_URL or not HA_TOKEN:
        raise ValueError("HA_URL and HA_TOKEN are required")

    # Initialize HomeAssistant Client before you use it.
    client_ha = Client(f"{HA_URL}/api",
                       HA_TOKEN,
                       verify_ssl=HA_VERIFY_SSL)


# NB ssd1306 devices are monochromatic; a pixel is enabled with
#    white and disabled with black.
# NB the ssd1306 class has no way of knowing the device resolution/size.

device = sh1106(i2c(port=1, address=0x3c), width=128, height=64, rotate=0)

# set the contrast to minimum.
device.contrast(1)

# show some info.
print(f'device size {device.size}')
print(f'device mode {device.mode}')


analog_devices = [
                  MCP3008AnalogSensor(0, 'Head'),  # Head Pressure
                  MCP3008AnalogSensor(3, 'Boiler')
                  ]  # Boiler Pressure

while True:

    for analog_device in analog_devices:
        analog_device.read()

    if HA_ENABLE and client_ha:
        for analog_device in analog_devices:
            analog_device.read()

    # NB this will only send the data to the display after this "with" block is complete.
    # see https://github.com/rm-hull/luma.core/blob/master/luma/core/render.py
    with canvas(device, dither=True) as draw:
        position_hight = 15
        for analog_device in analog_devices:
            draw.text((5, position_hight), analog_device.message_mpa, fill='white', font=font)
            position_hight += 15

    sleep(0.1)

# NB the display will be turn off after we exit this application.

