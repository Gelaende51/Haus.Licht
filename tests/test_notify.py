"""Notification blinks: rules (haus/profiles.py), the watcher (haus/notifywatch.py),
the notify action (haus/actions.py) and the core's ctl commands."""

import asyncio
import json
import logging
import time

import pytest
from dbus_fast import Message, MessageType

from haus import actions, profiles
from haus.common import UserError
from haus.notifywatch import NotifyWatcher
from haus.profiles import (NOTIFY_MIN_STEP_S, NOTIFY_PATTERNS, check_notify_rules, notify_match,
                           notify_steps)
from test_core import cmd, mock, mock_state, run, settle

SECRET_TITLE = "Dinner with Alice at 8"
SECRET_BODY = "the door code is 4711"


@pytest.fixture
def fast(monkeypatch):
    """Short timings for the tests that are not about timing."""
    step = 0.02
    monkeypatch.setattr(profiles, "NOTIFY_MIN_STEP_S", step)
    monkeypatch.setattr(profiles, "NOTIFY_HOLD_S", {p: step for p in NOTIFY_PATTERNS})
    monkeypatch.setattr(profiles, "NOTIFY_DOUBLE_GAP_S", step)
    monkeypatch.setattr(actions, "NOTIFY_MIN_STEP_S", step)


def rule(name, **kw):
    return {"name": name, **kw}


def lit(**extra):
    return {"power": True, **extra}


async def until(cond, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def applied(m, bid, since=0):
    return [c[2] for c in m.calls[since:] if c[1] == bid and c[0] == "apply"]


def notify_msg(app="Thunderbird", title=SECRET_TITLE, body=SECRET_BODY):
    return Message(message_type=MessageType.METHOD_CALL, destination="org.freedesktop.Notifications",
                   path="/org/freedesktop/Notifications", interface="org.freedesktop.Notifications",
                   member="Notify", signature="susssasa{sv}i",
                   body=[app, 0, "", title, body, [], {}, -1])


async def set_rules(d, rules, w=None):
    args = [json.dumps(rules)]
    return await cmd(d, *(["@" + str(w), "notify-rules-set"] if w else ["notify-rules-set"]), *args)


# ------------------------------------------------------------ rules (pure)

def test_first_match_wins_regexes_case_insensitive_empty_is_any():
    rules = check_notify_rules([
        rule("mail", app="^thunder", title="dinner"),
        rule("any thunderbird", app="Thunderbird"),
        rule("door", body=r"code is \d+"),
        rule("everything"),
    ])
    assert notify_match(rules, "Thunderbird", "DINNER at 8", "")["name"] == "mail"
    assert notify_match(rules, "thunderbird", "Invoice", "")["name"] == "any thunderbird"
    assert notify_match(rules, "Signal", "Hi", "the CODE IS 12")["name"] == "door"
    assert notify_match(rules, "Signal", "Hi", "")["name"] == "everything"
    assert notify_match(rules[:3], "Signal", "Hi", "") is None


def test_rule_defaults_and_validation():
    (r,) = check_notify_rules([rule("x")])
    assert r == {"name": "x", "app": "", "title": "", "body": "", "pattern": "blink", "count": 2,
                 "intensity": 100.0, "color": "", "bulbs": "all", "dark": False}
    for bad in ([rule("x", title="(unclosed")], [rule("x", app="[a-")], [rule("")], [rule("x", count=11)],
                [rule("x", count=0)], [rule("x", count=1.5)], [rule("x", pattern="strobe")],
                [rule("x", intensity=0)], [rule("x", color="red")], [rule("x", bulbs="some")],
                [rule("x"), rule("X")], "not a list", [42]):
        with pytest.raises(ValueError):
            check_notify_rules(bad)
    with pytest.raises(ValueError, match="title"):
        check_notify_rules([rule("x", title="(unclosed")])


@pytest.mark.parametrize("pattern", NOTIFY_PATTERNS)
@pytest.mark.parametrize("count", [1, 3, 10])
def test_pattern_steps_never_faster_than_three_per_second(pattern, count):
    steps = notify_steps(pattern, count, 100.0, 1.0)
    assert all(hold >= 1 / 3 for _level, hold in steps)
    times, t = [], 0.0
    for _level, hold in steps:
        times.append(t)
        t += hold
    times.append(t)  # the level given back
    assert all(sum(1 for x in times if s <= x < s + 1.0) <= 3 for s in times)
    assert NOTIFY_MIN_STEP_S >= 1 / 3


# ------------------------------------------------------------ the core

def test_invalid_regex_rejected_and_rules_kept(tmp_path, fast):
    async def body(d):
        await set_rules(d, [rule("ok", title="x")])
        with pytest.raises(UserError, match="bad.*title"):  # rule and field named (any language)
            await set_rules(d, [rule("bad", title="(oops")])
        with pytest.raises(UserError):
            await cmd(d, "notify-rules-set", "{not json")
        got = await cmd(d, "notify-rules")
        assert got["profile"] == "default" and [r["name"] for r in got["rules"]] == ["ok"]
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_lit_bulbs_dim_dark_bulbs_skipped_unless_allowed(tmp_path, fast):
    async def body(d):
        m = mock(d)
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("night", count=2, intensity=80)])
        i0 = len(m.calls)
        assert d.on_notification("App", "t", "b") == ["mock:1"]  # mock:2 is dark: left dark
        await until(lambda: not d.actions)
        assert applied(m, "mock:1", i0) == [80.0, 1.0, 80.0, 1.0, 50.0]  # dims to the lowest lit level, then back
        assert applied(m, "mock:2", i0) == []
        await set_rules(d, [rule("night", count=1, dark=True)])
        i0 = len(m.calls)
        assert sorted(d.on_notification("App", "t", "b")) == ["mock:1", "mock:2"]
        await until(lambda: not d.actions)
        assert applied(m, "mock:2", i0) == [100.0, 0.0, 0.0]  # dark: on, off, given back dark
    run(tmp_path, mock_state(mock_1=lit(), mock_2={"power": False}), body)


