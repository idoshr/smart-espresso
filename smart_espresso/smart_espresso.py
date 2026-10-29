import signal
import threading
from time import monotonic

from smart_espresso.analog_sensor.analog_sensor import AnalogSensor
from smart_espresso.home_assistant import HomeAssistantPublisher, machine_states
from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.sd_notify import notify_systemd
from smart_espresso.shot_history import ShotHistory
from smart_espresso.utils import font
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import (
    StatusServer,
    collect_sensor_readings,
    sensor_text,
)


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
        tank: WaterTank = None,
        shot_history: ShotHistory = None,
        classifier: MachineStateClassifier = None,
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
            tank: Tank counter. With a web server, the server's tank is used.
                Headless runs keep their own, in memory unless a path is set.
            shot_history: Shot log. Same rule as ``tank``.
            classifier: Machine state. Same rule as ``tank``.
        """
        self.analog_devices: list[AnalogSensor] = analog_devices or []
        self.digital_sensors: list = digital_sensors or []
        self.all_sensors = self.analog_devices + self.digital_sensors
        self.client_ha = client_ha
        self.display = display
        self.web_server: StatusServer = web_server
        if web_server is not None:
            self.tank = web_server.tank
            self.shot_history = web_server.shot_history
            self.classifier = web_server.classifier
        else:
            # In memory: importing or constructing the app must not start a
            # writer thread against the real shot log.
            self.shot_history = (
                shot_history
                if shot_history is not None
                else ShotHistory(state_path=None)
            )
            self.tank = tank if tank is not None else WaterTank(state_path=None)
            self.classifier = (
                classifier
                if classifier is not None
                else MachineStateClassifier(shot_history=self.shot_history)
            )
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
        self._stop = threading.Event()
        self._shut_down = False
        self._previous_signals = {}
        # systemd restarts us if this is quieter than WatchdogSec (30s).
        self._watchdog_interval = 10.0

    def run(self, max_ticks=None, max_seconds=None):
        if not self.all_sensors:
            raise ValueError(
                "No sensors to read (provide analog_devices or digital_sensors)"
            )

        installed = self._install_signals()
        self._stop.clear()
        try:
            if self.web_server:
                self.web_server.start()
            if self._publisher:
                self._publisher.start()

            notify_systemd("READY=1")
            notify_systemd("WATCHDOG=1")
            last_watchdog = monotonic()

            # max_ticks and max_seconds let a test run a finite loop. Production
            # calls run() with no limit and stays here until the process stops.
            # The count is per call, so a second run(max_ticks=n) does n more.
            started = monotonic()
            completed = 0
            while not self._stop.is_set():
                if max_ticks is not None and completed >= max_ticks:
                    break
                if max_seconds is not None and monotonic() - started >= max_seconds:
                    break
                self._tick()
                completed += 1
                self.ticks += 1
                now = monotonic()
                if now - last_watchdog >= self._watchdog_interval:
                    notify_systemd("WATCHDOG=1")
                    last_watchdog = now
            return completed
        finally:
            self.shutdown()
            if installed:
                self._restore_signals()

    def _tick(self):
        loop_start = monotonic()

        for sensor in self.all_sensors:
            self._record_read(sensor, loop_start)

        # Snapshot readings for the web dashboard. Cheap in-memory copy,
        # so it runs every tick and HTTP handlers never touch the ADCs.
        health = {
            name: item.snapshot(loop_start) for name, item in self._health.items()
        }
        if self.web_server:
            self.web_server.update(self.all_sensors, health=health)
        else:
            self._feed_headless(health)
        self._observe_pressure_zeros()

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
        # Event.wait returns as soon as SIGTERM sets the stop flag, so
        # shutdown is not stuck for the rest of the render interval.
        self._stop.wait(max(0.0, self.render_interval - elapsed))

    def _feed_headless(self, health) -> None:
        """Advance state, tank and history when no web server is running."""
        readings = collect_sensor_readings(self.all_sensors, health)
        self.classifier.update(
            readings["head_bar"],
            readings["boiler_bar"],
            readings["flow_mls"],
            readings["total_ml"],
            flow_sensor=readings["flow_sensor"],
        )
        if readings["tank_ml"] is not None:
            self.tank.update(readings["tank_ml"])

    def _install_signals(self) -> bool:
        if threading.current_thread() is not threading.main_thread():
            return False
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous_signals[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)
        return True

    def _handle_signal(self, signum, frame) -> None:
        self._stop.set()

    def _restore_signals(self) -> None:
        for sig, previous in self._previous_signals.items():
            signal.signal(sig, previous)
        self._previous_signals = {}

    def shutdown(self) -> None:
        """Flush durable state and stop sampler threads.

        Safe to call more than once. Sensor threads are joined from their
        own ``stop`` methods; the flow sampler's join is 1 s and the DHT
        poll returns as soon as its stop flag is set.
        """
        if self._shut_down:
            return
        self._shut_down = True
        self._stop.set()
        self._stop_sensors()
        close = getattr(self.classifier, "close_open_pull", None)
        if close is not None:
            self._quiet("Could not close the open pull", close)
        self._flush_history()
        save = getattr(self.tank, "save", None)
        if save is not None:
            self._quiet("Could not save the water tank", save)
        if self._publisher is not None:
            self._publisher.stop()

    def _stop_sensors(self) -> None:
        for sensor in list(self.all_sensors):
            stop = getattr(sensor, "stop", None)
            if stop is None:
                continue
            name = getattr(sensor, "name", "sensor")
            self._quiet(f"{name} stop failed", stop)

    def _flush_history(self) -> None:
        history = self.shot_history
        if history is None:
            return
        self._quiet("Could not flush shot history", history.flush)
        closer = getattr(history, "close", None)
        if closer is not None:
            self._quiet("Could not close shot history", closer)

    @staticmethod
    def _quiet(label: str, func) -> None:
        try:
            func()
        except Exception as error:
            print(f"{label}: {error}")

    def _observe_pressure_zeros(self) -> None:
        """Train a pressure zero only while that sensor is known to read zero."""
        machine_state = None
        if self.classifier is not None:
            machine_state = self.classifier.snapshot()["state"]
        flow_mls = 0.0
        for sensor in self.analog_devices:
            rate = getattr(sensor, "flow_rate", None)
            if rate is not None:
                flow_mls += float(rate) * 1000.0 / 60.0
        for sensor in self.analog_devices:
            observe = getattr(sensor, "observe_zero", None)
            if observe is None:
                continue
            observe(sensor.tracks_zero(machine_state, flow_mls))

    def _draw(self):
        # NB the luma canvas only sends the frame when this block ends.
        # A simulator display draws with Pillow, so luma is not imported.
        canvas = _display_canvas(self.display)
        with canvas(self.display, dither=True) as draw:
            position_height = 15
            for sensor in self.all_sensors:
                health = self._health[sensor.name]
                text = sensor_text(sensor, health.ok)
                draw.text(
                    (5, position_height),
                    text,
                    fill="white",
                    font=font,
                )
                position_height += 15

    def _record_read(self, sensor, now):
        """Read one sensor. A stale DHT sample counts as a failed read."""
        health = self._health[sensor.name]
        try:
            sensor.read()
        except Exception as error:
            # A failed read is "unknown", not the previous sample.
            health.record_failure(error, now)
            return
        sample_problem = getattr(sensor, "sample_problem", None)
        problem = sample_problem() if sample_problem else None
        if problem is not None:
            health.record_failure(problem, now)
            return
        health.record_success(now)

    def _ha_states(self):
        """Sensor entities that read successfully, plus machine, shot and tank."""
        states = []
        for sensor in self.all_sensors:
            if not self._health[sensor.name].ok:
                continue
            if hasattr(sensor, "ha_states"):
                states.extend(sensor.ha_states())
        if self.classifier is not None and self.tank is not None:
            states.extend(
                machine_states(
                    self.classifier.snapshot(),
                    self.tank.snapshot(),
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
