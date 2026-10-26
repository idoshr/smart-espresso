from time import monotonic, sleep

from luma.core.render import canvas
from luma.oled.device import sh1106

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor
from smart_espresso.utils import font
from smart_espresso.web.status_server import StatusServer


class SensorHealth:
    """Latest read outcome for one device, with throttled logging."""

    LOG_INTERVAL = 30.0

    def __init__(self, name):
        self.name = name
        self.ok = True
        self.last_error = None
        self.last_ok_at = None
        self.error_count = 0
        self._last_log = None

    def record_success(self, now):
        self.ok = True
        self.last_ok_at = now

    def record_failure(self, error, now, action="read"):
        self.ok = False
        self.last_error = f"{type(error).__name__}: {error}"
        self.error_count += 1
        if self._last_log is None or now - self._last_log >= self.LOG_INTERVAL:
            self._last_log = now
            print(f"{self.name} {action} failed ({self.error_count}): {error}")

    def snapshot(self, now):
        if self.last_ok_at is None:
            age = None
        else:
            age = round(max(0.0, now - self.last_ok_at), 3)
        return {
            "ok": self.ok,
            "last_error": self.last_error,
            "last_ok_age_s": age,
        }


class SmartEspresso:
    def __init__(
        self,
        analog_devices: list[AnalogSensor] = None,
        digital_sensors: list = None,
        client_ha=None,
        display: sh1106 = None,
        web_server: StatusServer = None,
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
            web_server: StatusServer serving the web dashboard (optional)
            render_interval: Update interval in seconds (default: 0.1)
            ha_update_interval: Minimum seconds between Home Assistant pushes
                (default: 1.0). Kept separate from render_interval so the
                display can refresh quickly without flooding HA's REST API
                with a request per sensor on every render tick.
        """
        self.analog_devices: list[AnalogSensor] = analog_devices or []
        self.digital_sensors: list = digital_sensors or []
        self.all_sensors = self.analog_devices + self.digital_sensors
        self.client_ha = client_ha
        self.display: sh1106 = display
        self.web_server: StatusServer = web_server
        self.render_interval: float = render_interval
        self.ha_update_interval: float = ha_update_interval
        self._last_ha_update: float = 0.0
        self._health = {
            sensor.name: SensorHealth(sensor.name) for sensor in self.all_sensors
        }
        self._display_health = SensorHealth("display")
        self.ticks = 0

    def run(self, max_ticks=None):
        if not self.all_sensors:
            raise ValueError(
                "No sensors to read (provide analog_devices or digital_sensors)"
            )

        if self.web_server:
            self.web_server.start()

        # max_ticks lets a test run a finite loop. Production calls run()
        # with no limit and stays here until the process is stopped.
        # The count is per call, so a second run(max_ticks=n) does n more.
        completed = 0
        while max_ticks is None or completed < max_ticks:
            self._tick()
            completed += 1
            self.ticks += 1
        return completed

    def _tick(self):
        loop_start = monotonic()

        for sensor in self.all_sensors:
            health = self._health[sensor.name]
            try:
                sensor.read()
            except Exception as error:
                # A failed read is "unknown", not the previous sample.
                health.record_failure(error, loop_start)
            else:
                health.record_success(loop_start)

        # Snapshot readings for the web dashboard. Cheap in-memory copy,
        # so it runs every tick and HTTP handlers never touch the ADCs.
        if self.web_server:
            self.web_server.update(
                self.all_sensors,
                health={
                    name: item.snapshot(loop_start)
                    for name, item in self._health.items()
                },
            )

        # Update Home Assistant, throttled independently of render_interval
        # so a slow/unreachable HA instance can't stall sensor sampling or
        # the display, and so we don't hammer its REST API every tick.
        if (
            self.client_ha
            and (loop_start - self._last_ha_update) >= self.ha_update_interval
        ):
            for sensor in self.all_sensors:
                try:
                    sensor.update_home_assistant(self.client_ha)
                except Exception as e:
                    print(f"Failed to update Home Assistant for {sensor.name}: {e}")
            self._last_ha_update = loop_start

        if self.display:
            try:
                self._draw()
            except Exception as error:
                self._display_health.record_failure(error, loop_start, action="render")

        elapsed = monotonic() - loop_start
        sleep(max(0.0, self.render_interval - elapsed))

    def _draw(self):
        # NB this will only send the data to the display after this "with" block is complete.
        # see https://github.com/rm-hull/luma.core/blob/master/luma/core/render.py
        with canvas(self.display, dither=True) as draw:
            position_height = 15
            for sensor in self.all_sensors:
                health = self._health[sensor.name]
                text = sensor.message if health.ok else f"{sensor.name}: fault"
                draw.text(
                    (5, position_height),
                    text,
                    fill="white",
                    font=font,
                )
                position_height += 15
