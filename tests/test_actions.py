"""Identify and Test (haus/actions.py): the bulb taken out of following, then given back."""

import asyncio

import pytest

from haus import actions
from haus.common import UserError, save_state
from haus.daemon import Daemon
from test_core import FakeDesktop, cmd, mock, mock_state, run, settle


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """Short timings: the steps stay, the seconds go."""
    for name, value in (("IDENTIFY_HALF_S", 0.02), ("IDENTIFY_MAX_S", 10.0), ("TEST_BRIGHT_S", 0.01),
                        ("TEST_FADE_S", 0.04), ("TEST_FADE_STEPS", 4), ("TEST_COLOR_S", 0.01),
                        ("TEST_STROBE", (0.005, 0.01)), ("TEST_STROBE_CYCLES", 2), ("END_WAIT_S", 1.0)):
        monkeypatch.setattr(actions, name, value)


async def until(cond, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def calls_of(m, bid, since=0):
    return [c for c in m.calls[since:] if c[1] == bid]


def applied(m, bid, since=0):
    return [c[2] for c in calls_of(m, bid, since) if c[0] == "apply"]


def lit(**extra):
    return {"power": True, **extra}


def test_status_fields_present(tmp_path):
    async def body(d):
        g = await cmd(d, "get")
        assert g["identifying"] == [] and g["testing"] == {}
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_identify_blinks_stops_at_the_limit_and_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "IDENTIFY_MAX_S", 0.15)

    async def body(d):
        m = mock(d)
        assert m.applied["mock:1"] == 50.0
        i0 = len(m.calls)
        g = await cmd(d, "identify", "mock:1")
        assert g["identifying"] == ["mock:1"]
        await until(lambda: not d.actions)
        blinks = applied(m, "mock:1", i0)
        assert blinks[:4] == [100.0, 0.0, 100.0, 0.0]
        assert blinks[-1] == 50.0  # given back: its intended level
        assert (await cmd(d, "get"))["identifying"] == []
        assert m.powerup["mock:1"] == "off"  # armed as before (lit: a flick boots it dark)
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_nothing_else_sends_while_identifying(tmp_path):
    async def body(d):
        m = mock(d)
        i0 = len(m.calls)
        await cmd(d, "identify", "mock:1")
        await cmd(d, "off", which="mock:1")  # the intended state moves ...
        await cmd(d, "on", which="mock:1")
        await cmd(d, "set", "level=80")
        d.desktop.screen = 70.0
        d.on_clap({"label": "Clap"})
        await settle(d, 5)
        assert d.actions  # still identifying
        during = calls_of(m, "mock:1", i0)
        assert {c[0] for c in during} == {"apply"}  # ... but no level, colour or OnPowerUp of its own
        assert {c[2] for c in during} <= {0.0, 100.0}
        assert applied(m, "mock:2", i0)  # the other bulb still follows
        g = await cmd(d, "identify", "mock:1", "off")
        assert g["identifying"] == []
        want = d.want(d.bulbs["mock:1"])
        assert applied(m, "mock:1", i0)[-1] == want
        await settle(d)
        assert m.powerup["mock:1"] == ("off" if d.bulbs["mock:1"].power else "on")
    run(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


@pytest.mark.parametrize("color,transition", [("hs", False), ("ct", True), ("none", False), ("both", True)])
def test_test_runs_its_steps(tmp_path, color, transition):
    async def body(d):
        m = mock(d)
        b = d.bulbs["mock:1"]
        i0 = len(m.calls)
        g = await cmd(d, "test", "mock:1")
        assert g["testing"]["mock:1"]["running"] is True
        await until(lambda: not d.actions)
        seen = calls_of(m, "mock:1", i0)
        levels = applied(m, "mock:1", i0)
        assert levels[0] == 100.0
        low = d.min_lit(b)
        if transition:
            assert ("fade", "mock:1", low) in seen
        else:
            assert levels[1:5] == sorted(levels[1:5], reverse=True) and levels[4] == low
        colors = [c[2] for c in seen if c[0] == "color"]
        if color in ("hs", "both"):
            assert [(c["hue"], c["sat"]) for c in colors[:4]] == [(0.0, 100.0), (120.0, 100.0), (240.0, 100.0),
                                                                 (0.0, 0.0)]
        elif color == "ct":
            assert colors[:2] == [{"mode": "ct", "kelvin": 2700}, {"mode": "ct", "kelvin": 6500}]
        else:
            assert colors == []
        assert levels[-9:-1] == [100.0, 0.0] * 4  # the last strobe phase, then the bulb given back
        assert levels[-1] == d.want(b)
        result = (await cmd(d, "get"))["testing"]["mock:1"]
        assert result["running"] is False and result["step"] == "done" and result["rate"] > 0
        assert result["fade"] == ("transition" if transition else "steps")
        await settle(d)
        intended = d.color(b)
        if color != "none" and intended is not None:
            assert m.colors["mock:1"] == intended  # the worker sent the intended colour again
    run(tmp_path, mock_state(mock_1=lit(config={"color": color, "transition": transition})), body)


def test_stop_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "TEST_BRIGHT_S", 5.0)

    async def body(d):
        m = mock(d)
        i0 = len(m.calls)
        await cmd(d, "test", "mock:1")
        await until(lambda: applied(m, "mock:1", i0) == [100.0])
        g = await cmd(d, "test", "mock:1", "stop")
        assert g["testing"]["mock:1"]["running"] is False and g["testing"]["mock:1"]["step"] == "stopped"
        assert g["testing"]["mock:1"]["why"] == "stop"
        assert applied(m, "mock:1", i0) == [100.0, 50.0]
        assert not d.actions
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_colour_left_alone_is_put_back(tmp_path, monkeypatch):
    async def body(d):
        m = mock(d)
        b = d.bulbs["mock:1"]
        assert d.color(b) is None  # white-only, not following Night Light: left alone
        m.colors["mock:1"] = {"mode": "ct", "kelvin": 3000}
        await cmd(d, "test", "mock:1")
        await until(lambda: not d.actions)
        assert m.colors["mock:1"] == {"mode": "ct", "kelvin": 3000}
    run(tmp_path, mock_state(mock_1=lit(config={"color": "ct"})), body)


