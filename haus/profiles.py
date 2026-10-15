"""The screen-to-bulb pipeline, the profile/settings model and its migration.

hauslicht.json (schema 2), next to the mesh network keys btmesh keeps at the top:
    bulbs:    {id: {kind, name, profile, power, config}}      id like "mesh:0002"
    profiles: {name: {track, off_at_zero, range_lo, range_hi, curve, level}}
              ("default" always exists)
    settings: {mode, slider, lid, flick, selected}            globals
Pure functions only: the core and the tests share them.
"""

from __future__ import annotations

import math

SCHEMA = 2
MODES = ("independent", "minimum", "maximum", "coupling", "off")  # what the keyboard slider moves
# The keyboard slider (k, 0-100) is a remote for one widget control. Brighter k
# always means brighter bulbs, so the window handles and the curve run the
# other way: a handle further left, or a lower curve, lights more.
KBD_BINDING = {"maximum": "range_hi", "minimum": "range_lo", "coupling": "curve", "independent": "level"}
TRACKING = ("always", "night", "day", "dark", "light", "never")  # when the screen drives the bulbs
LID_EVENTS = ("close", "open", "both", "never")  # which lid events switch the bulbs
RANGE_MIN, RANGE_MAX = -100.0, 200.0  # the screen window's scale
DEFAULT = "default"  # the reserved profile: "all" uses it, deleted profiles fall back to it
PROFILE_DEFAULTS = {"track": "always", "off_at_zero": False, "range_lo": 0.0, "range_hi": 100.0,
                    "curve": 0.0, "level": 50.0}
PROFILE_KEYS = tuple(PROFILE_DEFAULTS)
GLOBAL_DEFAULTS = {"mode": "maximum", "slider": 100, "lid": "both", "flick": True, "selected": "all"}
SETTABLE_GLOBALS = ("mode", "lid", "flick")  # `set key=value`; slider and selected have commands
POWERUP = ("off", "on", "restore")  # OnPowerUp values of the backend contract
LEDVANCE_NAME = "Volkslicht"


def kbd_to_value(key: str, k: float) -> float:
    if key in ("range_lo", "range_hi"):
        return 200.0 - 3.0 * k
    if key == "curve":
        return 100.0 - 2.0 * k
    return float(k)


def value_to_kbd(key: str, value: float) -> int:
    if key in ("range_lo", "range_hi"):
        return max(0, min(100, round((200.0 - value) / 3.0)))
    if key == "curve":
        return max(0, min(100, round((100.0 - value) / 2.0)))
    return max(0, min(100, round(value)))


def screen_window(screen: float, lo: float, hi: float) -> float:
    """Screen % mapped through the window [lo, hi] (scale -100..200) onto 0..1:
    lo gives an off bulb, hi a full one; 0..100 is 1:1."""
    if hi <= lo:
        return 1.0 if screen >= hi else 0.0
    return max(0.0, min(1.0, (screen - lo) / (hi - lo)))


def curved(x: float, curve: float) -> float:
    """curve -100..100: 0 linear, positive rises slowly from the dark end
    (x^4 at 100), negative rises fast (x^0.25 at -100)."""
    return x ** (4 ** (curve / 100))


def pipeline(profile: dict, screen: float, tracking: bool, mode: str) -> float:
    """Bulb % from the screen %: window, then curve; the level when not tracking."""
    if not tracking or mode == "independent":
        return max(0.0, min(100.0, float(profile["level"])))
    return curved(screen_window(screen, profile["range_lo"], profile["range_hi"]), profile["curve"]) * 100


def is_tracking(track: str, night: bool, day: bool, scheme: int | None) -> bool:
    """Whether the screen drives a profile now. scheme: the portal's
    color-scheme (1 dark, 2 light, 0 or None no preference)."""
    return {"always": True, "night": night, "day": day,
            "dark": scheme == 1, "light": scheme == 2}.get(track, False)


def compute_want(profile: dict, *, power: bool, paused: bool, manual: float | None, manual_exact: bool,
                 screen: float, tracking: bool, mode: str, min_lit: float) -> float | None:
    """What a bulb should show; None while a manual command rules (paused, no level).
    Switched on it never drops below min_lit unless the profile turns off at zero;
    an exact manual level (`dim`) is sent as it is."""
    if not power:
        return 0.0
    if paused and manual is None:
        return None
    if paused:
        want = float(manual)
        if manual_exact:
            return want
    else:
        want = round(pipeline(profile, screen, tracking, mode), 1)
    if not profile["off_at_zero"] and want < min_lit:
        want = min_lit  # switched on, however dark: keep a glow
    return want


def powerup_target(after_toggle: float | None, min_lit: float) -> str:
    """OnPowerUp that boots a bulb into what a toggle would show: lit or dark.
    A value at or under half the lowest lit step rounds to dark."""
    return "on" if after_toggle is not None and after_toggle > min_lit / 2 else "off"


