import os

from homeassistant_api import Client
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106

from smart_espresso.analog_sensor.ads1115_analog_sensor import ADS1115ADC
from smart_espresso.analog_sensor.analog_sensor import MedianFilterADC
from smart_espresso.analog_sensor.mcp3008_analog_sensor import MCP3008ADC
from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.shot_history import DEFAULT_STATE_PATH as DEFAULT_SHOT_HISTORY_PATH
from smart_espresso.shot_history import ShotHistory
from smart_espresso.smart_espresso import SmartEspresso
from smart_espresso.utils import strtobool
from smart_espresso.water_tank import DEFAULT_STATE_PATH, WaterTank
from smart_espresso.web.status_server import StatusServer

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
DISPLAY_ENABLE = strtobool(os.environ.get("DISPLAY_ENABLE") or False)
display = None
if DISPLAY_ENABLE:
    display = sh1106(i2c(port=1, address=0x3C), width=128, height=64, rotate=0)


# Mobile status dashboard, served on the local network.
WEB_ENABLE = strtobool(os.environ.get("WEB_ENABLE") or True)
web_server = None
if WEB_ENABLE:
    web_server = StatusServer(
        host=os.environ.get("WEB_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEB_PORT", 8080)),
        tank=WaterTank(
            capacity_ml=float(os.environ.get("WATER_TANK_ML", 2000)),
            state_path=os.environ.get("WATER_TANK_STATE", DEFAULT_STATE_PATH),
        ),
        shot_history=ShotHistory(
            state_path=os.environ.get("SHOT_HISTORY_STATE", DEFAULT_SHOT_HISTORY_PATH),
            max_shots=int(os.environ.get("SHOT_HISTORY_MAX", ShotHistory.MAX_SHOTS)),
        ),
    )


# Optional DHT22 / AM2302 ambient temperature & humidity sensor (GPIO).
# Skipped gracefully when the library is missing so the machine keeps running.
DHT_ENABLE = strtobool(os.environ.get("DHT_ENABLE") or True)
DHT_PIN = int(os.environ.get("DHT_PIN") or 4)
digital_sensors = []
if DHT_ENABLE:
    try:
        from smart_espresso.analog_sensor.dht22_sensor import DHT22Sensor

        digital_sensors.append(DHT22Sensor(pin=DHT_PIN, name="Environment"))
    except ImportError as error:
        print(f"DHT22 disabled: {error}")


if __name__ == "__main__":
    # Choose your ADC type: "MCP3008" or "ADS1115"
    # Set via environment variable or change here
    ADC_TYPE = os.environ.get("ADC_TYPE", "ADS1115")

    # How often the flow sensor's background thread samples the ADC. The
    # render loop alone (10 Hz) misses espresso-rate pulses (~20-30 Hz).
    FLOW_SAMPLE_INTERVAL = float(os.environ.get("FLOW_SAMPLE_INTERVAL") or 0.01)

    if ADC_TYPE == "ADS1115":
        # ADS1115 configuration (16-bit ADC, I2C interface)
        # Suitable for 5V sensors with gain=2/3 (±6.144V range)
        # or gain=1 (±4.096V range)
        analog_devices = [
            PressureAnalogSensor(
                adc=MedianFilterADC(ADS1115ADC(pin=0, gain=2 / 3)),
                name="Head",
                max_pressure_mpa=2.0,  # 0-2MPa sensor for brew head (0-20 bar)
            ),  # Head Pressure on ADS1115 channel A0
            PressureAnalogSensor(
                adc=MedianFilterADC(ADS1115ADC(pin=1, gain=2 / 3)),
                name="Boiler",
                max_pressure_mpa=0.5,  # 0-0.5MPa sensor for boiler (0-5 bar)
            ),  # Boiler Pressure on ADS1115 channel A1
            WaterFlowAnalogSensor(
                adc=MedianFilterADC(ADS1115ADC(pin=2, gain=1)),
                name="Brew",
                sample_interval=FLOW_SAMPLE_INTERVAL,
            ),  # Water flow (hall-effect pulse) on ADS1115 channel A2 (3rd port)
        ]
    else:
        # MCP3008 configuration (10-bit ADC, SPI interface)
        analog_devices = [
            PressureAnalogSensor(
                adc=MedianFilterADC(MCP3008ADC(pin=0)),
                name="Head",
                max_pressure_mpa=2.0,  # 0-2MPa sensor for brew head (0-20 bar)
            ),  # Head Pressure
            PressureAnalogSensor(
                adc=MedianFilterADC(MCP3008ADC(pin=1)),
                name="Boiler",
                max_pressure_mpa=0.5,  # 0-0.5MPa sensor for boiler (0-5 bar)
            ),  # Boiler Pressure
            WaterFlowAnalogSensor(
                adc=MedianFilterADC(MCP3008ADC(pin=2)),
                name="Brew",
                sample_interval=FLOW_SAMPLE_INTERVAL,
            ),  # Water flow (hall-effect pulse) on MCP3008 channel CH2
        ]

    se = SmartEspresso(
        analog_devices=analog_devices,
        digital_sensors=digital_sensors,
        client_ha=client_ha,
        display=display,
        web_server=web_server,
    )
    se.run()
# sudo ip route add 192.168.68.56 via 192.168.68.1 dev wlan0
