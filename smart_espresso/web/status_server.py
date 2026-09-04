"""Small Flask web dashboard exposing live machine status over the local network."""
import os
import threading
from time import monotonic
from typing import Optional

from flask import Flask, jsonify, send_from_directory

from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.water_tank import WaterTank

PATH = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = f"{PATH}/static"


class StatusServer:
    """
    Serves a mobile-oriented status page and a JSON API for the espresso machine.

    The render loop calls :meth:`update` on every tick with the live sensors;
    the values are copied into a lock-protected snapshot so HTTP requests never
    touch the sensors (and therefore never trigger I2C/SPI traffic) from the
    Flask worker threads.

    Flow sensors additionally get a rolling history of cumulative volume,
    which is what makes "water delivered in the last N seconds" answerable
    without the sensor itself having to keep a window.
    """

    # Rolling window used for the "recent water" figure and the bar chart.
    WINDOW_SECONDS = 120.0
    # Number of bars in the chart; 40 bars over 120s = one bar per 3 seconds.
    CHART_BUCKETS = 40
    # A little slack beyond the window so a bucket edge always has a sample
    # older than it to interpolate from.
    HISTORY_SLACK_SECONDS = 10.0

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        window_seconds: float = WINDOW_SECONDS,
        tank: Optional[WaterTank] = None,
        classifier: Optional[MachineStateClassifier] = None,
    ):
        """
        Initialize the status server.

        Args:
            host: Interface to bind to (0.0.0.0 exposes it on the LAN).
            port: TCP port to listen on.
            window_seconds: Length of the recent-water window, in seconds.
            tank: Water tank counter; a default 2 L tank is created if omitted.
            classifier: Machine state classifier; a default one is created if
                omitted.
        """
        self.host = host
        self.port = port
        self.window_seconds = window_seconds
        self.tank = tank if tank is not None else WaterTank()
        self.classifier = (
            classifier if classifier is not None else MachineStateClassifier()
        )

        self._lock = threading.Lock()
        self._snapshot: dict = {
            "pressure": [],
            "flow": [],
            "other": [],
            "machine": self.classifier.snapshot(),
            "tank": self.tank.snapshot(),
            "uptime": 0.0,
        }
        # name -> list[(monotonic_time, cumulative_liters)], oldest first
        self._flow_history: dict[str, list[tuple[float, float]]] = {}
        self._started_at = monotonic()
        self._thread: Optional[threading.Thread] = None

        self.app = Flask(__name__, static_folder=None)
        self._register_routes()

    def _register_routes(self) -> None:
        @self.app.route("/")
        def index():
            return send_from_directory(STATIC_DIR, "index.html")

        @self.app.route("/manifest.webmanifest")
        def manifest():
            return send_from_directory(STATIC_DIR, "manifest.webmanifest")

        @self.app.route("/api/status")
        def status():
            return jsonify(self.status_payload())

        @self.app.route("/api/tank/reset", methods=["POST"])
        def reset_tank():
            """Called by the dashboard's Filled button after a refill."""
            self.tank.reset()
            return jsonify(self.tank.snapshot())

    def update(self, sensors: list) -> None:
        """
        Copy the current sensor readings into the snapshot served over HTTP.

        Called once per render-loop tick, right after the sensors were read.
        Also feeds the state classifier and the tank counter, so both advance
        at the sampling rate rather than only when someone opens the page.
        """
        now = monotonic()
        pressure = []
        flow = []
        other = []
        head_bar = None
        boiler_bar = None
        total_ml = 0.0
        flow_rate_mls = 0.0

        for sensor in sensors:
            name = getattr(sensor, "name", "sensor")
            if isinstance(sensor, PressureAnalogSensor):
                bar = sensor.bar
                pressure.append(
                    {
                        "name": name,
                        "bar": round(bar, 2),
                        "max_bar": round(sensor.max_pressure_mpa * 10, 1),
                    }
                )
                if "boiler" in name.lower() or "steam" in name.lower():
                    boiler_bar = bar
                elif head_bar is None:
                    head_bar = bar
            elif isinstance(sensor, WaterFlowAnalogSensor):
                liters = sensor.liter
                self._record_flow(name, now, liters)
                sensor_ml = liters * 1000
                sensor_mls = sensor.flow_rate * 1000 / 60
                total_ml += sensor_ml
                flow_rate_mls += sensor_mls
                flow.append(
                    {
                        "name": name,
                        "total_ml": round(sensor_ml, 1),
                        "flow_rate_lpm": round(sensor.flow_rate, 3),
                        "flow_rate_mls": round(sensor_mls, 1),
                    }
                )
            else:
                other.append({"name": name, "message": str(sensor.message)})

        self.classifier.update(head_bar, boiler_bar, flow_rate_mls, total_ml)
        self.tank.update(total_ml)

        with self._lock:
            self._snapshot = {
                "pressure": pressure,
                "flow": flow,
                "other": other,
                "machine": self.classifier.snapshot(),
                "tank": self.tank.snapshot(),
                "uptime": round(now - self._started_at, 1),
            }

    def _record_flow(self, name: str, now: float, liters: float) -> None:
        """Append a cumulative-volume sample and drop samples older than the window."""
        with self._lock:
            history = self._flow_history.setdefault(name, [])
            history.append((now, liters))
            cutoff = now - (self.window_seconds + self.HISTORY_SLACK_SECONDS)
            # Samples are appended in time order, so trimming from the front
            # is enough; find the first index still inside the window.
            keep_from = 0
            for index, (sample_time, _) in enumerate(history):
                if sample_time >= cutoff:
                    keep_from = index
                    break
            else:
                keep_from = len(history) - 1
            if keep_from > 0:
                del history[:keep_from]

    def status_payload(self) -> dict:
        """Build the JSON payload for /api/status, including the flow chart buckets."""
        now = monotonic()
        with self._lock:
            snapshot = {
                "pressure": list(self._snapshot["pressure"]),
                "other": list(self._snapshot["other"]),
                "uptime": self._snapshot["uptime"],
            }
            flow_entries = [dict(entry) for entry in self._snapshot["flow"]]
            histories = {
                name: list(samples) for name, samples in self._flow_history.items()
            }

        for entry in flow_entries:
            samples = histories.get(entry["name"], [])
            recent_ml, buckets = self._window_stats(samples, now)
            entry["window_seconds"] = self.window_seconds
            entry["recent_ml"] = recent_ml
            entry["chart"] = buckets

        snapshot["flow"] = flow_entries
        snapshot["window_seconds"] = self.window_seconds
        # Read these live rather than from the cached snapshot: the state's
        # elapsed time keeps counting between render ticks, and a tank reset
        # must show up in the very next response.
        snapshot["machine"] = self.classifier.snapshot()
        snapshot["tank"] = self.tank.snapshot()
        return snapshot

    def _window_stats(
        self, samples: list[tuple[float, float]], now: float
    ) -> tuple[float, list[float]]:
        """
        Compute millilitres delivered in the window and per-bucket millilitres.

        Returns (recent_ml, buckets) where buckets is oldest-first and has
        CHART_BUCKETS entries, so the client can render a fixed-width chart.
        """
        buckets = [0.0] * self.CHART_BUCKETS
        if not samples:
            return 0.0, buckets

        bucket_seconds = self.window_seconds / self.CHART_BUCKETS
        window_start = now - self.window_seconds

        # Cumulative volume at each bucket edge (CHART_BUCKETS + 1 edges).
        edges = [window_start + i * bucket_seconds for i in range(self.CHART_BUCKETS + 1)]
        cumulative_at_edge = []
        index = 0
        last_value = samples[0][1]
        for edge in edges:
            while index < len(samples) and samples[index][0] <= edge:
                last_value = samples[index][1]
                index += 1
            cumulative_at_edge.append(last_value)

        for i in range(self.CHART_BUCKETS):
            delta = cumulative_at_edge[i + 1] - cumulative_at_edge[i]
            buckets[i] = round(max(delta, 0.0) * 1000, 2)

        recent_ml = round(max(cumulative_at_edge[-1] - cumulative_at_edge[0], 0.0) * 1000, 1)
        return recent_ml, buckets

    def start(self) -> None:
        """Start the HTTP server on a background daemon thread."""
        if self._thread is not None:
            return

        def _serve():
            # use_reloader=False: the reloader forks and would re-run main.py.
            self.app.run(
                host=self.host,
                port=self.port,
                threaded=True,
                use_reloader=False,
                debug=False,
            )

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()
        print(f"Status web server listening on http://{self.host}:{self.port}")
