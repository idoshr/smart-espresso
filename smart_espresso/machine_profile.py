"""Switchable machine profiles: tank size, one pressure target, and a colour id.

``machine_profile`` is the saved choice. A fresh install with no tank
capacity configured is ``Machine not set``: the capacity is null and
shots-left is not computed. An install that already had a tank size
(``WATER_TANK_ML`` or a saved tank file) is migrated once onto ``custom``
with that capacity, so shots-left keeps working, and a one-time
``prompt_pick_model`` flag asks the user to pick a model. Dismissing the
prompt persists. Migration is not a model switch: used water stays and
no refill is required.

A profile carries a tank capacity and a brew pressure target. The target
is 9 bar when the profile does not set one. Shot time (25 to 30 s) and
millilitres per cup (36 ml) are global and do not change when the model
changes.

Built-in models ship a tank size only when a maker spec page is cited.
A built-in with no spec URL is unconfirmed and carries no capacity; the
user enters theirs. ``custom`` is the unlisted machine.

Theme data is an accent colour id. This module does not carry logos,
trademark artwork, or model illustrations.

Switching to a different model sets ``refill_required``. Used water is
kept, the tank status becomes ``refill_needed``, and shots-left is
hidden until ``POST /api/machine-profile/refilled``. That call zeros
used water and clears the flag. Shots are not blocked while the flag
is set. A switch requested during an open shot waits until that shot
is saved, and the refill flag is set then.
"""

import math
import os
import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from smart_espresso.json_store import atomic_write_json, load_with_backup

DEFAULT_STATE_PATH = "~/.smart_espresso/machine_profile.json"

GENERIC_ID = "generic"
CUSTOM_ID = "custom"
UNSET_LABEL = "Machine not set"

# The tank size the app used before profiles existed. Used only when an
# existing tank file has no ``capacity_ml`` of its own, because that run
# was computing shots-left from this figure. Not a vendor spec.
LEGACY_DEFAULT_TANK_ML = 2000.0

# Home reservoirs sit around half a litre to a few litres. Outside this
# the figure is a typo, not a machine.
TANK_ML_MIN = 250.0
TANK_ML_MAX = 5000.0

# Brew pressure target used when a profile does not set its own.
DEFAULT_PRESSURE_TARGET_BAR = 9.0
PRESSURE_TARGET_MIN_BAR = 1.0
PRESSURE_TARGET_MAX_BAR = 15.0

# Global shot settings. They are not stored on a profile and a model
# switch must not change them. 36 ml matches WaterTank.SHOT_ML.
SHOT_TIME_LOW_S = 25.0
SHOT_TIME_HIGH_S = 30.0
ML_PER_CUP = 36.0

# Gaggia, "Capacity Water Tank 2,1 lt":
# https://www.gaggia.com/manual-machines/new-classic-e24/
GAGGIA_CLASSIC_E24_SOURCE = "https://www.gaggia.com/manual-machines/new-classic-e24/"
# Rancilio, "Built-in 2-litre water tank":
# https://www.ranciliogroup.com/rancilio/silvia/silvia/
RANCILIO_SILVIA_SOURCE = "https://www.ranciliogroup.com/rancilio/silvia/silvia/"

_ACCENT = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PROFILE_FIELDS = ("tank_capacity_ml", "pressure_target_bar", "accent")


def global_settings() -> Dict[str, float]:
    """Shot time and millilitres per cup. The same values for every machine."""
    return {
        "shot_time_low_s": SHOT_TIME_LOW_S,
        "shot_time_high_s": SHOT_TIME_HIGH_S,
        "ml_per_cup": ML_PER_CUP,
    }


