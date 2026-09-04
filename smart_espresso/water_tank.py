"""Track how much water was drawn from the tank since it was last filled."""
import json
import os
import tempfile
from time import monotonic
from typing import Optional

DEFAULT_STATE_PATH = "~/.smart_espresso/water_tank.json"


class WaterTank:
    """
    Counts water drawn from a fixed-size tank so the dashboard can warn before
    it runs dry.

    The flow sensor only knows a lifetime pulse total, so the tank keeps a
    baseline: everything measured since the last refill is what has been used.
    Pressing "Filled" on the dashboard calls :meth:`reset`, which moves the
    baseline to the current lifetime total.

    The baseline is persisted to disk, because the counter is only useful if it
    survives a reboot of the Pi — otherwise every restart would claim a full
    tank. Writes are throttled (``save_interval``) so a 10 Hz render loop does
    not wear out the SD card.
    """

    # Typical espresso volume, used for the "shots left" estimate.
    SHOT_ML = 36.0

    def __init__(
        self,
        capacity_ml: float = 2000.0,
        low_fraction: float = 0.15,
        state_path: Optional[str] = DEFAULT_STATE_PATH,
        save_interval: float = 15.0,
    ):
        """
        Initialize the tank counter.

        Args:
            capacity_ml: Tank size in millilitres (2 L tank by default).
            low_fraction: Fraction of capacity below which the tank reads low.
            state_path: File the baseline is persisted to; None disables
                persistence (used by tests).
            save_interval: Minimum seconds between disk writes.
        """
        self.capacity_ml = capacity_ml
        self.low_fraction = low_fraction
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self.save_interval = save_interval

        self._used_ml = 0.0
        self._baseline_ml: Optional[float] = None
        self._last_total_ml = 0.0
        self._refills = 0
        self._last_save = monotonic()

        self._load()

    def update(self, total_ml: float) -> float:
        """
        Feed the flow sensor's lifetime volume and return millilitres used.

        Args:
            total_ml: Lifetime volume measured by the flow sensor, in ml.
        """
        if self._baseline_ml is None:
            self._baseline_ml = total_ml

        # The lifetime counter only ever grows; if it went backwards the
        # sensor was reset or the process restarted. Everything it reports from
        # here on is new water, so offset the baseline by what was already
        # counted rather than discarding it.
        if total_ml < self._last_total_ml:
            self._baseline_ml = -self._used_ml

        self._last_total_ml = total_ml
        self._used_ml = max(total_ml - self._baseline_ml, 0.0)
        self._maybe_save()
        return self._used_ml

    def reset(self) -> None:
        """Mark the tank as refilled: zero the used volume from now on."""
        self._baseline_ml = self._last_total_ml
        self._used_ml = 0.0
        self._refills += 1
        self._save()

    @property
    def used_ml(self) -> float:
        return self._used_ml

    @property
    def remaining_ml(self) -> float:
        return max(self.capacity_ml - self._used_ml, 0.0)

    @property
    def percent(self) -> float:
        """Percentage of the tank still available (0-100)."""
        if self.capacity_ml <= 0:
            return 0.0
        return max(0.0, min(100.0, self.remaining_ml / self.capacity_ml * 100.0))

    @property
    def is_low(self) -> bool:
        return self.remaining_ml <= self.capacity_ml * self.low_fraction

    @property
    def is_empty(self) -> bool:
        return self.remaining_ml <= 0.0

    @property
    def shots_left(self) -> int:
        return int(self.remaining_ml // self.SHOT_ML)

    @property
    def refills(self) -> int:
        return self._refills

    def snapshot(self) -> dict:
        """Serialisable view of the tank for the web API."""
        return {
            "capacity_ml": round(self.capacity_ml, 1),
            "used_ml": round(self._used_ml, 1),
            "remaining_ml": round(self.remaining_ml, 1),
            "percent": round(self.percent, 1),
            "low": self.is_low,
            "empty": self.is_empty,
            "shots_left": self.shots_left,
            "refills": self._refills,
        }

    def _maybe_save(self) -> None:
        now = monotonic()
        if now - self._last_save >= self.save_interval:
            self._save()

    def _save(self) -> None:
        """Persist the counter, writing atomically so a power cut can't corrupt it."""
        self._last_save = monotonic()
        if not self.state_path:
            return

        payload = {
            "used_ml": round(self._used_ml, 3),
            "refills": self._refills,
            "capacity_ml": self.capacity_ml,
        }
        try:
            directory = os.path.dirname(self.state_path) or "."
            os.makedirs(directory, exist_ok=True)
            handle, temp_path = tempfile.mkstemp(dir=directory)
            with os.fdopen(handle, "w") as file:
                json.dump(payload, file)
            os.replace(temp_path, self.state_path)
        except OSError as error:
            print(f"Could not save water tank state to {self.state_path}: {error}")

    def _load(self) -> None:
        """Restore the used volume from disk; a missing or bad file starts fresh."""
        if not self.state_path or not os.path.exists(self.state_path):
            return

        try:
            with open(self.state_path) as file:
                payload = json.load(file)
            self._used_ml = float(payload.get("used_ml", 0.0))
            self._refills = int(payload.get("refills", 0))
        except (OSError, ValueError, TypeError) as error:
            print(f"Ignoring unreadable water tank state {self.state_path}: {error}")
            return

        # The flow sensor restarts at zero, so the restored volume becomes the
        # starting offset rather than a baseline against a lifetime total.
        self._baseline_ml = -self._used_ml
