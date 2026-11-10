"""Track how much water was drawn from the tank since it was last filled."""
import math
import os
import threading
from time import monotonic
from typing import Optional

from smart_espresso.json_store import atomic_write_json, load_with_backup

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
    tank. Writes are throttled (``save_interval``) and skipped entirely while
    nothing has changed, so an idle machine does not wear out the SD card.
    The temp file is fsynced before it replaces the destination, the previous
    good file is kept as ``.bak``, and a file that cannot be parsed is renamed
    to ``.corrupt-<time>`` instead of being overwritten.

    The render loop feeds this from its own thread while Flask worker threads
    read snapshots and reset it, so all state changes are guarded by a lock.
    Disk writes happen outside that lock: a slow SD card must not stall a
    dashboard request.
    """

    # Typical espresso volume, used for the "shots left" estimate.
    SHOT_ML = 36.0

    def __init__(
        self,
        capacity_ml: Optional[float] = 2000.0,
        low_fraction: float = 0.15,
        state_path: Optional[str] = DEFAULT_STATE_PATH,
        save_interval: float = 15.0,
    ):
        """
        Initialize the tank counter.

        Args:
            capacity_ml: Tank size in millilitres. None means the machine
                has no capacity yet, so shots-left is not computed. The
                class default is still 2 L for callers that do not use
                machine profiles.
            low_fraction: Fraction of capacity below which the tank reads low.
            state_path: File the baseline is persisted to; None disables
                persistence (used by tests).
            save_interval: Minimum seconds between disk writes.
        """
        self.capacity_ml: Optional[float]
        if capacity_ml is None:
            self.capacity_ml = None
            self.capacity_status = "machine_not_set"
        else:
            self.capacity_ml = float(capacity_ml)
            self.capacity_status = "ok"
        self.low_fraction = low_fraction
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self.save_interval = save_interval

        self._lock = threading.Lock()
        # Held for a whole save (read state, write file, mark it saved) so two
        # savers cannot interleave and let a stale payload land last.
        self._write_lock = threading.Lock()
        self._used_ml = 0.0
        self._baseline_ml: Optional[float] = None
        self._last_total_ml = 0.0
        self._refills = 0
        self._last_save = monotonic()
        # What is currently on disk, so an unchanged counter is not rewritten.
        self._saved_used_ml: Optional[float] = None
        self._saved_refills: Optional[int] = None

        self._load()

    def set_capacity(
        self, capacity_ml: Optional[float], status: Optional[str] = None
    ) -> None:
        """Change the tank size without resetting how much water was drawn.

        None clears the size. Shots-left is not computed until a size is
        set again. ``status`` is the API's reason when the size is absent.
        """
        if capacity_ml is None:
            with self._lock:
                self.capacity_ml = None
                self.capacity_status = status or "machine_not_set"
            return
        if isinstance(capacity_ml, bool) or not isinstance(capacity_ml, (int, float)):
            raise ValueError("capacity_ml must be a number")
        if not math.isfinite(capacity_ml) or capacity_ml <= 0:
            raise ValueError(
                f"capacity_ml must be a positive finite number, got {capacity_ml}"
            )
        with self._lock:
            self.capacity_ml = float(capacity_ml)
            self.capacity_status = "ok"

    def update(self, total_ml: float) -> float:
        """
        Feed the flow sensor's lifetime volume and return millilitres used.

        Args:
            total_ml: Lifetime volume measured by the flow sensor, in ml.
        """
        if not math.isfinite(total_ml):
            print(f"Ignoring non-finite flow total for the water tank: {total_ml}")
            return self._used_ml

        with self._lock:
            if self._baseline_ml is None:
                self._baseline_ml = total_ml

            # The lifetime counter only ever grows; if it went backwards the
            # sensor was reset or the process restarted. Everything it reports
            # from here on is new water, so offset the baseline by what was
            # already counted rather than discarding it.
            if total_ml < self._last_total_ml:
                self._baseline_ml = -self._used_ml

            self._last_total_ml = total_ml
            self._used_ml = max(total_ml - self._baseline_ml, 0.0)
            used = self._used_ml

        self._maybe_save()
        return used

    def reset(self) -> None:
        """Mark the tank as refilled: zero the used volume from now on."""
        with self._lock:
            self._baseline_ml = self._last_total_ml
            self._used_ml = 0.0
            self._refills += 1
        self._save()

    def _remaining_ml(self, used: float) -> Optional[float]:
        capacity = self.capacity_ml
        if capacity is None:
            return None
        return max(capacity - used, 0.0)

    def _percent(self, used: float) -> Optional[float]:
        capacity = self.capacity_ml
        remaining = self._remaining_ml(used)
        if capacity is None or remaining is None:
            return None
        if capacity <= 0:
            return 0.0
        return max(0.0, min(100.0, remaining / capacity * 100.0))

    def _is_low(self, used: float) -> Optional[bool]:
        capacity = self.capacity_ml
        remaining = self._remaining_ml(used)
        if capacity is None or remaining is None:
            return None
        return remaining <= capacity * self.low_fraction

    def _shots_left(self, used: float) -> Optional[int]:
        remaining = self._remaining_ml(used)
        if remaining is None:
            return None
        return int(remaining // self.SHOT_ML)

    @property
    def used_ml(self) -> float:
        return self._used_ml

    @property
    def remaining_ml(self) -> Optional[float]:
        return self._remaining_ml(self._used_ml)

    @property
    def percent(self) -> Optional[float]:
        """Percentage of the tank still available (0-100), or None if unset."""
        return self._percent(self._used_ml)

    @property
    def is_low(self) -> Optional[bool]:
        return self._is_low(self._used_ml)

    @property
    def is_empty(self) -> Optional[bool]:
        remaining = self._remaining_ml(self._used_ml)
        if remaining is None:
            return None
        return remaining <= 0.0

    @property
    def shots_left(self) -> Optional[int]:
        return self._shots_left(self._used_ml)

    @property
    def refills(self) -> int:
        return self._refills

    def snapshot(self) -> dict:
        """
        Serialisable view of the tank for the web API.

        Taken under the lock so a reader can never see a half-updated tank —
        for instance a used volume from before a refill next to a percentage
        from after it.
        """
        with self._lock:
            used = self._used_ml
            refills = self._refills
            capacity = self.capacity_ml
            status = self.capacity_status

        # Everything below is derived from that one reading, so the figures in
        # a payload always agree with each other. No capacity means shots-left
        # is absent: there is no size to divide by.
        if capacity is None:
            return {
                "capacity_ml": None,
                "used_ml": round(used, 1),
                "remaining_ml": None,
                "percent": None,
                "low": None,
                "empty": None,
                "shots_left": None,
                "refills": refills,
                "status": status,
            }
        remaining = max(capacity - used, 0.0)
        percent = (
            0.0 if capacity <= 0 else max(0.0, min(100.0, remaining / capacity * 100.0))
        )
        return {
            "capacity_ml": round(capacity, 1),
            "used_ml": round(used, 1),
            "remaining_ml": round(remaining, 1),
            "percent": round(percent, 1),
            "low": remaining <= capacity * self.low_fraction,
            "empty": remaining <= 0.0,
            "shots_left": int(remaining // self.SHOT_ML),
            "refills": refills,
            "status": status,
        }

    def save(self) -> None:
        """Write the counter immediately, ignoring ``save_interval``.

        Shutdown calls this so the last seconds of a pour are not left only
        in memory. An unchanged counter is not rewritten.
        """
        with self._lock:
            unchanged = (
                self._used_ml == self._saved_used_ml
                and self._refills == self._saved_refills
            )
        if unchanged:
            return
        self._save()

    def _maybe_save(self) -> None:
        """Persist at most every save_interval, and only when something moved."""
        with self._lock:
            unchanged = (
                self._used_ml == self._saved_used_ml
                and self._refills == self._saved_refills
            )
            if unchanged or monotonic() - self._last_save < self.save_interval:
                return
        self._save()

    def _save(self) -> None:
        """
        Persist the counter, writing atomically so a power cut can't corrupt it.

        Savers queue on ``_write_lock`` rather than racing: without it the
        render loop and a refill on a Flask thread can interleave so the older
        payload lands last, and a later power cut restores a count from before
        the refill. The file write still happens outside ``_lock``, so SD card
        latency cannot stall the render loop or a dashboard request.
        """
        with self._write_lock:
            with self._lock:
                self._last_save = monotonic()
                if not self.state_path:
                    return
                used, refills = self._used_ml, self._refills

            payload = {
                "used_ml": round(used, 3),
                "refills": refills,
                "capacity_ml": self.capacity_ml,
            }

            try:
                atomic_write_json(self.state_path, payload)
            except OSError as error:
                print(f"Could not save water tank state to {self.state_path}: {error}")
                return

            with self._lock:
                self._saved_used_ml = used
                self._saved_refills = refills

    def _load(self) -> None:
        """Restore the used volume. A bad file is kept aside; ``.bak`` is next."""
        if not self.state_path:
            return

        def parse(payload):
            if not isinstance(payload, dict):
                raise ValueError(f"expected an object, got {type(payload).__name__}")
            used = float(payload.get("used_ml", 0.0))
            # json accepts NaN and Infinity; either would poison every later
            # calculation, so treat them like any other unreadable file.
            if not math.isfinite(used):
                raise ValueError(f"used_ml is not a finite number: {used}")
            return used, int(payload.get("refills", 0))

        loaded = load_with_backup(self.state_path, parse)
        if loaded is None:
            return
        self._used_ml, self._refills = loaded
        # A value restored only from the backup is not in the main file yet,
        # so the next change has to write it.
        if os.path.exists(self.state_path):
            self._saved_used_ml = self._used_ml
            self._saved_refills = self._refills

        # The flow sensor restarts at zero, so the restored volume becomes the
        # starting offset rather than a baseline against a lifetime total.
        self._baseline_ml = -self._used_ml
