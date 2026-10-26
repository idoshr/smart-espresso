"""Small Flask web dashboard exposing live machine status over the local network."""
import os
import threading
from time import monotonic
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory

from smart_espresso.analog_sensor.pressure_analog_sensor import PressureAnalogSensor
from smart_espresso.analog_sensor.water_flow_sensor import WaterFlowAnalogSensor
from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import ShotHistory
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
    # How many saved shots the status payload carries. Curves stay on
    # GET /api/shots/<id> so the one-second poll stays small.
    RECENT_SHOTS = 12

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        window_seconds: float = WINDOW_SECONDS,
        tank: Optional[WaterTank] = None,
        classifier: Optional[MachineStateClassifier] = None,
        shot_history: Optional[ShotHistory] = None,
    ):
        """
        Initialize the status server.

        Args:
            host: Interface to bind to (0.0.0.0 exposes it on the LAN).
            port: TCP port to listen on.
            window_seconds: Length of the recent-water window, in seconds.
            tank: Water tank counter; a default 2 L tank is created if omitted.
            classifier: Machine state classifier; a default one is created if
                omitted, wired to ``shot_history`` so completed pulls are
                recorded.
            shot_history: Persisted shot log. A default log (under
                ``~/.smart_espresso/``) is created if omitted. Pass the same
                instance the classifier records into when supplying both.
        """
        self.host = host
        self.port = port
        self.window_seconds = window_seconds
        self.tank = tank if tank is not None else WaterTank()
        self.shot_history = shot_history if shot_history is not None else ShotHistory()
        self.classifier = (
            classifier
            if classifier is not None
            else MachineStateClassifier(shot_history=self.shot_history)
        )

        self._lock = threading.Lock()
        self._snapshot: dict = {
            "pressure": [],
            "flow": [],
            "other": [],
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

        # The service worker must be served from the site root to control
        # the whole origin; the icons ride along. Explicit per-file rules
        # keep HTTP semantics for everything else: a GET on the POST-only
        # reset endpoint still answers 405 with Allow: POST instead of
        # falling into a catch-all and 404ing as a missing file.
        def static_rule(filename):
            def view():
                return send_from_directory(STATIC_DIR, filename)

            return view

        for filename in (
            "sw.js",
            "dashboard_logic.js",
            "icon.svg",
            "icon-192.png",
            "icon-512.png",
            "apple-touch-icon.png",
        ):
            self.app.add_url_rule(
                f"/{filename}",
                f"static_{filename.replace('.', '_')}",
                static_rule(filename),
            )

        @self.app.route("/api/status")
        def status():
            return jsonify(self.status_payload())

        @self.app.route("/api/shots")
        def shots():
            """
            Saved shots, newest first, without curve samples.

            Optional ``limit`` keeps the newest N summaries. ``count`` is
            always the number of shots on hand. Read from memory: the log
            was loaded at startup and is appended when a pull finishes.
            """
            limit = _parse_limit(request.args.get("limit"))
            if limit is False:
                return jsonify({"error": "invalid limit"}), 400
            return jsonify(self.shot_history.list_payload(limit=limit))

        @self.app.route("/api/shots/<int:shot_id>")
        def shot_detail(shot_id):
            """One saved shot, including its pressure and flow curves."""
            shot = self.shot_history.get(shot_id)
            if shot is None:
                return jsonify({"error": "shot not found"}), 404
            return jsonify(shot)

        @self.app.route("/api/tank/reset", methods=["POST"])
        def reset_tank():
            """
            Called by the dashboard's Tank filled button after a refill.

            The dashboard sends X-Espresso-Dashboard; a form or image posted by
            some other site a phone happens to be visiting cannot set a custom
            header without a CORS preflight, which this server never grants.
            That keeps a stray page on the network from zeroing the count.
            """
            if request.headers.get("X-Espresso-Dashboard") != "1":
                return jsonify({"error": "missing dashboard header"}), 403

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
                        "sample_hz": _rounded_hz(sensor.sample_hz),
                    }
                )
            else:
                other.append({"name": name, "message": str(sensor.message)})

        # Both keep their own lock, so they are updated outside self._lock:
        # nesting would drag the tank's disk write into every reader's path.
        self.classifier.update(head_bar, boiler_bar, flow_rate_mls, total_ml)
        self.tank.update(total_ml)

        with self._lock:
            self._snapshot = {
                "pressure": pressure,
                "flow": flow,
                "other": other,
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
        # In memory, same as the machine and tank blocks. The curves are
        # not included; a tap on the dashboard fetches those by id.
        snapshot["shots"] = self.shot_history.status_summary(limit=self.RECENT_SHOTS)
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
        edges = [
            window_start + i * bucket_seconds for i in range(self.CHART_BUCKETS + 1)
        ]
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

        recent_ml = round(
            max(cumulative_at_edge[-1] - cumulative_at_edge[0], 0.0) * 1000, 1
        )
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


def _rounded_hz(sample_hz: Optional[float]):
    """One decimal, or None before the sensor has measured a rate."""
    if sample_hz is None:
        return None
    return round(sample_hz, 1)


def _parse_limit(raw: Optional[str]):
    """A non-negative int, None when the query omits it, or False when invalid."""
    if raw is None:
        return None
    try:
        limit = int(raw)
    except ValueError:
        return False
    if limit < 0:
        return False
    return limit
