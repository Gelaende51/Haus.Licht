"""What every follow mode (audio, video) shares when it drives a bulb: the
rate cap and the deadband. One bulb follows one source at a time (audio and
video are exclusive per bulb), so the cap is the bulb's: the global
`follow_rate` (1-3 updates a second, default 2; before: `audio_rate`).

    rate cap   at most follow_rate updates a second per bulb, never more than
               MAX_RATE whatever the kind: fast flashing is a photosensitivity
               risk, and on Bluetooth mesh every message uses one of the
               bulb's 24-bit sequence numbers (hence the default 2)
    deadband   an update goes out only when the brightness moved LEVEL_STEP
               points, the colour jumped (HUE_STEP, SAT_STEP, CT_STEP), or
               HOLD_S passed with any change at all

The daemon's serve_follow() applies both; haus/audiofollow.py and
haus/videofollow.py only say what the bulb should show.
"""

from __future__ import annotations

import math

RATES = (1, 2, 3)
DEFAULT_RATE = 2  # Bluetooth mesh: each update burns a sequence number
MAX_RATE = 3  # hard cap for every kind: photosensitivity
LEVEL_STEP = 3.0  # percent points: the deadband
HUE_STEP = 8.0  # degrees
CT_STEP = 150  # Kelvin
SAT_STEP = 8.0
HOLD_S = 2.0  # after this long any change is sent


def check_rate(value, key: str = "follow_rate") -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be 1, 2 or 3") from None
    if not math.isfinite(v) or round(v) not in RATES:
        raise ValueError(f"{key} must be 1, 2 or 3")
    return int(round(v))


def interval(rate: int) -> float:
    """Seconds between two updates of one bulb: never under 1 / MAX_RATE."""
    return 1.0 / max(1, min(MAX_RATE, int(rate)))


def color_jump(new: dict | None, old: dict | None) -> bool:
    """A colour past the deadband: hue ≥ HUE_STEP, Kelvin ≥ CT_STEP, another mode."""
    if new is None:
        return False
    if old is None or new.get("mode") != old.get("mode"):
        return True
    if new["mode"] == "ct":
        return abs(new["kelvin"] - old["kelvin"]) >= CT_STEP
    dh = abs(new["hue"] - old["hue"]) % 360
    return min(dh, 360 - dh) >= HUE_STEP or abs(new["sat"] - old["sat"]) >= SAT_STEP


def should_send(level: float, sent_level: float | None, color: dict | None, sent_color: dict | None,
                since_s: float, changed: bool) -> bool:
    """The deadband, after the rate cap allowed an update: brightness moved
    LEVEL_STEP, the colour jumped, or HOLD_S passed with any change at all."""
    if sent_level is None or abs(level - sent_level) >= LEVEL_STEP or color_jump(color, sent_color):
        return True
    return changed and since_s >= HOLD_S
