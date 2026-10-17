"""The screen-to-bulb pipeline, the profile/settings model and its migration.

hauslicht.json (schema 4), next to the mesh network keys btmesh keeps at the top:
    bulbs:    {id: {kind, name, profile, power, use, icon_on, icon_off, config}}
              id like "mesh:0002"; the dict's order is the user's list order;
              use false: idle (never driven, OnPowerUp left "on");
              manual {level, exact} only while a manual level rules (paused;
              level None: paused with no level), kept across a restart
    profiles: {name: {track, off_at_zero, range_lo, range_hi, curve, level, hue, sat, follow_ct, shift}}
              ("default" always exists)
    settings: {mode, slider, lid, flick, widgets, widget_bulbs, selected}   globals;
              widgets: how many widgets (1-8); per widget ("1", ...): widget_bulbs
              its bulb ids in list order, selected "all" | one of them;
              flick: off | on | fast | exclusive (schema 3: a bool)
Pure functions only: the core and the tests share them.
"""

from __future__ import annotations

import copy
import math

SCHEMA = 4
FLICK_MODES = ("off", "on", "fast", "exclusive")  # wall-switch flicks; fast/exclusive need the root setup
FAST_FLICK = ("fast", "exclusive")
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
                    "curve": 0.0, "level": 50.0, "hue": 30.0, "sat": 0.0, "follow_ct": False, "shift": 100.0}
PROFILE_KEYS = tuple(PROFILE_DEFAULTS)
GLOBAL_DEFAULTS = {"mode": "maximum", "slider": 100, "lid": "both", "flick": "on", "widgets": 1,
                   "widget_bulbs": {"1": []}, "selected": {"1": "all"}}
SETTABLE_GLOBALS = ("mode", "lid", "flick")  # `set key=value`; slider and selected have commands
MAX_WIDGETS = 8
BULB_DEFAULTS = {"name": "", "use": True, "icon_on": "", "icon_off": "", "config": {}}
POWERUP = ("off", "on", "restore")  # OnPowerUp values of the backend contract
LEDVANCE_NAME = "Volkslicht"
NEUTRAL_K = 6500  # Night Light's "no shift"; also the hs bulbs' coolest white
HS_RANGE_K = (1000, 6500)  # what a hue/sat bulb is given to imitate
CT_RANGE_K = (2700, 6500)  # a white bulb's default range (Backend.ct_range)
HUE_STEP, SAT_STEP, KELVIN_STEP = 1.0, 1.0, 25  # resend a colour only when it moved this much


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


def mired(kelvin: float) -> float:
    return 1e6 / kelvin


def kelvin_rgb(kelvin: float) -> tuple[int, int, int]:
    """Blackbody colour of a white, after Tanner Helland's fit
    (tannerhelland.com/2012/09/18/convert-temperature-rgb-algorithm-code.html):
    good to a few percent over 1000-40000 K, enough to show or imitate a white."""
    t = max(1000.0, min(40000.0, float(kelvin))) / 100
    r = 255.0 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    g = (99.4708025861 * math.log(t) - 161.1195681661 if t <= 66
         else 288.1221695283 * (t - 60) ** -0.0755148492)
    b = 255.0 if t >= 66 else 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(max(0, min(255, round(c))) for c in (r, g, b))


def rgb_hs(rgb: tuple[int, int, int]) -> tuple[float, float]:
    """Hue 0-360 and saturation 0-100 (HSV) of an RGB triple; the value is dropped."""
    r, g, b = (c / 255 for c in rgb)
    hi, lo = max(r, g, b), min(r, g, b)
    if hi == lo:
        return 0.0, 0.0
    d = hi - lo
    if hi == r:
        h = ((g - b) / d) % 6
    elif hi == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60, d / hi * 100