def test_switch_off_means_nothing(tmp_path, fast):
    async def body(d):
        await set_rules(d, [rule("all")])
        assert d.on_notification("App", "t", "b") == []
        assert not d.actions
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_timings_live_with_colour_at_most_three_changes_per_second(tmp_path):
    async def body(d):
        m = mock(d)
        stamps = []
        for name in ("apply", "apply_color"):
            orig = getattr(m, name)

            async def wrapped(bulb, value, _orig=orig):
                stamps.append(time.monotonic())
                return await _orig(bulb, value)
            setattr(m, name, wrapped)
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("red", pattern="double", count=2, color="#ff0000")])
        await settle(d)
        stamps.clear()
        i0 = len(m.calls)
        d.on_notification("App", "t", "b")
        await until(lambda: not d.actions, timeout=10)
        colors = [c[2] for c in m.calls[i0:] if c[0] == "color"]
        assert colors[0]["hue"] == 0.0 and colors[0]["sat"] == 100.0
        assert colors[-1] == d.color(d.bulbs["mock:1"])  # the colour given back
        assert len(stamps) == 2 + 8 + 1  # colour, 8 levels, level given back (colour back: the 2nd colour)
        assert all(b - a >= 1 / 3 - 0.01 for a, b in zip(stamps, stamps[1:]))
        assert all(sum(1 for x in stamps if s <= x < s + 1.0) <= 3 for s in stamps)
    run(tmp_path, mock_state(mock_1=lit(config={"color": "hs"})), body)


def test_inhibited_means_no_blink(tmp_path, fast):
    async def body(d):
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("all")])
        w = NotifyWatcher(d.on_notification)
        w.inhibited = True  # Do Not Disturb (no bus: the last reading stands)
        assert w.handle(notify_msg()) is True  # a call: marked handled, never answered
        await asyncio.sleep(0.05)
        assert not d.actions
        w.inhibited = False
        w.handle(notify_msg())
        await until(lambda: "mock:1" in d.actions)
        signal = Message(message_type=MessageType.SIGNAL, path="/x", interface="a.b", member="C")
        assert w.handle(signal) is None  # never swallows replies or signals
        await until(lambda: not d.actions)
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_a_running_notify_drops_new_ones(tmp_path, fast):
    async def body(d):
        m = mock(d)
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("all", count=3)])
        i0 = len(m.calls)
        assert d.on_notification("App", "t", "b") == ["mock:1"]
        assert d.on_notification("App", "t", "b") == []  # dropped, not queued
        await until(lambda: not d.actions)
        await asyncio.sleep(0.1)
        assert len(applied(m, "mock:1", i0)) == 3 * 2 + 1
    run(tmp_path, mock_state(mock_1=lit()), body)


