import os

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


def build_home_assistant_client(environ=None):
    """Build the HA client, or return None when Home Assistant is disabled.

    The client module is imported only when publishing is enabled, and the
    connect/read timeout is always passed through so a black-holed host
    cannot block the process that constructed the client.
    """
    env = os.environ if environ is None else environ
    if not strtobool(env.get("HA_ENABLE") or False):
        return None
    url = env.get("HA_URL")
    token = env.get("HA_TOKEN")
    if not url or not token:
        raise ValueError("HA_URL and HA_TOKEN are required")
    verify = strtobool(env.get("HA_VERIFY_SSL") or True)
    from smart_espresso.home_assistant import DEFAULT_TIMEOUT, Client

    print("Connecting to Home Assistant")
    return Client(f"{url}/api", token, verify_ssl=verify, timeout=DEFAULT_TIMEOUT)


client_ha = build_home_assistant_client()


# NB ssd1306 devices are monochromatic; a pixel is enabled with
#    white and disabled with black.
# NB the ssd1306 class has no way of knowing the display resolution/size.
def build_display(enabled=None, factory=None):
    """Open the OLED, or disable it with a warning if it is missing."""
    if enabled is None:
        enabled = strtobool(os.environ.get("DISPLAY_ENABLE") or False)
    if not enabled:
        return None
    if factory is None:

        def factory():
            return sh1106(i2c(port=1, address=0x3C), width=128, height=64, rotate=0)

    try:
        return factory()
    except Exception as error:
        print(f"WARNING: display disabled: {error}")
        return None


display = build_display()


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


_UNSET = object()


def build_digital_sensors(enabled=None, pin=None, factory=None):
    """Build the DHT22, or disable it with a warning if it is missing."""
    if enabled is None:
        enabled = strtobool(os.environ.get("DHT_ENABLE") or True)
    if not enabled:
        return []
    if pin is None:
        pin = int(os.environ.get("DHT_PIN") or 4)
    try:
        if factory is not None:
            return [factory()]
        from smart_espresso.analog_sensor.dht22_sensor import DHT22Sensor

        return [DHT22Sensor(pin=pin, name="Environment")]
    except Exception as error:
        print(f"WARNING: DHT22 disabled: {error}")
        return []


def _flow_sample_interval(explicit=_UNSET):
    # The render loop alone (10 Hz) misses espresso-rate pulses. The
    # default is 5 ms so a 6 ml/s pour at 30% duty still lands on a sample.
    if explicit is not _UNSET:
        return explicit
    return float(
        os.environ.get("FLOW_SAMPLE_INTERVAL")
        or WaterFlowAnalogSensor.DEFAULT_SAMPLE_INTERVAL
    )


def _default_adc(adc_type, pin, gain):
    if adc_type == "ADS1115":
        return ADS1115ADC(pin=pin, gain=gain)
    return MCP3008ADC(pin=pin)


def build_analog_devices(adc_type=None, flow_sample_interval=_UNSET, adc_builder=None):
    """
    Build the head, boiler and flow sensors.

    One channel that fails to construct (missing ADC, bad wiring) is skipped
    with a warning so the others still run. The flow channel is not median
    filtered: that filter drops one-sample pulses.
    """
    if adc_type is None:
        adc_type = os.environ.get("ADC_TYPE", "ADS1115")
    interval = _flow_sample_interval(flow_sample_interval)
    if adc_builder is None:

        def adc_builder(pin, gain):
            return _default_adc(adc_type, pin, gain)

    sensors = []

    def attempt(name, factory):
        try:
            sensors.append(factory())
        except Exception as error:
            print(f"WARNING: {name} sensor disabled: {error}")

    pressure_gain = 2 / 3
    attempt(
        "Head",
        lambda: PressureAnalogSensor(
            adc=MedianFilterADC(adc_builder(0, pressure_gain)),
            name="Head",
            max_pressure_mpa=2.0,
        ),
    )
    attempt(
        "Boiler",
        lambda: PressureAnalogSensor(
            adc=MedianFilterADC(adc_builder(1, pressure_gain)),
            name="Boiler",
            max_pressure_mpa=0.5,
        ),
    )
    attempt(
        "Brew",
        lambda: WaterFlowAnalogSensor(
            adc=adc_builder(2, 1),
            name="Brew",
            sample_interval=interval,
        ),
    )
    return sensors


# Optional DHT22 / AM2302 ambient temperature & humidity sensor (GPIO).
digital_sensors = build_digital_sensors()


if __name__ == "__main__":
    se = SmartEspresso(
        analog_devices=build_analog_devices(),
        digital_sensors=digital_sensors,
        client_ha=client_ha,
        display=display,
        web_server=web_server,
    )
    se.run()
# sudo ip route add 192.168.68.56 via 192.168.68.1 dev wlan0
