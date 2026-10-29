"""DHT22 (AM2302) temperature and humidity sensor.

The GPIO driver is ``adafruit-circuitpython-dht`` (Blinka, libgpiod on a Pi).
It is imported only when a sensor is constructed without an injected reader,
so the core package and CI do not need it.
"""
import threading
import time
from typing import Callable, Optional

# A sample older than this many poll intervals is not a reading anymore.
STALE_AFTER_POLLS = 3
# Checksum and timeout errors are normal on a DHT22. Log them, but not on
# every failed poll.
ERROR_LOG_INTERVAL = 30.0


def _circuitpython_reader(pin: int):
    """Build a reader around ``adafruit_dht.DHT22``. Imported on first use."""
    try:
        import adafruit_dht
        import board
    except ImportError as error:
        raise ImportError(
            "DHT22 driver not found. Install it with: "
            "pip install 'smart-espresso[dht]'"
        ) from error

    gpio = getattr(board, f"D{pin}", None)
    if gpio is None:
        raise ImportError(
            f"Board pin D{pin} is not available to adafruit-circuitpython-dht"
        )
    # pulseio is unreliable on Pi 4 / Bookworm. The bitbang path uses libgpiod.
    device = adafruit_dht.DHT22(gpio, use_pulseio=False)

    class _BoundRead:
        def __init__(self, device):
            self.device = device

        def __call__(self):
            # temperature's measure() also fills humidity, and a second
            # property access inside the library's own 2 s window does not
            # hit the wire.
            temperature = self.device.temperature
            humidity = self.device.humidity
            return humidity, temperature

    return _BoundRead(device)


