from time import monotonic, sleep

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor
from smart_espresso.home_assistant import HomeAssistantPublisher, machine_states
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
        display=None,
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
            ha_update_interval: Minimum seconds between Home Assistant
                submissions (default: 1.0). The HTTP push runs on a worker
                thread, so this only limits how often a snapshot is queued.
        """
        self.analog_devices: list[AnalogSensor] = analog_devices or []
        self.digital_sensors: list = digital_sensors or []
        self.all_sensors = self.analog_devices + self.digital_sensors
        self.client_ha = client_ha
        self.display = display
        self.web_server: StatusServer = web_server
        self.render_interval: float = render_interval
        self.ha_update_interval: float = ha_update_interval
        self._last_ha_update: float = 0.0
        self._publisher = (
            HomeAssistantPublisher(client_ha) if client_ha is not None else None
        )
        self._health = {
            sensor.name: SensorHealth(sensor.name) for sensor in self.all_sensors
        }
        self._display_health = SensorHealth("display")
        self.ticks = 0

    def run(self, max_ticks=None, max_seconds=None):
        if not self.all_sensors:
            raise ValueError(
                "No sensors to read (provide analog_devices or digital_sensors)"
            )

        if self.web_server:
            self.web_server.start()
        if self._publisher:
            self._publisher.start()

        # max_ticks and max_seconds let a test run a finite loop. Production
        # calls run() with no limit and stays here until the process stops.
        # The count is per call, so a second run(max_ticks=n) does n more.
        started = monotonic()
        completed = 0
        try:
            while True:
                if max_ticks is not None and completed >= max_ticks:
                    break
                if max_seconds is not None and monotonic() - started >= max_seconds:
                    break
                self._tick()
                completed += 1
                self.ticks += 1
        finally:
            if self._publisher:
                self._publisher.stop()
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

        # Queue the newest snapshot for the publisher thread. The HTTP push
        # runs there, so a slow Home Assistant cannot stall sampling, the
        # shot timer or the display. Submissions are throttled; the worker
        # keeps only the newest snapshot and backs off while HA is down.
        if (
            self._publisher
            and (loop_start - self._last_ha_update) >= self.ha_update_interval
        ):
            self._publisher.submit(self._ha_states())
            self._last_ha_update = loop_start

        if self.display:
            try:
                self._draw()
            except Exception as error:
                self._display_health.record_failure(error, loop_start, action="render")

        elapsed = monotonic() - loop_start
        sleep(max(0.0, self.render_interval - elapsed))

    def _draw(self):
        # NB the luma canvas only sends the frame when this block ends.
        # A simulator display draws with Pillow, so luma is not imported.
        canvas = _display_canvas(self.display)
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

    def _ha_states(self):
        """Sensor entities that read successfully, plus machine, shot and tank."""
        states = []
        for sensor in self.all_sensors:
            if not self._health[sensor.name].ok:
                continue
            if hasattr(sensor, "ha_states"):
                states.extend(sensor.ha_states())
        if self.web_server:
            states.extend(
                machine_states(
                    self.web_server.classifier.snapshot(),
                    self.web_server.tank.snapshot(),
                )
            )
        return states


def _display_canvas(device):
    """The OLED canvas. Sim displays never import luma."""
    if getattr(device, "sim_display", False):
        from smart_espresso.sim.sensors import PillowCanvas

        return PillowCanvas
    from luma.core.render import canvas

    return canvas
