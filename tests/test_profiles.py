import pytest

from haus.profiles import (
    PROFILE_DEFAULTS, check_profile_value, compute_want, curved, is_tracking, kbd_to_value, pipeline,
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
