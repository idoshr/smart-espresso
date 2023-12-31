import os

from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from homeassistant_api import Client

from smart_espresso.analog_sensor.perssure_analog_sensor import PressureAnalogSensor
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.utils import strtobool


HA_ENABLE = strtobool(os.environ.get("HA_ENABLE") or False)
client_ha = None
if HA_ENABLE:
    HA_URL = os.environ.get("HA_URL")   # e.g. http://192.168.0.123:8123
    HA_TOKEN = os.environ.get("HA_TOKEN")
    HA_VERIFY_SSL = strtobool(os.environ.get("HA_VERIFY_SSL") or True)
    if not HA_URL or not HA_TOKEN:
        raise ValueError("HA_URL and HA_TOKEN are required")

    # Initialize HomeAssistant Client before you use it.
    print('Connecting to Home Assistant')
    client_ha = Client(f"{HA_URL}/api",
                       HA_TOKEN,
                       verify_ssl=HA_VERIFY_SSL)


# NB ssd1306 devices are monochromatic; a pixel is enabled with
#    white and disabled with black.
# NB the ssd1306 class has no way of knowing the display resolution/size.

display = sh1106(i2c(port=1, address=0x3c), width=128, height=64, rotate=0)

# set the contrast to minimum.
display.contrast(1)

# show some info.
print(f'device size {display.size}')
print(f'device mode {display.mode}')


if __name__ == '__main__':
    analog_devices = [
        PressureAnalogSensor(pin=0, name='Head'),         # Head Pressure
        PressureAnalogSensor(pin=3, name='Boiler')        # Boiler Pressure
    ]
    se = SmartEspresso(analog_devices=analog_devices,
                       client_ha=client_ha,
                       display=display)
    se.run()