def hs_rgb(hue: float, sat: float) -> tuple[int, int, int]:
    """Full-value RGB of a hue/saturation."""
    h, s = (hue % 360) / 60, sat / 100
    x = 1 - abs(h % 2 - 1)
    r, g, b = [(1, x, 0), (x, 1, 0), (0, 1, x), (0, x, 1), (x, 0, 1), (1, 0, x)][int(h) % 6]
    return tuple(round(255 * (1 - s * (1 - c))) for c in (r, g, b))


def color_want(profile: dict, caps, screen_k: float | None,
               ct_range: tuple[float, float] = CT_RANGE_K) -> dict | None:
    """The colour a bulb should show, or None (leave its colour alone).

    Following (`follow_ct` on and Night Light readable): the bulb's white is
    shifted from 6500 K like the screen's, in mired, scaled by `shift` (100 the
    same shift, 0 none, 200 double), clamped to the bulb's range: a white bulb
    (`ct`) gets {mode ct, kelvin}; a colour-only one (`hs`) the blackbody hue
    and saturation of that Kelvin (range 1000-6500). Not following: a colour
    bulb gets the profile's hue/sat, a white one is left as it is."""
    caps = set(caps)
    if profile["follow_ct"] and screen_k:
        m = mired(NEUTRAL_K) + profile["shift"] / 100 * (mired(screen_k) - mired(NEUTRAL_K))
        kelvin = 1e6 / m if m > 0 else float(NEUTRAL_K)
        if "ct" in caps:
            lo, hi = ct_range
            return {"mode": "ct", "kelvin": round(max(lo, min(hi, kelvin)))}
        if "hs" in caps:
            hue, sat = rgb_hs(kelvin_rgb(max(HS_RANGE_K[0], min(HS_RANGE_K[1], kelvin))))
            return {"mode": "hs", "hue": round(hue, 1), "sat": round(sat, 1)}
        return None
    if "hs" in caps:
        return {"mode": "hs", "hue": round(float(profile["hue"]), 1), "sat": round(float(profile["sat"]), 1)}
    return None


def color_moved(new: dict | None, old: dict | None) -> bool:
    """Whether a colour differs enough from the one sent to send it."""
    if new is None:
        return False
    if old is None or new["mode"] != old["mode"]:
        return True
    if new["mode"] == "ct":
        return abs(new["kelvin"] - old["kelvin"]) >= KELVIN_STEP
    dh = abs(new["hue"] - old["hue"]) % 360
    return min(dh, 360 - dh) >= HUE_STEP or abs(new["sat"] - old["sat"]) >= SAT_STEP


def color_rgb(color: dict | None) -> str | None:
    """"#rrggbb" for display: hs at full value, ct its blackbody colour."""
    if color is None:
        return None
    rgb = kelvin_rgb(color["kelvin"]) if color["mode"] == "ct" else hs_rgb(color["hue"], color["sat"])
    return "#{:02x}{:02x}{:02x}".format(*rgb)


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
    if key in ("level", "hue", "sat", "shift"):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{key} needs a number")
        return max(0.0, min({"hue": 360.0, "shift": 200.0}.get(key, 100.0), value))
    if key == "follow_ct":
        return parse_bool(value)
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
        if value in FLICK_MODES:
            return value
        try:
            return "on" if parse_bool(value) else "off"  # schema 3 stored a bool
        except ValueError:
            raise ValueError(f"flick must be one of {', '.join(FLICK_MODES)}") from None
    raise ValueError(f"unknown setting {key}")


def flick_toggles(mode: str) -> bool:
    """Whether a flick toggles (every mode but off)."""
    return mode != "off"


def check_manual(value) -> dict | None:
    """A stored manual level: {"level": 0-100 or None, "exact": bool}; None: none or unreadable."""
    if not isinstance(value, dict):
        return None
    level = value.get("level")
    if level is not None:
        try:
            level = float(level)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(level):
            return None
        level = max(0.0, min(100.0, level))
    return {"level": level, "exact": value.get("exact") is True}


def check_widget_count(value) -> int:
    n = int(value)
    if not 1 <= n <= MAX_WIDGETS:
        raise ValueError(f"widgets must be 1-{MAX_WIDGETS}")
    return n