def parse_bool(value) -> bool:
    if value in (True, 1, "on", "true", "1", "yes"):
        return True
    if value in (False, 0, "off", "false", "0", "no"):
        return False
    raise ValueError(f"expected on or off, got {value!r}")


def check_profile_value(profile: dict, key: str, value):
    """Validated, clamped value of one profile key."""
    if key == "track":
        if value not in TRACKING:
            raise ValueError(f"track must be one of {', '.join(TRACKING)}")
        return value
    if key == "off_at_zero":
        return parse_bool(value)
    if key in ("range_lo", "range_hi"):
        value = max(RANGE_MIN, min(RANGE_MAX, float(value)))
        # the handles may meet but not cross
        return min(value, profile["range_hi"]) if key == "range_lo" else max(value, profile["range_lo"])
    if key == "curve":
        return max(-100.0, min(100.0, float(value)))
    if key == "level":
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("level needs a number")
        return max(0.0, min(100.0, value))
    raise ValueError(f"unknown setting {key}")


def check_global_value(key: str, value):
    if key == "mode":
        if value not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        return value
    if key == "lid":
        if value not in LID_EVENTS:
            raise ValueError(f"lid must be one of {', '.join(LID_EVENTS)}")
        return value
    if key == "flick":
        return parse_bool(value)
    raise ValueError(f"unknown setting {key}")


def mesh_id(unicast: str | int) -> str:
    """Bulb id of a mesh node: mesh:0002 for unicast 0x0002."""
    addr = int(unicast, 16) if isinstance(unicast, str) else unicast
    return f"mesh:{addr:04x}"


def _migrate_lid(d: dict) -> None:
    """lid_state (bool) became lid (events); screen_state is gone."""
    if "lid_state" in d:
        d.setdefault("lid", "both" if d.pop("lid_state") else "never")
    d.pop("screen_state", None)


def _clean_profile(p: dict) -> dict:
    out = {}
    for key, default in PROFILE_DEFAULTS.items():
        try:
            out[key] = check_profile_value({**PROFILE_DEFAULTS, **out}, key, p.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    if out["range_lo"] > out["range_hi"]:
        out["range_lo"], out["range_hi"] = out["range_hi"], out["range_lo"]
    return out


def migrate(state: dict) -> bool:
    """Bring a state dict to schema 2 in place; True when anything changed.

    Schema 1 (single bulb): `sync` held every setting, `presets` named copies,
    every mesh node in `devices` was driven. Now: `sync`'s screen keys become
    the profile "default", presets become profiles (their global keys are
    dropped), mode/slider/lid become settings, and every mesh node becomes a
    bulb, switched off (power was not kept before; dark is the safe guess)."""
    before = repr(state)
    old = state.pop("sync", None)
    presets = state.pop("presets", None)
    v1 = state.get("version", 1) < SCHEMA
    bulbs = state.setdefault("bulbs", {})
    profiles = state.setdefault("profiles", {})
    settings = state.setdefault("settings", {})
    if old is not None:
        _migrate_lid(old)
        profiles.setdefault(DEFAULT, {k: old[k] for k in PROFILE_KEYS if k in old})
        for key in ("mode", "slider", "lid"):
            if key in old:
                settings.setdefault(key, old[key])
    for name, preset in (presets or {}).items():
        _migrate_lid(preset)
        target = name if name != DEFAULT else f"{DEFAULT} (preset)"
        profiles.setdefault(target, {k: preset[k] for k in PROFILE_KEYS if k in preset})
    if v1:
        devices = sorted(state.get("devices", {}), key=lambda a: int(a, 16))
        for i, unicast in enumerate(devices):
            bulbs.setdefault(mesh_id(unicast), {
                "kind": "mesh",
                "name": LEDVANCE_NAME if len(devices) == 1 else f"{LEDVANCE_NAME} {i + 1}",
                "profile": DEFAULT, "power": False,
                "config": {"unicast": f"0x{int(unicast, 16):04x}", "scale": 100}})
    profiles.setdefault(DEFAULT, {})
    for name in list(profiles):
        profiles[name] = _clean_profile(profiles[name])
    for key, default in GLOBAL_DEFAULTS.items():
        settings.setdefault(key, default)
    try:
        settings["mode"] = check_global_value("mode", settings["mode"])
        settings["lid"] = check_global_value("lid", settings["lid"])
        settings["flick"] = check_global_value("flick", settings["flick"])
    except ValueError:
        for key in ("mode", "lid", "flick"):
            settings[key] = GLOBAL_DEFAULTS[key]
    for entry in bulbs.values():
        entry.setdefault("name", "")
        entry.setdefault("config", {})
        entry["power"] = bool(entry.get("power", False))
        if entry.get("profile") not in profiles:
            entry["profile"] = DEFAULT
    if settings["selected"] != "all" and settings["selected"] not in bulbs:
        settings["selected"] = "all"
    state["version"] = SCHEMA
    return repr(state) != before


def new_state() -> dict:
    state: dict = {"version": SCHEMA}
    migrate(state)
    return state