class DHT22Sensor:
    """
    DHT22 (AM2302) digital temperature and humidity sensor.

    Sampling bit-bangs GPIO and can block, and the datasheet wants at least
    two seconds between reads. A daemon thread does that work. ``read``
    returns the cached sample and never touches the pin.

    A failed read is logged (at most once every 30 s) and the thread keeps
    polling. A sample older than three poll intervals is dropped: the values
    become ``None``, ``message`` says stale or no data, and Home Assistant
    is not given the old numbers.
    """

    def __init__(
        self,
        pin: int,
        name: str = "DHT22",
        use_fahrenheit: bool = False,
        reader: Optional[Callable[[], tuple]] = None,
        interval: float = 2.0,
        now: Optional[Callable[[], float]] = None,
    ):
        """
        Initialize the DHT22 sensor.

        Args:
            pin: GPIO pin number (BCM numbering, e.g. 4 for GPIO4).
            name: Sensor name for display and Home Assistant.
            use_fahrenheit: Convert Celsius readings to Fahrenheit.
            reader: Callable returning ``(humidity, temperature)``. The
                default constructs an ``adafruit_dht.DHT22`` on ``pin``.
                Tests pass a fake. The callable may raise; the poll thread
                catches that and tries again.
            interval: Seconds between polls. The DHT22 datasheet floor is 2.
            now: Monotonic clock used to age samples. Defaults to
                ``time.monotonic``.
        """
        if interval <= 0:
            raise ValueError(f"interval must be positive (got {interval})")

        self.name = name
        self.pin = pin
        self.use_fahrenheit = use_fahrenheit
        self._interval = interval
        self._now = now or time.monotonic
        self._reader = reader if reader is not None else _circuitpython_reader(pin)
        self._device = getattr(self._reader, "device", None)

        self._temperature: Optional[float] = None
        self._humidity: Optional[float] = None
        self._last_ok: Optional[float] = None
        self._error_count = 0
        self._last_error_log: Optional[float] = None

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name=f"dht22-{name}", daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Poll until stopped. One bad read does not end the loop."""
        while not self._stop_event.is_set():
            try:
                humidity, temperature = self._reader()
            except Exception as error:
                self._log_error(error)
            else:
                self._store(humidity, temperature)
            if self._stop_event.wait(self._interval):
                return

    def _store(self, humidity, temperature) -> None:
        if humidity is None or temperature is None:
            return
        try:
            humidity = float(humidity)
            temperature = float(temperature)
        except (TypeError, ValueError) as error:
            self._log_error(error)
            return
        if self.use_fahrenheit:
            temperature = temperature * 9.0 / 5.0 + 32.0
        with self._lock:
            self._humidity = humidity
            self._temperature = temperature
            self._last_ok = self._now()

    def _log_error(self, error: BaseException) -> None:
        now = self._now()
        with self._lock:
            self._error_count += 1
            count = self._error_count
            last = self._last_error_log
            if last is not None and now - last < ERROR_LOG_INTERVAL:
                return
            self._last_error_log = now
        print(f"{self.name} DHT22 read failed ({count}): {error}")

    def _cached(self):
        """Return ``(humidity, temperature, state)`` with state ok/stale/none."""
        with self._lock:
            if self._last_ok is None:
                return None, None, "none"
            age = self._now() - self._last_ok
            if age > self._interval * STALE_AFTER_POLLS:
                return None, None, "stale"
            return self._humidity, self._temperature, "ok"

    def sample_problem(self):
        """
        Exception when the cache is missing or expired, else None.

        The render loop records this on the sensor's health entry, which
        keeps the value out of Home Assistant and marks ``/api/status``.
        """
        _humidity, _temperature, state = self._cached()
        if state == "ok":
            return None
        if state == "stale":
            return RuntimeError("stale")
        return RuntimeError("no data")

    def read(self) -> tuple[Optional[float], Optional[float]]:
        """
        Return the cached ``(humidity, temperature)``.

        Both are ``None`` when nothing has been read yet, or when the last
        good sample is older than three poll intervals. This does not talk
        to the sensor.
        """
        humidity, temperature, _state = self._cached()
        return humidity, temperature

    def stop(self) -> None:
        """Stop the background polling thread and release the GPIO pin."""
        self._stop_event.set()
        self._poll_thread.join(timeout=max(self._interval, 0.5))
        if self._device is not None:
            try:
                self._device.exit()
            except Exception as error:
                print(f"{self.name} DHT22 close failed: {error}")
            self._device = None

    @property
    def temperature(self) -> Optional[float]:
        """Last temperature, or None when it is missing or stale."""
        _humidity, temperature, _state = self._cached()
        return temperature

    @property
    def humidity(self) -> Optional[float]:
        """Last humidity, or None when it is missing or stale."""
        humidity, _temperature, _state = self._cached()
        return humidity

    @property
    def temperature_unit(self) -> str:
        """Temperature unit symbol."""
        return "°F" if self.use_fahrenheit else "°C"

    def _line_name(self) -> str:
        """User-facing name. The Home Assistant slug stays on ``self.name``."""
        if self.name == "Environment":
            return "Room"
        return self.name

    @property
    def message(self) -> str:
        """One line for the display and the dashboard's other-sensor row."""
        humidity, temperature, state = self._cached()
        label = self._line_name()
        if state == "stale":
            with self._lock:
                age = 0 if self._last_ok is None else self._now() - self._last_ok
            seconds = max(0, int(round(age)))
            if seconds < 60:
                seen = f"{seconds} s ago"
            else:
                seen = f"{max(1, int(round(seconds / 60)))} min ago"
            return f"{label}: Last seen {seen}"
        if state != "ok" or temperature is None or humidity is None:
            return f"{label}: No data"
        temp_str = f"{temperature:.1f}{self.temperature_unit}"
        humidity_str = f"{humidity:.1f}%"
        return f"{label}: {temp_str} {humidity_str}"

    def ha_states(self):
        """Entity states for a fresh sample. Empty when the sample is stale."""
        from smart_espresso.home_assistant import State

        humidity, temperature, _state = self._cached()
        slug = self.name.lower().replace(" ", "_")
        states = []
        if temperature is not None:
            states.append(
                State(
                    entity_id=f"sensor.{slug}_temperature",
                    state=str(round(temperature, 1)),
                    attributes={
                        "unit_of_measurement": self.temperature_unit,
                        "friendly_name": f"{self.name} Temperature",
                        "device_class": "temperature",
                    },
                )
            )
        if humidity is not None:
            states.append(
                State(
                    entity_id=f"sensor.{slug}_humidity",
                    state=str(round(humidity, 1)),
                    attributes={
                        "unit_of_measurement": "%",
                        "friendly_name": f"{self.name} Humidity",
                        "device_class": "humidity",
                    },
                )
            )
        return states

    def update_home_assistant(self, client):
        """Publish the current temperature and humidity."""
        if client is None:
            return
        for state in self.ha_states():
            client.set_state(state)

    def __repr__(self) -> str:
        humidity, temperature, _state = self._cached()
        return (
            f"DHT22Sensor(pin={self.pin}, name='{self.name}', "
            f"temp={temperature}, humidity={humidity})"
        )
