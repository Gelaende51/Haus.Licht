import pytest

from haus.profiles import (
    PROFILE_DEFAULTS, check_profile_value, color_moved, color_rgb, color_want, kelvin_rgb, compute_want, curved, is_tracking, kbd_to_value, pipeline,
    powerup_target, screen_window, value_to_kbd,
)

P = dict(PROFILE_DEFAULTS)


def want(profile=None, **kw):
    args = dict(power=True, paused=False, manual=None, manual_exact=False, screen=50.0,
                tracking=True, mode="maximum", min_lit=0.5)
    args.update(kw)
    return compute_want({**P, **(profile or {})}, **args)


def test_window_and_curve():
    assert screen_window(50, 0, 100) == 0.5
    assert screen_window(-10, 0, 100) == 0.0
    assert screen_window(150, 0, 100) == 1.0
    assert screen_window(50, -100, 200) == pytest.approx(0.5)
    assert screen_window(60, 50, 50) == 1.0 and screen_window(40, 50, 50) == 0.0
    assert curved(0.5, 0) == 0.5
    assert curved(0.5, 100) == pytest.approx(0.5 ** 4)
    assert curved(0.5, -100) == pytest.approx(0.5 ** 0.25)


def test_pipeline_level_rules_when_not_tracking_or_independent():
    prof = {**P, "level": 30.0}
    assert pipeline(prof, 80, tracking=False, mode="maximum") == 30.0
    assert pipeline(prof, 80, tracking=True, mode="independent") == 30.0
    assert pipeline(prof, 80, tracking=True, mode="maximum") == pytest.approx(80.0)


def test_want_power_pause_floor():
    assert want(power=False) == 0.0
    assert want(paused=True) is None  # a manual command without a level rules
    assert want(screen=0.0) == 0.5  # switched on: never below the lowest lit step
    assert want(screen=0.0, min_lit=1.0) == 1.0
    assert want({"off_at_zero": True}, screen=0.0) == 0.0
    assert want(paused=True, manual=0.0) == 0.5  # nudged/brightness slider: floored
    assert want(paused=True, manual=0.0, manual_exact=True) == 0.0  # `dim 0`: off
    assert want(screen=33.333) == 33.3


def test_powerup_target():
    assert powerup_target(0.5, 0.5) == "on"
    assert powerup_target(0.25, 0.5) == "off"  # rounds to dark on a 0.5 step
    assert powerup_target(0.3, 0.5) == "on"
    assert powerup_target(0.5, 1.0) == "off"
    assert powerup_target(0.0, 0.5) == "off"
    assert powerup_target(None, 0.5) == "off"


def test_tracking_values():
    assert is_tracking("always", False, True, None)
    assert not is_tracking("never", True, False, 1)
    assert is_tracking("night", True, False, None) and not is_tracking("night", False, True, None)
    assert is_tracking("day", False, True, None) and not is_tracking("day", True, False, None)
    assert is_tracking("dark", False, True, 1) and not is_tracking("dark", False, True, 2)
    assert is_tracking("light", False, True, 2) and not is_tracking("light", False, True, 0)


def test_kbd_mapping_round_trip():
    for key in ("range_lo", "range_hi", "curve", "level"):
        for k in (0, 17, 50, 100):
            assert value_to_kbd(key, kbd_to_value(key, k)) == k


def test_profile_values_are_clamped_and_handles_do_not_cross():
    prof = {**P, "range_lo": 20.0, "range_hi": 80.0}
    assert check_profile_value(prof, "range_lo", 90) == 80.0
    assert check_profile_value(prof, "range_hi", 10) == 20.0
    assert check_profile_value(prof, "range_hi", 999) == 200.0
    assert check_profile_value(prof, "curve", -500) == -100.0
    assert check_profile_value(prof, "off_at_zero", "on") is True
    with pytest.raises(ValueError):
        check_profile_value(prof, "track", "sometimes")
    with pytest.raises(ValueError):
        check_profile_value(prof, "mode", "maximum")


def test_mesh_lightness_scale():
    from haus.backends.mesh import lightness_scale
    assert lightness_scale(0x052E, None) == 100
    assert lightness_scale(0x052E, (1, 65535)) == 100  # the quirk table wins
    assert lightness_scale(0x0059, (1, 100)) == 100
    assert lightness_scale(0x0059, (1, 65535)) == 65535
    assert lightness_scale(None, None) == 65535


def test_color_want_not_following():
    assert color_want(P, {"hs"}, 3000) == {"mode": "hs", "hue": 30.0, "sat": 0.0}
    assert color_want({**P, "hue": 240, "sat": 70}, {"hs", "ct"}, None) == {"mode": "hs", "hue": 240.0, "sat": 70.0}
    assert color_want(P, {"ct"}, 3000) is None  # a white bulb's white is left alone
    assert color_want(P, set(), 3000) is None


def test_color_want_following_shift_and_clamp():
    f = {**P, "follow_ct": True}
    assert color_want(f, {"ct"}, None) is None  # Night Light unknown
    assert color_want(f, {"ct", "hs"}, 4000) == {"mode": "ct", "kelvin": 4000}  # ct wins
    assert color_want({**f, "shift": 0}, {"ct"}, 4000) == {"mode": "ct", "kelvin": 6500}
    # double the shift in mired: 153.8 + 2 * (250 - 153.8) = 346.2 -> 2889 K
    assert color_want({**f, "shift": 200}, {"ct"}, 4000) == {"mode": "ct", "kelvin": 2889}
    assert color_want(f, {"ct"}, 2000) == {"mode": "ct", "kelvin": 2700}  # clamped to the default range
    assert color_want(f, {"ct"}, 2000, (2200, 6000)) == {"mode": "ct", "kelvin": 2200}
    assert color_want(f, {"ct"}, 6500, (2200, 6000)) == {"mode": "ct", "kelvin": 6000}
    warm = color_want(f, {"hs"}, 3000)
    assert warm["mode"] == "hs" and 25 < warm["hue"] < 35 and 50 < warm["sat"] < 65
    assert color_want({**f, "shift": 200}, {"hs"}, 1000) == color_want(f, {"hs"}, 1000)  # clamped at 1000 K
    assert color_want(f, {"hs"}, 6500)["sat"] < 5  # neutral: nearly white
    assert color_want(f, {"onoff"}, 3000) is None


def test_color_helpers():
    assert kelvin_rgb(6500) == (255, 254, 250) and kelvin_rgb(1000)[2] == 0
    assert color_rgb({"mode": "hs", "hue": 0, "sat": 100}) == "#ff0000"
    assert color_rgb({"mode": "hs", "hue": 240, "sat": 50}) == "#8080ff"
    assert color_rgb({"mode": "ct", "kelvin": 6500}) == "#fffefa" and color_rgb(None) is None
    hs = {"mode": "hs", "hue": 359.5, "sat": 50.0}
    assert not color_moved({**hs, "hue": 0.2}, hs) and color_moved({**hs, "hue": 1.0}, hs)
    assert color_moved({"mode": "ct", "kelvin": 3000}, hs) and color_moved(hs, None) and not color_moved(None, hs)
    assert not color_moved({"mode": "ct", "kelvin": 3020}, {"mode": "ct", "kelvin": 3000})


def test_colour_profile_values():
    assert check_profile_value(P, "hue", "400") == 360.0 and check_profile_value(P, "sat", -3) == 0.0
    assert check_profile_value(P, "shift", 250) == 200.0 and check_profile_value(P, "follow_ct", "on") is True
    with pytest.raises(ValueError):
        check_profile_value(P, "hue", "nan")
