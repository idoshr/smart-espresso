"""Persisted log of completed espresso shots.

Each record is the whole of one pull: when it started, how long water was
flowing, the volume, the headline peaks, and the head-pressure, boiler and
flow curves. A later shot-card image can be drawn from that single record;
this module only stores and serves it.

Curves are sampled while the pull is open and written once, when it is
committed. The file write runs on a background thread: serialising a full
log is too slow to sit inside ``classifier.update``. The temp file and the
directory are fsynced, and the previous good file is kept as ``.bak``. An
unreadable file is renamed to ``.corrupt-<time>`` and is not overwritten.
The log is capped (the oldest shots fall off) and restored on boot.

The render loop records from its own thread while Flask workers read
summaries, so mutations and reads share a lock. The disk writer takes its
own snapshot and does not hold that lock across the card.
"""
import atexit
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from smart_espresso.json_store import atomic_write_json, load_with_backup

DEFAULT_STATE_PATH = "~/.smart_espresso/shot_history.json"

# One file shape. Bump only when a reader would misread an older file.
FILE_VERSION = 1


def _finite_float(value) -> Optional[float]:
    """Return value as a finite float, or None when it is not one.

    Booleans are rejected: ``float(True)`` is ``1.0``, and a JSON ``true`` in
    a numeric field would otherwise sneak through as a reading.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _required_float(value, name: str) -> float:
    number = _finite_float(value)
    if number is None:
        raise ValueError(f"{name} is not a finite number: {value!r}")
    return number


def _optional_round(value, places: int = 2) -> Optional[float]:
    number = _finite_float(value)
    if number is None:
        return None
    return round(number, places)


def _parse_sample(raw) -> Optional[dict]:
    """One curve point, or None when the point cannot be read."""
    if not isinstance(raw, dict):
        return None
    elapsed = _finite_float(raw.get("t"))
    if elapsed is None or elapsed < 0:
        return None
    return {
        "t": round(elapsed, 2),
        "head_bar": _optional_round(raw.get("head_bar")),
        "boiler_bar": _optional_round(raw.get("boiler_bar")),
        "flow_mls": _optional_round(raw.get("flow_mls")),
    }


class _OpenShot:
    """Curve buffer for the pull currently in progress. Not persisted."""

    def __init__(self, started_at: str):
        self.started_at = started_at
        self.samples: list[dict] = []
        self.latest: Optional[dict] = None
        self.peak_head: Optional[float] = None
        self.peak_boiler: Optional[float] = None
        self.peak_flow: Optional[float] = None
        self._last_elapsed: Optional[float] = None
        self._last_stored_elapsed: Optional[float] = None

    def note(
        self,
        elapsed: float,
        head_bar: Optional[float],
        boiler_bar: Optional[float],
        flow_mls: Optional[float],
        interval: float,
        max_samples: int,
    ) -> None:
        """Remember one raw reading, storing a curve point at ``interval``."""
        elapsed_value = _finite_float(elapsed)
        if elapsed_value is None:
            return
        if elapsed_value < 0:
            elapsed_value = 0.0
        # A clock step backwards is a glitch, not a new point on the curve.
        if self._last_elapsed is not None and elapsed_value + 1e-6 < self._last_elapsed:
            return
        self._last_elapsed = elapsed_value

        head = _finite_float(head_bar)
        boiler = _finite_float(boiler_bar)
        flow = _finite_float(flow_mls)
        if head is not None:
            self.peak_head = (
                head if self.peak_head is None else max(self.peak_head, head)
            )
        if boiler is not None:
            self.peak_boiler = (
                boiler if self.peak_boiler is None else max(self.peak_boiler, boiler)
            )
        if flow is not None:
            self.peak_flow = (
                flow if self.peak_flow is None else max(self.peak_flow, flow)
            )

        point = {
            "t": round(elapsed_value, 2),
            "head_bar": None if head is None else round(head, 2),
            "boiler_bar": None if boiler is None else round(boiler, 2),
            "flow_mls": None if flow is None else round(flow, 2),
        }
        self.latest = point

        due = (
            self._last_stored_elapsed is None
            or elapsed_value - self._last_stored_elapsed >= interval - 1e-9
        )
        if not due or len(self.samples) >= max_samples:
            return
        if self.samples and self.samples[-1]["t"] == point["t"]:
            return
        self.samples.append(point)
        self._last_stored_elapsed = elapsed_value

    def finish(self, shot_id: int, seconds: float, ml: float) -> dict:
        """Close the buffer into a shot record, keeping the final reading."""
        if self.latest is not None and (
            not self.samples or self.samples[-1]["t"] != self.latest["t"]
        ):
            self.samples.append(self.latest)
        return {
            "id": shot_id,
            "started_at": self.started_at,
            "seconds": round(seconds, 1),
            "ml": round(ml, 1),
            "peak_head_bar": _optional_round(self.peak_head),
            "peak_boiler_bar": _optional_round(self.peak_boiler),
            "peak_flow_mls": _optional_round(self.peak_flow),
            "samples": self.samples,
        }


class ShotHistory:
    """
    Capped, durable log of completed shots.

    A shot is opened when a pull starts, sampled for as long as it runs, and
    either committed (it counted as a shot) or abandoned (a short blip). Only
    a commit touches the disk. The caller decides what counts as a shot; this
    class does not second-guess the 2 s / volume rule.

    ``state_path`` is expanded with ``~`` and may be None to keep the log in
    memory only, which is what the tests do. ``max_shots`` is the cap, oldest
    first to fall off. ``sample_interval`` is the curve resolution in seconds
    of elapsed pull time; peaks are tracked from every raw reading so a spike
    between two stored points is not lost. ``max_samples`` bounds one curve
    so a forgotten pump cannot grow the file without limit; the final reading
    is kept even when that bound has been hit.
    """

    MAX_SHOTS = 200
    SAMPLE_INTERVAL = 0.25
    MAX_SAMPLES = 480

    def __init__(
        self,
        state_path: Optional[str] = DEFAULT_STATE_PATH,
        max_shots: int = MAX_SHOTS,
        sample_interval: float = SAMPLE_INTERVAL,
        max_samples: int = MAX_SAMPLES,
        now: Optional[Callable[[], float]] = None,
    ):
        """
        Initialize the log and restore whatever was saved last time.

        Args:
            state_path: File the log is persisted to. None disables
                persistence. A leading ``~`` is expanded.
            max_shots: How many completed shots to keep. Oldest are dropped.
            sample_interval: Seconds between stored curve points.
            max_samples: Upper bound on stored points in one curve.
            now: Clock returning a Unix timestamp for the shot's start.
                Defaults to ``time.time``.
        """
        if max_shots < 1:
            raise ValueError(f"max_shots must be at least 1 (got {max_shots})")
        if sample_interval <= 0:
            raise ValueError(
                f"sample_interval must be positive (got {sample_interval})"
            )
        if max_samples < 1:
            raise ValueError(f"max_samples must be at least 1 (got {max_samples})")

        self.state_path = os.path.expanduser(state_path) if state_path else None
        self.max_shots = max_shots
        self.sample_interval = sample_interval
        self.max_samples = max_samples
        self._now = now or _wall_time

        self._lock = threading.Lock()
        self._shots: list[dict] = []
        self._next_id = 1
        self._active: Optional[_OpenShot] = None
        self._cv = threading.Condition()
        self._pending = None
        self._generation = 0
        self._written = 0
        self._stop = False
        self._writer = None

        self._load()
        if self.state_path:
            self._writer = threading.Thread(
                target=self._write_loop,
                name="shot-history",
                daemon=True,
            )
            self._writer.start()
            atexit.register(self._flush_on_exit)

    def open(self) -> None:
        """Start a new pull. Replaces any pull that was left open."""
        with self._lock:
            self._active = _OpenShot(self._stamp())

    def sample(
        self,
        elapsed: float,
        head_bar: Optional[float],
        boiler_bar: Optional[float],
        flow_mls: Optional[float],
    ) -> None:
        """
        Feed one reading of the open pull.

        ``elapsed`` is seconds since the pull started (wall time on the
        machine's clock, pauses included). Readings before :meth:`open` are
        ignored. Nothing is written to disk here.
        """
        with self._lock:
            if self._active is None:
                return
            self._active.note(
                elapsed,
                head_bar,
                boiler_bar,
                flow_mls,
                self.sample_interval,
                self.max_samples,
            )

    def commit(self, seconds: float, ml: float, wait: bool = True) -> Optional[dict]:
        """
        Keep the open pull and persist the log.

        Returns the stored record, or None when nothing was open or the
        totals could not be read. ``seconds`` is the flowing time (pauses
        excluded) and ``ml`` the volume, both as the classifier measured them.

        The file write runs on the background writer. ``wait`` blocks until
        that snapshot is on disk (or the attempt has failed and the previous
        file is still in place). The render loop passes ``wait=False`` so a
        large log cannot stall ``classifier.update``.
        """
        with self._lock:
            active = self._active
            self._active = None
            if active is None:
                return None
            try:
                seconds_value = _required_float(seconds, "seconds")
                ml_value = _required_float(ml, "ml")
            except ValueError as error:
                print(f"Dropping shot with unreadable totals: {error}")
                return None
            if seconds_value < 0 or ml_value < 0:
                print(
                    "Dropping shot with negative totals: "
                    f"seconds={seconds_value}, ml={ml_value}"
                )
                return None
            shot = active.finish(self._next_id, seconds_value, ml_value)
            self._next_id += 1
            self._shots.append(shot)
            if len(self._shots) > self.max_shots:
                del self._shots[: len(self._shots) - self.max_shots]
            stored = _public_shot(shot, with_samples=True)
            payload = {
                "version": FILE_VERSION,
                "next_id": self._next_id,
                "shots": list(self._shots),
            }
        if self.state_path:
            self._enqueue(payload)
            if wait:
                self.flush()
        return stored

    def abandon(self) -> None:
        """Drop the open pull without writing. Used for blips that are not shots."""
        with self._lock:
            self._active = None

    def get(self, shot_id: int) -> Optional[dict]:
        """The full record for one shot, or None when that id is not kept."""
        with self._lock:
            for shot in self._shots:
                if shot["id"] == shot_id:
                    return _public_shot(shot, with_samples=True)
        return None

    def list_payload(self, limit: Optional[int] = None) -> dict:
        """
        Summary list for ``GET /api/shots``.

        ``count`` is how many shots are kept, not how many this response
        carries. Summaries are newest first and omit the curves. ``limit``
        of None returns every summary; ``0`` returns none.
        """
        with self._lock:
            chosen = _newest(self._shots, limit)
            return {
                "count": len(self._shots),
                "shots": [_public_shot(shot, with_samples=False) for shot in chosen],
            }

    def status_summary(self, limit: int = 12) -> dict:
        """Small newest-first summary embedded in ``GET /api/status``."""
        with self._lock:
            chosen = _newest(self._shots, limit)
            return {
                "count": len(self._shots),
                "recent": [_public_shot(shot, with_samples=False) for shot in chosen],
            }

    def _stamp(self) -> str:
        moment = datetime.fromtimestamp(self._now(), tz=timezone.utc).astimezone()
        return moment.isoformat(timespec="seconds")

    def _flush_on_exit(self) -> None:
        """Best-effort flush when the process exits. Never raises."""
        try:
            self.flush()
        except Exception as error:
            print(f"Shot history was not flushed on exit: {error}")

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the newest queued snapshot has been written."""
        if not self.state_path:
            return
        deadline = time.monotonic() + timeout
        with self._cv:
            target = self._generation
            while self._written < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("shot history write did not finish")
                self._cv.wait(remaining)

    def close(self) -> None:
        """Flush and stop the writer. Used by tests that delete the directory."""
        self.flush()
        if self._writer is None:
            return
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self._writer.join(timeout=2.0)
        self._writer = None

    def _enqueue(self, payload: dict) -> None:
        with self._cv:
            self._generation += 1
            self._pending = payload
            self._cv.notify()

    def _write_loop(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait()
                if self._pending is None and self._stop:
                    return
                payload = self._pending
                generation = self._generation
                self._pending = None
            try:
                atomic_write_json(self.state_path, payload)
            except OSError as error:
                # The previous file is still in place. A later commit retries.
                print(f"Could not save shot history to {self.state_path}: {error}")
            with self._cv:
                self._written = generation
                self._cv.notify_all()

    def _load(self) -> None:
        """Restore the log. A bad file is kept aside; ``.bak`` is the fallback."""
        if not self.state_path:
            return

        def parse(payload):
            return self._parse_payload(payload)

        loaded = load_with_backup(self.state_path, parse)
        if loaded is None:
            return
        shots, next_id = loaded
        self._shots = shots[-self.max_shots :]
        highest = max((shot["id"] for shot in self._shots), default=0)
        self._next_id = max(next_id, highest + 1)

    def _parse_payload(self, payload) -> tuple[list[dict], int]:
        if not isinstance(payload, dict):
            raise ValueError(f"expected an object, got {type(payload).__name__}")
        raw_shots = payload.get("shots", [])
        if not isinstance(raw_shots, list):
            raise ValueError(f"shots is not a list: {type(raw_shots).__name__}")

        shots = []
        seen: set[int] = set()
        for raw in raw_shots:
            shot = self._parse_shot(raw)
            if shot is None or shot["id"] in seen:
                continue
            seen.add(shot["id"])
            shots.append(shot)

        next_id = payload.get("next_id", 1)
        if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
            next_id = 1
        return shots, next_id

    def _parse_shot(self, raw) -> Optional[dict]:
        try:
            return _shot_from_disk(raw)
        except (TypeError, ValueError) as error:
            print(f"Ignoring unreadable shot in {self.state_path}: {error}")
            return None


def _wall_time() -> float:
    # Imported lazily so tests can pass a clock without importing time here
    # at class-definition time. time.time is the production clock.
    from time import time

    return time()


def _shot_from_disk(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"expected an object, got {type(raw).__name__}")

    shot_id = raw.get("id")
    if isinstance(shot_id, bool) or not isinstance(shot_id, int) or shot_id < 1:
        raise ValueError(f"bad id: {shot_id!r}")

    started_at = raw.get("started_at")
    if not isinstance(started_at, str) or not started_at.strip():
        raise ValueError("missing started_at")

    seconds = _required_float(raw.get("seconds"), "seconds")
    ml = _required_float(raw.get("ml"), "ml")
    if seconds < 0 or ml < 0:
        raise ValueError(f"negative duration or volume: seconds={seconds}, ml={ml}")

    samples_raw = raw.get("samples", [])
    if not isinstance(samples_raw, list):
        raise ValueError("samples is not a list")
    samples = []
    for item in samples_raw:
        parsed = _parse_sample(item)
        if parsed is not None:
            samples.append(parsed)

    return {
        "id": shot_id,
        "started_at": started_at,
        "seconds": round(seconds, 1),
        "ml": round(ml, 1),
        "peak_head_bar": _peak(raw, "peak_head_bar", "head_bar", samples),
        "peak_boiler_bar": _peak(raw, "peak_boiler_bar", "boiler_bar", samples),
        "peak_flow_mls": _peak(raw, "peak_flow_mls", "flow_mls", samples),
        "samples": samples,
    }


def _peak(raw: dict, key: str, sample_key: str, samples: list[dict]) -> Optional[float]:
    """Use the stored peak, or derive one from the curve when it was omitted."""
    if key in raw:
        return _optional_round(raw.get(key))
    values = [
        sample[sample_key] for sample in samples if sample[sample_key] is not None
    ]
    if not values:
        return None
    return round(max(values), 2)


def _public_shot(shot: dict, with_samples: bool) -> dict:
    """A copy safe to hand to a request thread."""
    summary = {
        "id": shot["id"],
        "started_at": shot["started_at"],
        "seconds": shot["seconds"],
        "ml": shot["ml"],
        "peak_head_bar": shot["peak_head_bar"],
        "peak_boiler_bar": shot["peak_boiler_bar"],
        "peak_flow_mls": shot["peak_flow_mls"],
    }
    if with_samples:
        summary["samples"] = [dict(point) for point in shot["samples"]]
    return summary


def _newest(shots: list[dict], limit: Optional[int]) -> list[dict]:
    """Newest first. ``limit`` None keeps all; 0 keeps none.

    ``shots[-0:]`` is the whole list in Python, so a zero limit is handled
    on its own.
    """
    if limit is None:
        chosen = shots
    elif limit <= 0:
        chosen = []
    else:
        chosen = shots[-limit:]
    return list(reversed(chosen))
