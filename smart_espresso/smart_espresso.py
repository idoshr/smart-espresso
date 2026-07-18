from time import monotonic, sleep
from typing import Union

from homeassistant_api import Client
from luma.core.render import canvas
from luma.oled.device import sh1106

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor
from smart_espresso.utils import font


class SmartEspresso:
    def __init__(
        self,
        analog_devices: list[AnalogSensor] = None,
        digital_sensors: list = None,
        client_ha: Client = None,
        display: sh1106 = None,
        render_interval: float = 0.1,
        ha_update_interval: float = 1.0,
    ):
        """
        Initialize SmartEspresso monitoring system.

        Args:
            analog_devices: List of analog sensors (pressure, flow, etc.)
            digital_sensors: List of digital sensors (DHT22, etc.)
            client_ha: Home Assistant API client
            display: OLED display device
            render_interval: Update interval in seconds (default: 0.1)
            ha_update_interval: Minimum seconds between Home Assistant pushes
                (default: 1.0). Kept separate from render_interval so the
                display can refresh quickly without flooding HA's REST API
                with a request per sensor on every render tick.
        """
        self.analog_devices: list[AnalogSensor] = analog_devices or []
        self.digital_sensors: list = digital_sensors or []
        self.all_sensors = self.analog_devices + self.digital_sensors
        self.client_ha: Client = client_ha
        self.display: sh1106 = display
        self.render_interval: float = render_interval
        self.ha_update_interval: float = ha_update_interval
        self._last_ha_update: float = 0.0

    def run(self):
        if not self.all_sensors:
            raise ValueError("No sensors to read (provide analog_devices or digital_sensors)")

        while True:
            loop_start = monotonic()

            # Read all sensors
            for sensor in self.all_sensors:
                sensor.read()

            # Update Home Assistant, throttled independently of render_interval
            # so a slow/unreachable HA instance can't stall sensor sampling or
            # the display, and so we don't hammer its REST API every tick.
            if self.client_ha and (loop_start - self._last_ha_update) >= self.ha_update_interval:
                for sensor in self.all_sensors:
                    try:
                        sensor.update_home_assistant(self.client_ha)
                    except Exception as e:
                        print(f"Failed to update Home Assistant for {sensor.name}: {e}")
                self._last_ha_update = loop_start

            # Update display
            if self.display:
                # NB this will only send the data to the display after this "with" block is complete.
                # see https://github.com/rm-hull/luma.core/blob/master/luma/core/render.py
                with canvas(self.display, dither=True) as draw:
                    position_height = 15
                    for sensor in self.all_sensors:
                        draw.text(
                            (5, position_height),
                            sensor.message,
                            fill="white",
                            font=font,
                        )
                        position_height += 15

            elapsed = monotonic() - loop_start
            sleep(max(0.0, self.render_interval - elapsed))

        # NB the display will be turn off after we exit this application.