def check_icon(value) -> str:
    """A freedesktop icon name or an absolute file path; "" = none."""
    value = "" if value is None else str(value).strip()
    if any(ord(c) < 32 for c in value) or ("/" in value and not value.startswith("/")):
        raise ValueError("an icon is an icon name or an absolute file path")
    return value


def clean_widget_bulbs(lists, bulbs: dict, count: int) -> dict:
    """Per widget 1..count its bulb ids: known ones, once, in the list order
    of `bulbs`; higher widgets dropped, missing ones empty."""
    if not isinstance(lists, dict):
        lists = {}
    out = {}
    for w in range(1, count + 1):
        ids = lists.get(str(w))
        ids = set(ids) if isinstance(ids, (list, tuple)) else set()
        out[str(w)] = [bid for bid in bulbs if bid in ids]
    return out


def clean_selected(selected, bulbs: dict, lists: dict, count: int) -> dict:
    """Per widget 1..count: "all" or a used bulb of that widget."""
    if isinstance(selected, str):
        selected = {"1": selected}  # schema 2: one selection
    if not isinstance(selected, dict):
        selected = {}
    out = {}
    for w in range(1, count + 1):
        sel = selected.get(str(w), "all")
        ok = sel == "all" or (sel in lists.get(str(w), ()) and bulbs[sel].get("use", True))
        out[str(w)] = sel if ok else "all"
    return out


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
    """Bring a state dict to schema 4 in place; True when anything changed.

    Schema 1 (single bulb): `sync` held every setting, `presets` named copies,
    every mesh node in `devices` was driven. Now: `sync`'s screen keys become
    the profile "default", presets become profiles (their global keys are
    dropped), mode/slider/lid become settings, and every mesh node becomes a
    bulb, switched off (power was not kept before; dark is the safe guess).
    Schema 2 had one widget and one selection (a string): every bulb is now
    used and in widget 1, and that string is widget 1's selection.
    Schema 3's flick was a bool: true is now "on", false "off"."""
    before = repr(state)
    old = state.pop("sync", None)
    presets = state.pop("presets", None)
    v1 = state.get("version", 1) < 2
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
    if "widget_bulbs" not in settings:
        settings["widget_bulbs"] = {"1": list(bulbs)}
    for key, default in GLOBAL_DEFAULTS.items():
        settings.setdefault(key, copy.deepcopy(default))
    try:
        settings["mode"] = check_global_value("mode", settings["mode"])
        settings["lid"] = check_global_value("lid", settings["lid"])
        settings["flick"] = check_global_value("flick", settings["flick"])
    except ValueError:
        for key in ("mode", "lid", "flick"):
            settings[key] = GLOBAL_DEFAULTS[key]
    try:
        settings["widgets"] = check_widget_count(settings["widgets"])
    except (TypeError, ValueError):
        settings["widgets"] = 1
    for entry in bulbs.values():
        for key, default in BULB_DEFAULTS.items():
            entry.setdefault(key, copy.deepcopy(default))
        entry["power"] = bool(entry.get("power", False))
        entry["use"] = entry["use"] is not False
        if "manual" in entry:
            manual = check_manual(entry["manual"])
            if manual is None:
                entry.pop("manual")
            else:
                entry["manual"] = manual
        if entry.get("profile") not in profiles:
            entry["profile"] = DEFAULT
        for key in ("icon_on", "icon_off"):
            try:
                entry[key] = check_icon(entry[key])
            except ValueError:
                entry[key] = ""
    settings["widget_bulbs"] = clean_widget_bulbs(settings["widget_bulbs"], bulbs, settings["widgets"])
    settings["selected"] = clean_selected(settings["selected"], bulbs, settings["widget_bulbs"],
                                          settings["widgets"])
    state["version"] = SCHEMA
    return repr(state) != before


def new_state() -> dict:
    state: dict = {"version": SCHEMA}
    migrate(state)
    return state