class ProfileError(ValueError):
    """A profile id or a custom value the API can show to the user."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MachineProfile:
    """One machine model, or the custom bucket for an unlisted machine."""

    id: str
    vendor: str
    model: str
    tank_capacity_ml: Optional[float]
    pressure_target_bar: float
    accent: str
    source_url: Optional[str]
    custom: bool
    # True when no maker spec was cited. The tank size stays empty until
    # the user enters one. Never filled with a placeholder.
    unconfirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        confirmed = (
            not self.custom
            and not self.unconfirmed
            and self.source_url is not None
            and self.tank_capacity_ml is not None
        )
        return {
            "id": self.id,
            "vendor": self.vendor,
            "model": self.model,
            "custom": self.custom,
            "unconfirmed": self.unconfirmed,
            "capacity_confirmed": confirmed,
            "tank_capacity_ml": self.tank_capacity_ml,
            "pressure_target_bar": self.pressure_target_bar,
            "theme": {"accent": self.accent},
            "source_url": self.source_url,
        }


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError("invalid_profile", f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ProfileError("invalid_profile", f"{name} must be a finite number")
    return number


def validate_tank(value: Any) -> float:
    """Accept a tank size inside the home-machine range, in millilitres."""
    if isinstance(value, str):
        stripped = value.strip()
        try:
            number = float(stripped) if stripped else float("nan")
        except ValueError as error:
            raise ProfileError(
                "invalid_profile", "tank_capacity_ml must be a number"
            ) from error
        if not math.isfinite(number):
            raise ProfileError(
                "invalid_profile", "tank_capacity_ml must be a finite number"
            )
    else:
        number = _finite("tank_capacity_ml", value)
    if number < TANK_ML_MIN or number > TANK_ML_MAX:
        raise ProfileError(
            "invalid_profile",
            f"tank_capacity_ml must be between {TANK_ML_MIN:g} and {TANK_ML_MAX:g} ml",
        )
    return number


def validate_accent(value: Any) -> str:
    """A colour token id. Not a hex colour, a URL, or an image name."""
    if not isinstance(value, str) or _ACCENT.fullmatch(value) is None:
        raise ProfileError(
            "invalid_profile",
            "accent must be a short colour id (letters, digits, hyphens)",
        )
    return value


def validate_pressure_target(value: Any) -> float:
    """Accept one brew pressure target in bar. Omitted profiles use 9."""
    if isinstance(value, str):
        stripped = value.strip()
        try:
            number = float(stripped) if stripped else float("nan")
        except ValueError as error:
            raise ProfileError(
                "invalid_profile", "pressure_target_bar must be a number"
            ) from error
        if not math.isfinite(number):
            raise ProfileError(
                "invalid_profile", "pressure_target_bar must be a finite number"
            )
    else:
        number = _finite("pressure_target_bar", value)
    if number < PRESSURE_TARGET_MIN_BAR or number > PRESSURE_TARGET_MAX_BAR:
        raise ProfileError(
            "invalid_profile",
            "pressure_target_bar must be between "
            f"{PRESSURE_TARGET_MIN_BAR:g} and {PRESSURE_TARGET_MAX_BAR:g}",
        )
    return number


def is_spec_url(value: Optional[str]) -> bool:
    """True when ``value`` is an http(s) URL with a host and no spaces."""
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def require_source_url(profile: MachineProfile) -> None:
    """Cited models need a spec URL. Unconfirmed and custom models do not.

    An unconfirmed built-in also must not carry a tank size. A number
    there would be a placeholder, not a measurement.
    """
    if profile.custom:
        return
    if profile.unconfirmed or not profile.source_url:
        if profile.tank_capacity_ml is not None:
            raise ProfileError(
                "invalid_profile",
                f"{profile.id} has no maker spec, so it cannot carry a capacity",
            )
        return
    if not is_spec_url(profile.source_url):
        raise ProfileError(
            "invalid_profile",
            f"{profile.id} is missing a source URL for the maker spec",
        )
    if profile.tank_capacity_ml is None:
        raise ProfileError(
            "invalid_profile",
            f"{profile.id} cites a spec but has no tank capacity",
        )


def _pressure_or_default(value: Any) -> float:
    if value is None:
        return DEFAULT_PRESSURE_TARGET_BAR
    return validate_pressure_target(value)


def _builtin(
    profile_id: str,
    vendor: str,
    model: str,
    tank_capacity_ml: float,
    accent: str,
    source_url: str,
) -> MachineProfile:
    profile = MachineProfile(
        id=profile_id,
        vendor=vendor,
        model=model,
        tank_capacity_ml=validate_tank(tank_capacity_ml),
        pressure_target_bar=DEFAULT_PRESSURE_TARGET_BAR,
        accent=validate_accent(accent),
        source_url=source_url,
        custom=False,
        unconfirmed=False,
    )
    require_source_url(profile)
    return profile


def _unconfirmed(
    profile_id: str, vendor: str, model: str, accent: str
) -> MachineProfile:
    """A listed model with no maker spec. The tank size is left empty."""
    profile = MachineProfile(
        id=profile_id,
        vendor=vendor,
        model=model,
        tank_capacity_ml=None,
        pressure_target_bar=DEFAULT_PRESSURE_TARGET_BAR,
        accent=validate_accent(accent),
        source_url=None,
        custom=False,
        unconfirmed=True,
    )
    require_source_url(profile)
    return profile


# Order is the order GET /api/machine-profiles returns.
CATALOG_ORDER: Tuple[MachineProfile, ...] = (
    _unconfirmed(GENERIC_ID, "Generic", "Current setup", "crema"),
    _builtin(
        "gaggia-classic-e24",
        "Gaggia",
        "Classic E24",
        2100.0,  # 2.1 L on the maker's spec page
        "gaggia",
        GAGGIA_CLASSIC_E24_SOURCE,
    ),
    _builtin(
        "rancilio-silvia",
        "Rancilio",
        "Silvia",
        2000.0,  # 2 L on the maker's spec page
        "rancilio",
        RANCILIO_SILVIA_SOURCE,
    ),
    MachineProfile(
        id=CUSTOM_ID,
        vendor="Custom",
        model="Custom",
        tank_capacity_ml=None,
        pressure_target_bar=DEFAULT_PRESSURE_TARGET_BAR,
        accent="crema",
        source_url=None,
        custom=True,
        unconfirmed=False,
    ),
)

CATALOG: Dict[str, MachineProfile] = {profile.id: profile for profile in CATALOG_ORDER}

for _profile in CATALOG_ORDER:
    require_source_url(_profile)
    validate_pressure_target(_profile.pressure_target_bar)
    validate_accent(_profile.accent)


def read_legacy_tank_capacity(
    environ: Optional[Mapping[str, str]], tank_state_path: Optional[str]
) -> Optional[float]:
    """The tank size an existing install was already using, if it had one.

    ``WATER_TANK_ML`` wins, because that is what the process used to size
    the tank. Otherwise a saved tank file supplies ``capacity_ml``. A file
    that only has ``used_ml`` was written by the pre-profile app, which
    always counted shots-left against 2000 ml.
    """
    if environ is not None:
        raw = environ.get("WATER_TANK_ML")
        if raw is not None and str(raw).strip():
            try:
                return validate_tank(str(raw).strip())
            except ProfileError as error:
                print(f"WARNING: WATER_TANK_ML ignored: {error.detail}")
    if not tank_state_path:
        return None
    path = os.path.expanduser(tank_state_path)
    if not os.path.exists(path) and not os.path.exists(path + ".bak"):
        return None

    def parse(payload: Any) -> float:
        if not isinstance(payload, dict):
            raise ValueError("expected an object")
        if "capacity_ml" in payload and payload["capacity_ml"] is not None:
            try:
                return validate_tank(payload["capacity_ml"])
            except ProfileError as error:
                raise ValueError(error.detail) from error
        if "used_ml" in payload:
            return LEGACY_DEFAULT_TANK_ML
        raise ValueError("tank file has no capacity")

    return load_with_backup(path, parse)


def _custom_from_payload(data: Any) -> MachineProfile:
    if not isinstance(data, dict):
        raise ValueError("custom must be an object")
    unknown = set(data) - {"tank_capacity_ml", "pressure_target_bar", "accent"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"unknown custom fields: {names}")
    base = CATALOG[CUSTOM_ID]
    try:
        if "tank_capacity_ml" not in data or data["tank_capacity_ml"] is None:
            tank = None
        else:
            tank = validate_tank(data["tank_capacity_ml"])
        if "pressure_target_bar" in data:
            target = _pressure_or_default(data["pressure_target_bar"])
        else:
            target = base.pressure_target_bar
        accent = validate_accent(data["accent"]) if "accent" in data else base.accent
    except ProfileError as error:
        raise ValueError(error.detail) from error
    return replace(
        base, tank_capacity_ml=tank, pressure_target_bar=target, accent=accent
    )


def _parse_user_capacities(data: Any) -> Dict[str, float]:
    if not isinstance(data, dict):
        raise ValueError("user_capacities must be an object")
    parsed: Dict[str, float] = {}
    for key, value in data.items():
        if key not in CATALOG or not CATALOG[key].unconfirmed:
            raise ValueError(f"user capacity is not allowed for {key}")
        try:
            parsed[str(key)] = validate_tank(value)
        except ProfileError as error:
            raise ValueError(error.detail) from error
    return parsed


def _parse_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("expected an object")
    allowed = {
        "machine_profile",
        "custom",
        "prompt_pick_model",
        "pending",
        "user_capacities",
        "migrated_from_tank",
        "refill_required",
    }
    unknown = set(payload) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"unknown profile fields: {names}")
    if "machine_profile" not in payload:
        raise ValueError("machine_profile is missing")
    profile_id = payload["machine_profile"]
    if profile_id is None:
        profile_id = ""
    elif not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("machine_profile must be a string or null")
    custom = None
    if payload.get("custom") is not None:
        custom = _custom_from_payload(payload["custom"])
    prompt = payload.get("prompt_pick_model", False)
    if not isinstance(prompt, bool):
        raise ValueError("prompt_pick_model must be a boolean")
    user_capacities = {}
    if payload.get("user_capacities") is not None:
        user_capacities = _parse_user_capacities(payload["user_capacities"])
    pending = payload.get("pending")
    if pending is not None and not isinstance(pending, dict):
        raise ValueError("pending must be an object")
    migrated = payload.get("migrated_from_tank", False)
    if not isinstance(migrated, bool):
        raise ValueError("migrated_from_tank must be a boolean")
    refill = payload.get("refill_required", False)
    if not isinstance(refill, bool):
        raise ValueError("refill_required must be a boolean")
    return {
        "profile_id": profile_id,
        "custom": custom,
        "prompt_pick_model": prompt,
        "pending": pending,
        "user_capacities": user_capacities,
        "migrated_from_tank": migrated,
        "refill_required": refill,
    }


class MachineProfileStore:
    """The saved ``machine_profile`` choice, custom values, and the prompt.

    A missing file on a fresh install (no ``WATER_TANK_ML``, no tank
    file) is ``Machine not set``. The same situation with a configured
    tank size migrates once onto ``custom`` and raises
    ``prompt_pick_model``. A later start reads the saved file and does
    not migrate again.

    An unknown id is also ``Machine not set``. It does not borrow a
    tank size from another model. A switch during an open shot is kept
    on ``pending`` until that shot is saved.
    """

    def __init__(
        self,
        state_path: Optional[str] = DEFAULT_STATE_PATH,
        read_env: bool = False,
        environ: Optional[Mapping[str, str]] = None,
        tank_state_path: Optional[str] = None,
    ):
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self._lock = threading.Lock()
        self._requested_id = ""
        self._custom: Optional[MachineProfile] = None
        self._user_capacities: Dict[str, float] = {}
        self._pending: Optional[MachineProfile] = None
        self._prompt_pick_model = False
        self._refill_required = False
        self._migrated = False
        self._persisted = False
        self._load_error: Optional[str] = None
        self._load()
        if self._persisted or self._load_error or not read_env:
            return
        env = os.environ if environ is None else environ
        self._configure_fresh(env, tank_state_path)

    def _load(self) -> None:
        if not self.state_path:
            return
        existed = os.path.exists(self.state_path) or os.path.exists(
            self.state_path + ".bak"
        )
        loaded = load_with_backup(self.state_path, _parse_state)
        if loaded is None:
            if existed:
                self._load_error = "profile_state_unreadable"
                self._requested_id = ""
            return
        self._requested_id = loaded["profile_id"]
        self._custom = loaded["custom"]
        self._prompt_pick_model = loaded["prompt_pick_model"]
        self._refill_required = loaded["refill_required"]
        self._user_capacities = loaded["user_capacities"]
        self._migrated = loaded["migrated_from_tank"]
        self._persisted = True
        if loaded["pending"] is not None:
            # The open shot is not restored, so the waiting switch applies.
            try:
                self._promote_pending(loaded["pending"])
            except ProfileError as error:
                print(f"Ignoring unreadable pending profile switch: {error}")
                self._pending = None
            else:
                self._write(self._payload_unlocked())

    def _promote_pending(self, pending: Dict[str, Any]) -> None:
        previous = self._requested_id
        profile = self._profile_from_pending(pending)
        self._install(profile)
        self._mark_refill_if_switched(previous, profile.id)
        self._pending = None

    def _profile_from_pending(self, pending: Mapping[str, Any]) -> MachineProfile:
        if "id" not in pending:
            raise ProfileError("invalid_profile", "pending switch is missing id")
        profile_id = pending["id"]
        catalog = CATALOG.get(profile_id) if isinstance(profile_id, str) else None
        updates: Dict[str, Any] = {}
        if catalog is not None and catalog.custom:
            for key in _PROFILE_FIELDS:
                if key in pending:
                    updates[key] = pending[key]
        elif (
            catalog is not None
            and catalog.unconfirmed
            and pending.get("tank_capacity_ml") is not None
        ):
            updates["tank_capacity_ml"] = pending["tank_capacity_ml"]
        return self._resolve(profile_id, updates)

    def _configure_fresh(
        self, env: Mapping[str, str], tank_state_path: Optional[str]
    ) -> None:
        raw_id = env.get("MACHINE_PROFILE")
        chosen = (
            str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else ""
        )
        legacy = read_legacy_tank_capacity(env, tank_state_path)
        if chosen:
            self._requested_id = chosen
            if chosen in CATALOG:
                self._fill_legacy_capacity(chosen, legacy)
            return
        if legacy is None:
            self._requested_id = ""
            return
        self._migrate(legacy)

    def _fill_legacy_capacity(self, profile_id: str, legacy: Optional[float]) -> None:
        """Keep shots-left when the chosen model has no capacity of its own."""
        if legacy is None:
            return
        profile = CATALOG[profile_id]
        if profile.custom:
            self._custom = replace(profile, tank_capacity_ml=legacy)
            return
        if profile.unconfirmed:
            self._user_capacities[profile_id] = legacy

    def _migrate(self, capacity_ml: float) -> None:
        """Move a pre-profile tank size onto Custom. Safe to repeat."""
        custom = replace(CATALOG[CUSTOM_ID], tank_capacity_ml=capacity_ml)
        self._requested_id = CUSTOM_ID
        self._custom = custom
        self._prompt_pick_model = True
        self._refill_required = False
        self._migrated = True
        self._persisted = True
        self._pending = None
        self._load_error = None
        self._write(self._payload_unlocked())

    def _selected_unlocked(self) -> Optional[MachineProfile]:
        if not self._requested_id or self._requested_id not in CATALOG:
            return None
        if self._requested_id == CUSTOM_ID:
            return self._custom or CATALOG[CUSTOM_ID]
        found = CATALOG[self._requested_id]
        if found.unconfirmed:
            entered = self._user_capacities.get(found.id)
            if entered is not None:
                return replace(found, tank_capacity_ml=entered)
        return found

    def _capacity_source_unlocked(
        self, active: Optional[MachineProfile]
    ) -> Optional[str]:
        if active is None or active.tank_capacity_ml is None:
            return None
        if active.custom:
            return "custom"
        catalog = CATALOG.get(active.id)
        if catalog is not None and catalog.unconfirmed:
            return "user"
        return "profile"

    def _error_unlocked(self) -> Optional[str]:
        if self._load_error:
            return self._load_error
        if self._requested_id and self._requested_id not in CATALOG:
            return "unknown_profile"
        return None

    def _status_unlocked(
        self, active: Optional[MachineProfile], error: Optional[str]
    ) -> str:
        if self._pending is not None:
            return "pending"
        if active is None or error is not None:
            return "machine_not_set"
        if active.tank_capacity_ml is None:
            return "capacity_unconfirmed"
        return "ready"

    def _pending_public(self) -> Optional[Dict[str, Any]]:
        pending = self._pending
        if pending is None:
            return None
        return {
            "id": pending.id,
            "vendor": pending.vendor,
            "model": pending.model,
            "tank_capacity_ml": pending.tank_capacity_ml,
            "pressure_target_bar": pending.pressure_target_bar,
            "status": "pending",
            "reason": "shot_in_progress",
        }

    def _public_unlocked(self) -> Dict[str, Any]:
        active = self._selected_unlocked()
        error = self._error_unlocked()
        if active is None:
            body: Dict[str, Any] = {
                "id": None,
                "vendor": None,
                "model": None,
                "custom": False,
                "unconfirmed": False,
                "capacity_confirmed": False,
                "tank_capacity_ml": None,
                "pressure_target_bar": None,
                "theme": None,
                "source_url": None,
            }
            label = UNSET_LABEL
        else:
            body = active.to_dict()
            label = active.model
        body["ok"] = error is None and (active is not None or self._pending is not None)
        body["error"] = error
        body["status"] = self._status_unlocked(active, error)
        body["label"] = label
        body["requested_id"] = self._requested_id or None
        body["fallback_id"] = None
        body["pending"] = self._pending_public()
        body["prompt_pick_model"] = self._prompt_pick_model
        body["refill_required"] = self._refill_required
        body["tank_capacity_source"] = self._capacity_source_unlocked(active)
        return body

    def active(self) -> Optional[MachineProfile]:
        """The profile in use, or None when the machine is not set."""
        with self._lock:
            return self._selected_unlocked()

    def public(self) -> Dict[str, Any]:
        """Active profile, prompt flag, and any switch waiting on a shot."""
        with self._lock:
            return self._public_unlocked()

    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def list_profiles(self) -> list:
        """Catalog entries. Saved custom and user capacities are filled in."""
        with self._lock:
            items = []
            for profile in CATALOG_ORDER:
                shown = profile
                source: Optional[str] = None
                if profile.id == CUSTOM_ID and self._custom is not None:
                    shown = self._custom
                    if shown.tank_capacity_ml is not None:
                        source = "custom"
                elif profile.unconfirmed:
                    entered = self._user_capacities.get(profile.id)
                    if entered is not None:
                        shown = replace(profile, tank_capacity_ml=entered)
                        source = "user"
                elif profile.tank_capacity_ml is not None:
                    source = "profile"
                item = shown.to_dict()
                item["tank_capacity_source"] = source
                items.append(item)
            return items

    def _resolve(self, profile_id: Any, updates: Mapping[str, Any]) -> MachineProfile:
        if not isinstance(profile_id, str) or profile_id not in CATALOG:
            shown = (
                profile_id if isinstance(profile_id, str) else type(profile_id).__name__
            )
            raise ProfileError("unknown_profile", f"Unknown machine profile '{shown}'")
        unknown = set(updates) - set(_PROFILE_FIELDS)
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ProfileError("invalid_profile", f"unknown fields: {names}")
        profile = CATALOG[profile_id]
        if profile.custom:
            return self._resolve_custom(updates)
        if (
            updates.get("pressure_target_bar") is not None
            or updates.get("accent") is not None
        ):
            raise ProfileError(
                "invalid_profile",
                "The pressure target and accent are set on the custom profile",
            )
        if "tank_capacity_ml" in updates:
            if not profile.unconfirmed:
                raise ProfileError(
                    "invalid_profile",
                    "Tank capacity is fixed by the maker spec on this profile",
                )
            entered = validate_tank(updates["tank_capacity_ml"])
            return replace(profile, tank_capacity_ml=entered)
        if profile.unconfirmed:
            saved = self._user_capacities.get(profile.id)
            if saved is None:
                return profile
            return replace(profile, tank_capacity_ml=saved)
        if updates:
            raise ProfileError(
                "invalid_profile",
                "Tank capacity is fixed by the maker spec on this profile",
            )
        return profile

    def _resolve_custom(self, updates: Mapping[str, Any]) -> MachineProfile:
        base = self._custom or CATALOG[CUSTOM_ID]
        tank = base.tank_capacity_ml
        if "tank_capacity_ml" in updates:
            if updates["tank_capacity_ml"] is None:
                tank = None
            else:
                tank = validate_tank(updates["tank_capacity_ml"])
        if "pressure_target_bar" in updates:
            target = _pressure_or_default(updates["pressure_target_bar"])
        else:
            target = base.pressure_target_bar
        accent = base.accent
        if "accent" in updates:
            accent = validate_accent(updates["accent"])
        return replace(
            base, tank_capacity_ml=tank, pressure_target_bar=target, accent=accent
        )

    def _install(self, profile: MachineProfile) -> None:
        self._requested_id = profile.id
        if profile.custom:
            self._custom = profile
        elif profile.unconfirmed and profile.tank_capacity_ml is not None:
            self._user_capacities[profile.id] = profile.tank_capacity_ml

    def _mark_refill_if_switched(self, previous: str, new_id: str) -> None:
        """A change of model asks for a refill. The first choice does not."""
        if previous and previous != new_id:
            self._refill_required = True

    def _payload_unlocked(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "machine_profile": self._requested_id or None,
            "prompt_pick_model": self._prompt_pick_model,
            "refill_required": self._refill_required,
        }
        if self._migrated:
            payload["migrated_from_tank"] = True
        if self._custom is not None:
            payload["custom"] = {
                "tank_capacity_ml": self._custom.tank_capacity_ml,
                "pressure_target_bar": self._custom.pressure_target_bar,
                "accent": self._custom.accent,
            }
        if self._user_capacities:
            payload["user_capacities"] = dict(self._user_capacities)
        if self._pending is not None:
            payload["pending"] = {
                "id": self._pending.id,
                "tank_capacity_ml": self._pending.tank_capacity_ml,
                "pressure_target_bar": self._pending.pressure_target_bar,
                "accent": self._pending.accent,
            }
        return payload

    def _write(self, payload: Dict[str, Any]) -> None:
        if not self.state_path:
            return
        atomic_write_json(self.state_path, payload)

    def select(
        self,
        profile_id: Any,
        updates: Optional[Mapping[str, Any]] = None,
        defer: bool = False,
    ) -> Dict[str, Any]:
        """Switch profile. Unknown ids and bad values change nothing.

        ``defer`` stores the switch as pending. The live profile, including
        its tank size, stays until :meth:`activate_pending`.
        """
        if updates is None:
            updates = {}
        if not isinstance(updates, Mapping):
            raise ProfileError("invalid_profile", "updates must be an object")
        with self._lock:
            resolved = self._resolve(profile_id, updates)
            if defer:
                self._pending = resolved
            else:
                previous = self._requested_id
                self._install(resolved)
                self._pending = None
                self._mark_refill_if_switched(previous, resolved.id)
            self._load_error = None
            self._persisted = True
            payload = self._payload_unlocked()
            state = self._public_unlocked()
        self._write(payload)
        return state

    def activate_pending(self) -> bool:
        """Apply a switch that was waiting for the open shot to be saved."""
        with self._lock:
            pending = self._pending
            if pending is None:
                return False
            previous = self._requested_id
            self._install(pending)
            self._mark_refill_if_switched(previous, pending.id)
            self._pending = None
            self._persisted = True
            payload = self._payload_unlocked()
            changed = True
        self._write(payload)
        return changed

    def dismiss_prompt(self) -> Dict[str, Any]:
        """Clear the one-time model prompt. A second call stays cleared."""
        with self._lock:
            self._prompt_pick_model = False
            self._persisted = True
            payload = self._payload_unlocked()
            state = self._public_unlocked()
        self._write(payload)
        return state

    def clear_refill(self) -> Dict[str, Any]:
        """Drop the refill flag. The caller zeros used water itself."""
        with self._lock:
            self._refill_required = False
            self._persisted = True
            payload = self._payload_unlocked()
            state = self._public_unlocked()
        self._write(payload)
        return state

    def apply(self, tank: Any = None) -> None:
        """Push the live tank size and the refill flag onto the tank.

        Used water is not changed here. No capacity leaves shots-left
        uncomputed. A required refill hides shots-left until it is confirmed.
        """
        profile = self.active()
        if tank is None:
            return
        if profile is None:
            tank.set_capacity(None, status="machine_not_set")
            tank.set_refill_required(False)
            return
        if profile.tank_capacity_ml is None:
            tank.set_capacity(None, status="capacity_unconfirmed")
            tank.set_refill_required(False)
            return
        tank.set_capacity(profile.tank_capacity_ml)
        tank.set_refill_required(self._refill_required)
