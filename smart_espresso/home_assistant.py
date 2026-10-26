"""Small Home Assistant REST client.

``homeassistant-api`` 4.1.1 pins pydantic 1.9, which cannot be imported on
Python 3.13 (Raspberry Pi OS Trixie). The monitor only needs to publish
entity states, so this module POSTs them directly and is imported only when
Home Assistant is enabled.
"""

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
