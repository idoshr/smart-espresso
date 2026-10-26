"""Small Home Assistant REST client.

``homeassistant-api`` 4.1.1 pins pydantic 1.9, which cannot be imported on
Python 3.13 (Raspberry Pi OS Trixie). The monitor only needs to publish
entity states, so this module POSTs them directly and is imported only when
Home Assistant is enabled.
"""

import threading
from time import monotonic

import requests

# (connect, read) seconds. A black-holed HA host must not block forever.
DEFAULT_TIMEOUT = (2.0, 3.0)


class State:
    """One entity state to publish."""

    def __init__(self, entity_id, state, attributes=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = dict(attributes or {})


class Client:
    """POST ``/api/states/<entity_id>`` with a long-lived access token."""

    def __init__(self, api_url, token, verify_ssl=True, timeout=DEFAULT_TIMEOUT):
        if not api_url:
            raise ValueError("api_url is required")
        if not token:
            raise ValueError("token is required")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def set_state(self, state):
        """Publish ``state`` and raise if Home Assistant rejects it."""
        response = self._session.post(
            f"{self.api_url}/states/{state.entity_id}",
            json={"state": state.state, "attributes": state.attributes},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response


def machine_states(machine, tank):
    """Machine state, shot timer and tank entities. Sensors publish themselves."""
    shot = machine.get("shot") or {}
    return [
        State(
            entity_id="sensor.espresso_machine_state",
            state=str(machine["state"]),
            attributes={
                "friendly_name": "Espresso Machine",
                "label": machine.get("label"),
                "severity": machine.get("severity"),
            },
        ),
        State(
            entity_id="sensor.espresso_machine_shot",
            state=str(shot.get("seconds", 0.0)),
            attributes={
                "unit_of_measurement": "s",
                "friendly_name": "Espresso Shot Timer",
                "ml": shot.get("ml", 0.0),
                "active": machine.get("state") == "brewing",
            },
        ),
        State(
            entity_id="sensor.espresso_water_tank",
            state=str(tank["remaining_ml"]),
            attributes={
                "unit_of_measurement": "mL",
                "friendly_name": "Espresso Water Tank",
                "used_ml": tank["used_ml"],
                "percent": tank["percent"],
            },
        ),
    ]


class HomeAssistantPublisher:
    """Publish the newest snapshot on a background thread.

    The slot holds one payload. A newer submit replaces anything not yet
    pushed, so a slow Home Assistant cannot build a backlog. Failures back
    off exponentially and the same thread retries when the client works again.
    """

    def __init__(self, client, min_backoff=1.0, max_backoff=30.0):
        self.client = client
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._lock = threading.Lock()
        self._latest = None
        self._generation = 0
        self._pushed = -1
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._last_log = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ha-publisher", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def submit(self, states):
        """Keep only the latest list of entity states."""
        with self._lock:
            self._generation += 1
            self._latest = (self._generation, list(states))
        self._wake.set()

    def _run(self):
        backoff = self._min_backoff
        while not self._stop.is_set():
            with self._lock:
                item = self._latest
            if item is None or item[0] == self._pushed:
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            try:
                for state in item[1]:
                    self.client.set_state(state)
            except Exception as error:
                self._log_failure(error)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2.0, self._max_backoff)
                continue
            self._pushed = item[0]
            backoff = self._min_backoff

    def _log_failure(self, error):
        now = monotonic()
        if self._last_log is not None and now - self._last_log < 30.0:
            return
        self._last_log = now
        print(f"Home Assistant publish failed: {error}")