def test_a_test_ends_identify_and_identify_waits_for_the_test(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "TEST_BRIGHT_S", 5.0)

    async def body(d):
        await cmd(d, "identify", "mock:1")
        g = await cmd(d, "test", "mock:1")
        assert g["identifying"] == [] and g["testing"]["mock:1"]["running"] is True
        with pytest.raises(UserError):
            await cmd(d, "identify", "mock:1")
        await cmd(d, "test", "mock:1", "stop")
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_flick_and_offline_end_it(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "identify", "mock:1")
        await asyncio.sleep(0.05)
        i0 = len(m.calls)
        await cmd(d, "mock-flick", "mock:1")
        await until(lambda: not d.actions)
        b = d.bulbs["mock:1"]
        assert b.power is False and applied(m, "mock:1", i0)[-1] == 0.0  # the flick's new state
        await cmd(d, "identify", "mock:2")
        await cmd(d, "mock-offline", "mock:2")
        await until(lambda: not d.actions)
        with pytest.raises(UserError):
            await cmd(d, "test", "mock:2")  # unreachable: refused
    run(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


def test_unused_bulb_refused(tmp_path):
    async def body(d):
        with pytest.raises(UserError):
            await cmd(d, "identify", "mock:1")
    run(tmp_path, mock_state(mock_1=lit(use=False)), body)


def test_suspend_and_stop_end_it_and_still_disarm(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1=lit(), mock_2=lit()))

    async def main():
        d = Daemon(path, tmp_path / "sock", desktop=FakeDesktop())
        await d.start()
        follow = asyncio.create_task(d.follow())
        try:
            await settle(d)
            m = mock(d)
            await cmd(d, "identify", "mock:1")
            await asyncio.sleep(0.05)
            await d.before_sleep()  # lid both: dark while asleep, OnPowerUp "on"
            assert not d.actions
            assert m.applied["mock:1"] == 0.0 and m.powerup["mock:1"] == "on"
            await d.after_wake()
            await settle(d)
            assert m.applied["mock:1"] == 50.0
            await cmd(d, "test", "mock:2")
        finally:
            follow.cancel()
            await d.stop()
        assert not d.actions
        assert m.powerup == {"mock:1": "on", "mock:2": "on"}  # shutdown_arm ran
        assert m.applied["mock:2"] == 50.0  # given back before the stop
    asyncio.run(main())


def test_mesh_transition_time_byte():
    from haus.backends.mesh import transition_time
    assert transition_time(4.0) == 0x28  # 40 x 100 ms: the test's fade
    assert transition_time(0.2) == 0x02
    assert transition_time(10.0) == 0x40 | 10  # 1 s steps
    assert transition_time(100.0) == 0x80 | 10  # 10 s steps
