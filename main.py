import os

from homeassistant_api import Client
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106

from smart_espresso.analog_sensor.mcp3008_analog_sensor import MCP3008ADC
from smart_espresso.analog_sensor.ads1115_analog_sensor import ADS1115ADC
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.utils import strtobool

HA_ENABLE = strtobool(os.environ.get("HA_ENABLE") or False)
client_ha = None
if HA_ENABLE:
    HA_URL = os.environ.get("HA_URL")  # e.g. http://192.168.0.123:8123
    HA_TOKEN = os.environ.get("HA_TOKEN")
    HA_VERIFY_SSL = strtobool(os.environ.get("HA_VERIFY_SSL") or True)
    if not HA_URL or not HA_TOKEN:
        raise ValueError("HA_URL and HA_TOKEN are required")

    # Initialize HomeAssistant Client before you use it.
    print("Connecting to Home Assistant")
    client_ha = Client(f"{HA_URL}/api", HA_TOKEN, verify_ssl=HA_VERIFY_SSL)


# NB ssd1306 devices are monochromatic; a pixel is enabled with
#    white and disabled with black.
# NB the ssd1306 class has no way of knowing the display resolution/size.

# display = sh1106(i2c(port=1, address=0x3C), width=128, height=64, rotate=0)

# set the contrast to minimum.
# display.contrast(1)

# show some info.
# print(f"device size {display.size}")
# print(f"device mode {display.mode}")


if __name__ == "__main__":
    # Choose your ADC type: "MCP3008" or "ADS1115"
    # Set via environment variable or change here
    ADC_TYPE = os.environ.get("ADC_TYPE", "ADS1115")

    if ADC_TYPE == "ADS1115":
        # ADS1115 configuration (16-bit ADC, I2C interface)
        # Suitable for 5V sensors with gain=2/3 (±6.144V range)
        # or gain=1 (±4.096V range)
        analog_devices = [
            PressureAnalogSensor(
                adc=ADS1115ADC(pin=0, gain=2/3),
                name="Head"
            ),  # Head Pressure on ADS1115 channel A0
            PressureAnalogSensor(
                adc=ADS1115ADC(pin=1, gain=2/3),
                name="Boiler"
            ),  # Boiler Pressure on ADS1115 channel A1
        ]
    else:
        # MCP3008 configuration (10-bit ADC, SPI interface)
        analog_devices = [
            PressureAnalogSensor(
                adc=MCP3008ADC(pin=0),
                name="Head"
            ),  # Head Pressure
            PressureAnalogSensor(
                adc=MCP3008ADC(pin=1),
                name="Boiler"
            ),  # Boiler Pressure
        ]

    se = SmartEspresso(
        analog_devices=analog_devices, client_ha=client_ha, display=None
    )
    se.run()
# sudo ip route add 192.168.68.56 via 192.168.68.1 dev wlan0