def test_identify_and_test_are_never_interrupted_and_take_over(tmp_path, fast, monkeypatch):
    monkeypatch.setattr(actions, "TEST_BRIGHT_S", 5.0)

    async def body(d):
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("all", count=10)])
        await cmd(d, "identify", "mock:1")
        await cmd(d, "test", "mock:2")
        assert d.on_notification("App", "t", "b") == []
        assert d.actions["mock:1"].kind == "identify" and d.actions["mock:2"].kind == "test"
        await cmd(d, "identify", "mock:1", "off")
        await cmd(d, "test", "mock:2", "stop")
        await until(lambda: not d.actions)
        assert d.on_notification("App", "t", "b") == ["mock:1", "mock:2"]
        g = await cmd(d, "identify", "mock:1")  # Identify comes first: the blink gives way
        assert g["identifying"] == ["mock:1"]
        await cmd(d, "test", "mock:2")
        assert d.actions["mock:2"].kind == "test"
        await cmd(d, "identify", "mock:1", "off")
        await cmd(d, "test", "mock:2", "stop")
    run(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


def test_rules_are_per_profile(tmp_path, fast):
    state = mock_state(mock_1=lit(), mock_2=lit(profile="evening"))
    state["profiles"]["evening"] = dict(state["profiles"]["default"])

    async def body(d):
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("mail", app="thunderbird")])  # selection all: the default profile
        await cmd(d, "select", "mock:2")
        g = await cmd(d, "get")
        assert g["profile"] == "evening" and g["notify_rules"] == []
        await set_rules(d, [rule("chat", app="signal")])
        assert [r["name"] for r in d.profiles["default"]["notify_rules"]] == ["mail"]
        assert [r["name"] for r in d.profiles["evening"]["notify_rules"]] == ["chat"]
        assert d.on_notification("Thunderbird", "t", "b") == ["mock:1"]
        await until(lambda: not d.actions)
        assert d.on_notification("Signal", "t", "b") == ["mock:2"]
        await until(lambda: not d.actions)
        g = await cmd(d, "notify-test", "chat")
        assert g["notify_test"] == ["mock:1", "mock:2"]  # the rule's bulbs: all in use
        await until(lambda: not d.actions)
        with pytest.raises(UserError):
            await cmd(d, "notify-test", "mail")  # not in this profile
        saved = json.loads(d.state_path.read_text())
        assert [r["name"] for r in saved["profiles"]["evening"]["notify_rules"]] == ["chat"]
    run(tmp_path, state, body)


def test_selection_rule_blinks_only_the_selection(tmp_path, fast):
    async def body(d):
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("sel", bulbs="selection")])
        assert d.on_notification("App", "t", "b") == ["mock:1", "mock:2"]  # selection all
        await until(lambda: not d.actions)
        await cmd(d, "select", "mock:2")
        await set_rules(d, [rule("sel", bulbs="selection")])  # mock:2's profile is default too
        assert d.on_notification("App", "t", "b") == ["mock:2"]
        await until(lambda: not d.actions)
    run(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


def test_nothing_of_title_or_body_in_logs_status_or_state(tmp_path, fast, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(d):
        await cmd(d, "notify", "on")
        await set_rules(d, [rule("dinner", title="dinner")])
        w = NotifyWatcher(d.on_notification)
        w.inhibited = False
        w.handle(notify_msg())
        await until(lambda: d.notify_last is not None)
        await until(lambda: not d.actions)
        g = await cmd(d, "get")
        last = g["notify_status"]["last"]
        assert last["rule"] == "dinner" and last["app"] == "Thunderbird" and set(last) == {"rule", "app", "time"}
        everything = json.dumps(g) + d.state_path.read_text() + caplog.text
        everything += json.dumps(await cmd(d, "notify-status"))
        for secret in (SECRET_TITLE, SECRET_BODY, "Alice", "4711"):
            assert secret not in everything
        assert "Thunderbird" in caplog.text and "dinner" in caplog.text
    run(tmp_path, mock_state(mock_1=lit()), body)
