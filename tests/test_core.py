"""The core with mock bulbs and a fake desktop: selection, profiles, flicks."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from haus.common import UserError, _, save_state
from haus.daemon import Daemon
from haus.profiles import new_state


class FakeDesktop:
    def __init__(self) -> None:
        self.env = SimpleNamespace(night=False, day=True, scheme=None, scheme_name=None, screen_kelvin=None)
        self.screen = 50.0
        self.screen_on = True
        self.backlight = None
        self.closed = False
        self.slider_ready = False
        self.pushed: list[int] = []

    def screen_level(self) -> float:
        return self.screen

    def lid_closed(self) -> bool:
        return self.closed

    async def push_slider(self, value: int) -> bool:
        self.pushed.append(value)
        return False

    async def start(self, slider_value: int) -> None:
        pass

    def close(self) -> None:
        pass


def mock_state(**bulbs) -> dict:
    state = new_state()
    for bid, extra in bulbs.items():
        state["bulbs"][bid.replace("_", ":")] = {"kind": "mock", "name": bid, "profile": "default",
                                                 "power": False, "config": {}, **extra}
    state["settings"]["widget_bulbs"] = {"1": list(state["bulbs"])}
    return state


def run(tmp_path, state, body):
    path = tmp_path / "state.json"
    save_state(path, state)

    async def main():
        d = Daemon(path, tmp_path / "sock", desktop=FakeDesktop())
        await d.start()
        follow = asyncio.create_task(d.follow())
        try:
            await settle(d)
            return await body(d)
        finally:
            follow.cancel()
            await d.stop()

    return asyncio.run(main())


async def settle(d, rounds=3):
    for _ in range(rounds):
        d.wake.set()
        await asyncio.sleep(0.05)


async def cmd(d, cmd, *args, which=None):
    return (await d.command({"cmd": cmd, "args": list(args), "which": which}))["results"][0]


def mock(d):
    return d.backends["mock"]


def test_dark_bulbs_stay_dark_and_arm_on(tmp_path):
    async def body(d):
        m = mock(d)
        assert m.applied == {"mock:1": 0.0, "mock:2": 0.0}
        # dark: a flick would boot it lit, so OnPowerUp "on"
        assert m.powerup == {"mock:1": "on", "mock:2": "on"}
        g = await cmd(d, "get")
        assert g["power"] is False and g["bulbs"] == 0.0 and g["onpowerup"] == 1
        assert {b["id"] for b in g["bulbs_list"]} == {"mock:1", "mock:2"}
    run(tmp_path, mock_state(mock_1={}, mock_2={"config": {"min_lit": "0.5"}}), body)


def test_selection_on_off_toggle_and_persisted_power(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "select", "mock:1")
        g = await cmd(d, "on")
        await settle(d)
        assert m.applied["mock:1"] == 50.0 and m.applied["mock:2"] == 0.0
        assert g["selected"] == "mock:1" and g["power"] is True
        assert m.powerup["mock:1"] == "off"  # lit: a flick boots it dark
        saved = json.loads(d.state_path.read_text())
        assert saved["bulbs"]["mock:1"]["power"] is True and saved["bulbs"]["mock:2"]["power"] is False
        await cmd(d, "select", "all")
        await cmd(d, "toggle")  # one is on: all off
        await settle(d)
        assert m.applied == {"mock:1": 0.0, "mock:2": 0.0}
        await cmd(d, "toggle")  # none on: all on
        await settle(d)
        assert m.applied == {"mock:1": 50.0, "mock:2": 50.0}
        with pytest.raises(UserError):
            await cmd(d, "select", "mock:9")
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_profiles_per_bulb_and_all_means_default(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "on")
        await cmd(d, "select", "mock:1")
        await cmd(d, "profile", "save", "desk")
        await cmd(d, "bulb-set", "mock:1", "profile=desk", "name=Desk lamp")
        g = await cmd(d, "set", "range_hi=50")  # the selection's profile: desk
        await settle(d)
        assert g["profile"] == "desk" and g["range_hi"] == 50.0
        assert m.applied["mock:1"] == 100.0 and m.applied["mock:2"] == 50.0
        assert d.state["profiles"]["default"]["range_hi"] == 100.0
        await cmd(d, "select", "all")
        g = await cmd(d, "set", "level=20")
        assert g["profile"] == "default" and d.state["profiles"]["desk"]["level"] == 50.0
        await cmd(d, "profile", "rename", "desk", "reading")
        assert d.bulbs["mock:1"].profile == "reading"
        with pytest.raises(UserError):
            await cmd(d, "profile", "delete", "default")
        with pytest.raises(UserError):
            await cmd(d, "profile", "rename", "default", "x")
        await cmd(d, "profile", "delete", "reading")
        assert d.bulbs["mock:1"].profile == "default"
        assert d.bulbs["mock:1"].name == "Desk lamp"
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_preset_alias_apply_on_all_and_on_one_bulb(tmp_path):
    async def body(d):
        await cmd(d, "set", "curve=40")
        await cmd(d, "preset", "save", "soft evening")
        await cmd(d, "set", "curve=0")
        g = await cmd(d, "preset", "apply", "soft evening")  # "all": into default
        assert g["curve"] == 40.0 and g["presets"] == ["soft evening"]
        await cmd(d, "select", "mock:2")
        await cmd(d, "set", "curve=-10")  # mock:2 is on default
        await cmd(d, "profile", "apply", "soft evening")  # one bulb: it uses the profile
        assert d.bulbs["mock:2"].profile == "soft evening" and d.bulbs["mock:1"].profile == "default"
        g = await cmd(d, "get")
        assert g["profile"] == "soft evening" and g["curve"] == 40.0
        await cmd(d, "preset", "delete", "soft evening")
        assert d.bulbs["mock:2"].profile == "default"
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_flick_toggles_and_flick_off_means_on(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "mock-flick", "mock:1")
        await settle(d)
        assert d.bulbs["mock:1"].power is True and m.applied["mock:1"] == 50.0
        await cmd(d, "mock-flick", "mock:1")
        await settle(d)
        assert d.bulbs["mock:1"].power is False and m.applied["mock:1"] == 0.0
        g = await cmd(d, "set", "flick=off")
        await settle(d)
        assert g["flick"] is False and m.powerup["mock:1"] == "on"
        await cmd(d, "mock-flick", "mock:1")
        await cmd(d, "mock-flick", "mock:1")
        assert d.bulbs["mock:1"].power is True  # every power cycle: on
        await settle(d)
        assert m.powerup["mock:1"] == "on"  # lit, but flick mode off: "on" all the same
    run(tmp_path, mock_state(mock_1={}), body)


def test_power_returned_rules(tmp_path):
    async def body(d):
        b = d.bulbs["mock:1"]
        await cmd(d, "mock-return", "mock:1", "5")  # quick: a flick from off
        assert b.power is True
        await cmd(d, "mock-return", "mock:1", "5")
        assert b.power is False
        await cmd(d, "mock-return", "mock:1", "40")  # long outage: on
        assert b.power is True
        await cmd(d, "off")
        await cmd(d, "mock-return", "mock:1", "none")  # first contact: power as saved
        assert b.power is False
        await cmd(d, "set", "flick=off")
        await cmd(d, "mock-return", "mock:1", "5")
        assert b.power is True
    run(tmp_path, mock_state(mock_1={}), body)


def test_powerup_target_with_off_at_zero(tmp_path):
    async def body(d):
        m = mock(d)
        d.desktop.screen = 0.0
        await cmd(d, "set", "off_at_zero=on")
        await settle(d)
        # dark, and a toggle would compute 0 %: a flick must boot it dark
        assert m.powerup["mock:1"] == "off"
        d.desktop.screen = 60.0
        await settle(d)
        assert m.powerup["mock:1"] == "on"
    run(tmp_path, mock_state(mock_1={}), body)


def test_offline_bulbs_are_skipped_and_resent_on_return(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "mock-offline", "mock:1")
        await cmd(d, "on")
        await settle(d)
        assert m.applied["mock:1"] == 0.0 and m.applied["mock:2"] == 50.0
        g = await cmd(d, "get")
        assert g["online"] is False and g["connected"] is False
        await cmd(d, "mock-return", "mock:1", "60")
        await settle(d)
        assert m.applied["mock:1"] == 50.0
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_nudge_slider_and_manual(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "on")
        await cmd(d, "select", "mock:1")
        await cmd(d, "slider", "50")  # mode maximum: range_hi = 200 - 3 * 50
        assert d.state["profiles"]["default"]["range_hi"] == 50.0
        await cmd(d, "nudge", "-10")
        assert d.state["settings"]["slider"] == 40 and d.state["profiles"]["default"]["range_hi"] == 80.0
        await cmd(d, "mode", "off")
        await settle(d)
        before = m.applied["mock:1"]
        g = await cmd(d, "nudge", "5")
        await settle(d)
        assert g["following"] is False and m.applied["mock:1"] == pytest.approx(before + 5)
        assert m.applied["mock:2"] == pytest.approx(before)  # not selected
        r = (await d.command({"cmd": "dim", "args": ["0"], "which": "mock:2"}))["results"]
        assert r == [{"bulb": "mock:2", "percent": 0.0}]  # dim 0: off, no floor
        await cmd(d, "resume", which="all")
        await settle(d)
        assert not any(b.paused for b in d.bulbs.values())
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_add_remove_discover_backends(tmp_path):
    async def body(d):
        found = await cmd(d, "discover")
        assert [x["address"] for x in found["discovered"]] == ["mock-1", "mock-2"]
        g = await cmd(d, "add", "mock", "address=AA:BB", "name=Shelf", "min_lit=0.5")
        assert g["added"] == "mock:aabb"
        await settle(d)
        assert mock(d).applied["mock:aabb"] == 50.0  # a new bulb comes on
        with pytest.raises(UserError):
            await cmd(d, "add", "mock", "address=AA:BB")
        with pytest.raises(UserError):
            await cmd(d, "add", "mock", "fail=on")
        with pytest.raises(UserError):
            await cmd(d, "add", "mock", "min_lit=7")
        with pytest.raises(UserError):
            await cmd(d, "add", "hue")
        g = await cmd(d, "add", "mock")
        assert g["added"] == "mock:1"
        await cmd(d, "select", "mock:1")
        g = await cmd(d, "remove", "mock:1")
        assert g["selected"] == "all" and "mock:1" not in json.loads(d.state_path.read_text())["bulbs"]
        b = await cmd(d, "backends")
        assert b["backends"][0]["kind"] == "mock" and b["backends"][0]["available"] is True
        assert {f["key"] for f in b["backends"][0]["fields"]} == {"min_lit", "address", "color", "fail"}
    run(tmp_path, new_state(), body)


def test_lid_switches_every_bulb(tmp_path):
    async def body(d):
        await cmd(d, "select", "mock:1")
        d.desktop.closed = True
        await settle(d)
        assert not any(b.power for b in d.bulbs.values())
        d.desktop.closed = False
        await settle(d)
        assert all(b.power for b in d.bulbs.values())
    run(tmp_path, mock_state(mock_1={"power": True}, mock_2={"power": True}), body)


def test_get_keeps_the_old_fields(tmp_path):
    async def body(d):
        g = await cmd(d, "get")
        old = {"bulb", "following", "mode", "slider", "track", "range_lo", "range_hi", "curve", "level",
               "lid", "off_at_zero", "night", "tracking", "screen_on", "lid_closed", "power", "bulbs",
               "connected", "online", "onpowerup", "advert_age", "kbd_binds", "screen", "presets"}
        new = {"selected", "flick", "profile", "bulbs_list", "profiles", "backends"}
        assert old | new <= set(g)
        assert set(g["bulbs_list"][0]) >= {"id", "name", "kind", "profile", "power", "output", "connected",
                                           "online", "caps", "experimental"}
    run(tmp_path, mock_state(mock_1={}), body)


def test_socket_round_trip(tmp_path):
    async def body(d):
        sock = tmp_path / "s.sock"
        server = await asyncio.start_unix_server(d.handle_client, path=str(sock))
        async with server:
            async def ask(req):
                r, w = await asyncio.open_unix_connection(str(sock))
                w.write(json.dumps(req).encode() + b"\n")
                await w.drain()
                line = await r.readline()
                w.close()
                return json.loads(line)
            assert (await ask({"cmd": "get", "arg": None, "which": None}))["results"][0]["bulb"] == "sync"
            # the old widget's form: arg only
            assert "error" not in await ask({"cmd": "preset", "arg": "save Two words", "which": None})
            assert "Two words" in d.state["profiles"]
            err = await ask({"cmd": "profile", "arg": "delete default", "which": None})
            assert err["error"] == _("the default profile cannot be deleted or renamed")
    run(tmp_path, mock_state(mock_1={}), body)


async def connect(d, tmp_path, req):
    """A socket client of d (the server runs inside the test's loop)."""
    sock = tmp_path / "w.sock"
    if not sock.exists():
        d.test_server = await asyncio.start_unix_server(d.handle_client, path=str(sock))
    r, w = await asyncio.open_unix_connection(str(sock))
    w.write(json.dumps(req).encode() + b"\n")
    await w.drain()
    return r, w


async def next_line(r, timeout=2.0):
    return json.loads(await asyncio.wait_for(r.readline(), timeout))


async def until(r, event):
    while True:
        line = await next_line(r)
        if line["event"] == event:
            return line


def test_order_is_the_list_order(tmp_path):
    async def body(d):
        g = await cmd(d, "order", "mock:3", "mock:1", "mock:2")
        assert [b["id"] for b in g["bulbs_list"]] == ["mock:3", "mock:1", "mock:2"]
        assert g["in_widget"] == ["mock:3", "mock:1", "mock:2"]
        assert list(json.loads(d.state_path.read_text())["bulbs"]) == ["mock:3", "mock:1", "mock:2"]
        assert d.state["bulbs"] is d.backends["mock"].ctx.state["bulbs"]  # reordered in place
        for bad in (["mock:1", "mock:2"], ["mock:1", "mock:1", "mock:2"], ["mock:1", "mock:2", "mock:9"]):
            with pytest.raises(UserError):
                await cmd(d, "order", *bad)
        assert list(d.bulbs) == ["mock:3", "mock:1", "mock:2"]
    run(tmp_path, mock_state(mock_1={}, mock_2={}, mock_3={}), body)


def test_a_bulb_not_in_use_is_idle(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "on")
        await cmd(d, "select", "mock:2")
        await settle(d)
        assert m.applied == {"mock:1": 50.0, "mock:2": 50.0} and m.powerup["mock:2"] == "off"
        with pytest.raises(UserError):
            await cmd(d, "bulb-set", "mock:2", "use=maybe")
        g = await cmd(d, "bulb-set", "mock:2", "use=off")
        await settle(d)
        assert g["selected"] == "all" and g["in_widget"] == ["mock:1"]
        assert [b["use"] for b in g["bulbs_list"]] == [True, False]  # still listed for the settings
        assert m.powerup["mock:2"] == "on"  # idle: a power cut boots it lit, as any bulb
        assert d.state["settings"]["widget_bulbs"]["1"] == ["mock:1", "mock:2"]  # kept for use=on
        before = [c for c in m.calls if c[1] == "mock:2"]
        await cmd(d, "toggle")  # "all" is mock:1 alone
        await cmd(d, "set", "mode=off")
        d.desktop.screen = 80.0
        d.desktop.closed = True
        await settle(d)
        assert d.bulbs["mock:2"].power is True and d.bulbs["mock:1"].power is False
        with pytest.raises(UserError):
            await cmd(d, "on", which="mock:2")
        with pytest.raises(UserError):
            await cmd(d, "select", "mock:2")
        await cmd(d, "mock-flick", "mock:2")  # idle: not toggled
        await cmd(d, "mock-return", "mock:2", "3")
        await settle(d)
        assert d.bulbs["mock:2"].power is True
        after = [c for c in m.calls if c[1] == "mock:2"][len(before):]
        assert after == [("powerup", "mock:2", "on")]  # a new link: OnPowerUp checked again, no level
        assert ("idle", None, True) not in m.calls  # mock:1 is still driven
        await cmd(d, "bulb-set", "mock:1", "use=off")
        await settle(d)
        assert ("idle", None, True) in m.calls  # every mock bulb idle: the backend may let go
        d.desktop.closed = False
        await settle(d)
        g = await cmd(d, "bulb-set", "mock:2", "use=on")
        assert g["in_widget"] == ["mock:2"]  # back in its widget
        await settle(d)
        back = m.calls.index(("idle", None, False))
        assert back < m.calls.index(("apply", "mock:2", 80.0))  # told before the first send
        assert m.applied["mock:2"] == 80.0  # driven again, as before (lid open switched nothing idle)
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_an_unconfirmed_idle_arm_is_retried(tmp_path, monkeypatch):
    import haus.daemon
    monkeypatch.setattr(haus.daemon, "ARM_RETRY_S", 0.5)

    async def body(d):
        m = mock(d)
        answers = [False, True]
        real = m.set_powerup

        async def flaky(bulb, value):
            ok = await real(bulb, value)
            return answers.pop(0) if answers else ok

        m.set_powerup = flaky
        await cmd(d, "bulb-set", "mock:1", "use=off")
        await settle(d)
        assert d.bulbs["mock:1"].armed is None and ("idle", None, True) not in m.calls  # link kept
        await asyncio.sleep(0.5)
        await settle(d)
        assert d.bulbs["mock:1"].armed == "on" and ("idle", None, True) in m.calls
    run(tmp_path, mock_state(mock_1={"power": True}), body)


def test_widget_lists(tmp_path):
    async def body(d):
        m = mock(d)
        with pytest.raises(UserError):
            await cmd(d, "set", "widgets=9")
        g = await cmd(d, "set", "widgets=3")
        assert g["widgets"] == 3 and d.state["settings"]["widget_bulbs"] == {
            "1": ["mock:1", "mock:2", "mock:3"], "2": [], "3": []}
        g = await cmd(d, "@3", "widget-bulbs", "mock:3,mock:1")
        assert (g["widget"], g["in_widget"]) == (3, ["mock:1", "mock:3"])  # in list order
        with pytest.raises(UserError):
            await cmd(d, "@3", "widget-bulbs", "mock:1,mock:9")
        g = await cmd(d, "@1", "widget-bulbs", "mock:2")
        assert g["in_widget"] == ["mock:2"]
        await cmd(d, "on")  # widget 1's all: mock:2
        await settle(d)
        # membership does not decide driving: mock:1 and mock:3 are still driven by their profile
        assert m.applied == {"mock:1": 0.0, "mock:2": 50.0, "mock:3": 0.0}
        d.desktop.screen = 70.0
        await settle(d)
        assert m.applied["mock:2"] == 70.0
        await cmd(d, "order", "mock:3", "mock:2", "mock:1")
        assert d.state["settings"]["widget_bulbs"]["3"] == ["mock:3", "mock:1"]
        await cmd(d, "bulb-set", "mock:1", "use=off")
        g = await cmd(d, "@3", "widget-bulbs", "mock:3")
        assert g["in_widget"] == ["mock:3"]
        assert d.state["settings"]["widget_bulbs"]["3"] == ["mock:3", "mock:1"]  # unused: kept
        g = await cmd(d, "@3", "widget-bulbs")
        assert g["in_widget"] == [] and d.state["settings"]["widget_bulbs"]["3"] == ["mock:1"]
        await cmd(d, "@2", "select", "all")
        await cmd(d, "@2", "widget-bulbs", "mock:2")
        await cmd(d, "@2", "select", "mock:2")
        g = await cmd(d, "set", "widgets=1")
        assert d.state["settings"]["widget_bulbs"] == {"1": ["mock:2"]}
        assert d.state["settings"]["selected"] == {"1": "all"} and g["widgets"] == 1
        with pytest.raises(UserError):
            await cmd(d, "@2", "get")
        g = await cmd(d, "set", "widgets=2")
        assert d.state["settings"]["widget_bulbs"]["2"] == []  # the old list is gone
        await cmd(d, "remove", "mock:2")
        assert d.state["settings"]["widget_bulbs"] == {"1": [], "2": []}
    run(tmp_path, mock_state(mock_1={}, mock_2={}, mock_3={}), body)


def test_widget_context_and_selection(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "on")
        await cmd(d, "set", "widgets=2")
        await cmd(d, "@2", "widget-bulbs", "mock:2")
        with pytest.raises(UserError):
            await cmd(d, "@2", "select", "mock:1")  # not in widget 2
        g = await cmd(d, "@2", "select", "mock:2")
        assert (g["widget"], g["selected"], g["in_widget"]) == (2, "mock:2", ["mock:2"])
        g = await cmd(d, "get")
        assert (g["widget"], g["selected"], g["in_widget"]) == (1, "all", ["mock:1", "mock:2"])
        assert d.state["settings"]["selected"] == {"1": "all", "2": "mock:2"}
        await cmd(d, "@2", "profile", "save", "two")
        await cmd(d, "@2", "profile", "apply", "two")
        assert d.bulbs["mock:2"].profile == "two" and d.bulbs["mock:1"].profile == "default"
        # @2 nudge: the bound value (mode maximum: range_hi) of widget 2's selection, in slider
        # steps (100 is slider 33; 23 is 131), not the LED
        slider, pushed = d.settings["slider"], list(d.desktop.pushed)
        g = await cmd(d, "@2", "nudge", "-10")
        assert d.profiles["two"]["range_hi"] == 131.0 and d.profiles["default"]["range_hi"] == 100.0
        assert g["profile"] == "two" and d.settings["slider"] == slider and d.desktop.pushed == pushed
        await cmd(d, "@2", "off")
        await settle(d)
        assert d.bulbs["mock:2"].power is False and d.bulbs["mock:1"].power is True
        await cmd(d, "@2", "set", "level=20")
        assert d.profiles["two"]["level"] == 20.0 and d.profiles["default"]["level"] == 50.0
        await cmd(d, "@2", "on")
        await cmd(d, "mode", "off")
        await settle(d)
        before = m.applied["mock:2"]
        await cmd(d, "@2", "nudge", "5")  # mode off: the brightness of widget 2's selection
        await settle(d)
        assert m.applied["mock:2"] == pytest.approx(before + 5) and not d.bulbs["mock:1"].paused
        await cmd(d, "@2", "select", "all")
        await cmd(d, "@2", "toggle")  # widget 2's all: mock:2 alone
        assert d.bulbs["mock:2"].power is False and d.bulbs["mock:1"].power is True
        await cmd(d, "@2", "select", "mock:2")
        await cmd(d, "bulb-set", "mock:2", "use=off")  # an unused bulb is no selection
        assert d.state["settings"]["selected"] == {"1": "all", "2": "all"}
        await cmd(d, "bulb-set", "mock:2", "use=on")
        await cmd(d, "@2", "select", "mock:2")
        await cmd(d, "remove", "mock:2")
        assert d.state["settings"]["selected"] == {"1": "all", "2": "all"}
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_icons(tmp_path):
    async def body(d):
        g = await cmd(d, "bulb-set", "mock:1", "icon_on=lightbulb-on", "icon_off=/usr/share/icons/off lamp.svg")
        b = g["bulbs_list"][0]
        assert (b["icon_on"], b["icon_off"]) == ("lightbulb-on", "/usr/share/icons/off lamp.svg")
        with pytest.raises(UserError):
            await cmd(d, "bulb-set", "mock:1", "icon_on=icons/relative.svg")
        g = await cmd(d, "bulb-set", "mock:1", "icon_on=")
        assert g["bulbs_list"][0]["icon_on"] == ""
        assert json.loads(d.state_path.read_text())["bulbs"]["mock:1"]["icon_off"].endswith("off lamp.svg")
    run(tmp_path, mock_state(mock_1={}), body)


def test_new_bulbs_join_widget_1(tmp_path):
    async def body(d):
        await cmd(d, "set", "widgets=2")
        g = await cmd(d, "add", "mock")
        assert g["bulbs_list"][0]["use"] is True and g["in_widget"] == ["mock:1"]
        assert d.state["settings"]["widget_bulbs"] == {"1": ["mock:1"], "2": []}
    run(tmp_path, new_state(), body)


def test_watch_snapshot_intended_and_flick(tmp_path):
    async def body(d):
        r, w = await connect(d, tmp_path, {"cmd": "watch", "args": [], "which": None})
        snap = await next_line(r)
        assert snap["event"] == "snapshot" and snap["selected"] == "all" and snap["widget"] == 1
        assert snap["intended"] == {"mock:1": {"power": False, "percent": 0.0, "color": None}}
        await cmd(d, "on")
        line = await until(r, "intended")
        assert (line["bulb"], line["power"], line["percent"], line["color"]) == ("mock:1", True, 50.0, None)
        assert isinstance(line["t"], float)
        await cmd(d, "mock-flick", "mock:1")
        flick = await next_line(r)  # at once, before the new intended state
        assert (flick["event"], flick["bulb"], flick["power"]) == ("flick", "mock:1", False)
        line = await next_line(r)
        assert (line["event"], line["power"], line["percent"]) == ("intended", False, 0.0)
        await cmd(d, "mock-offline", "mock:1")
        assert (await until(r, "dropout"))["bulb"] == "mock:1"
        await cmd(d, "mock-return", "mock:1", "40")
        back = await until(r, "returned")
        assert (back["gap"], back["power"]) == (40.0, True)
        w.close()
        await settle(d)
        await cmd(d, "off")
        await settle(d)
        assert not d.watchers  # the client went: dropped
        d.test_server.close()
    run(tmp_path, mock_state(mock_1={}), body)


def test_a_stuck_watcher_never_blocks(tmp_path, monkeypatch):
    import haus.daemon
    monkeypatch.setattr(haus.daemon, "WATCH_QUEUE", 4)

    async def body(d):
        r, w = await connect(d, tmp_path, {"cmd": "watch", "args": [], "which": None})  # never read
        r2, w2 = await connect(d, tmp_path, {"cmd": "@1", "args": ["watch"], "which": None})
        await next_line(r2)
        got = 0

        async def keep_up():  # the second watcher reads everything
            nonlocal got
            while chunk := await r2.read(1 << 20):
                got += len(chunk)

        reading = asyncio.create_task(keep_up())
        for _ in range(60):  # big lines: the socket buffers fill, then the queue
            d.emit({"event": "test", "pad": "x" * 100_000})
            await asyncio.sleep(0.005)
            if len(d.watchers) == 1:
                break
        assert len(d.watchers) == 1  # the stuck one dropped, the reading one kept
        await asyncio.sleep(0.1)
        assert got > 1_000_000
        reading.cancel()
        g = await asyncio.wait_for(cmd(d, "on"), 1.0)
        assert g["power"] is True
        w.close()
        w2.close()
        d.test_server.close()
    run(tmp_path, mock_state(mock_1={}), body)


def colours(d, bid):
    return [c[2] for c in mock(d).calls if c[:2] == ("color", bid)]


def test_colour_sent_once_on_change_and_never_dark_or_idle(tmp_path):
    async def body(d):
        await settle(d)
        assert not [c for c in mock(d).calls if c[0] == "color"]  # dark: nothing
        await cmd(d, "on")
        await settle(d)
        assert colours(d, "mock:hs") == [{"mode": "hs", "hue": 30.0, "sat": 0.0}]
        assert colours(d, "mock:ct") == [] and colours(d, "mock:plain") == []  # white, not following
        await cmd(d, "set", "sat=80")
        await settle(d)
        await cmd(d, "set", "hue=30.4")  # under the step: not sent again
        await settle(d)
        assert colours(d, "mock:hs")[-1] == {"mode": "hs", "hue": 30.0, "sat": 80.0}
        await cmd(d, "set", "hue=200")
        d.desktop.screen = 60.0
        await settle(d)
        assert colours(d, "mock:hs") == [{"mode": "hs", "hue": 30.0, "sat": 0.0},
                                         {"mode": "hs", "hue": 30.0, "sat": 80.0},
                                         {"mode": "hs", "hue": 200.0, "sat": 80.0}]
        g = await cmd(d, "get")
        assert (g["hue"], g["sat"], g["follow_ct"], g["shift"], g["screen_kelvin"]) == (200.0, 80.0, False, 100.0, None)
        caps = {b["id"]: b["caps"] for b in g["bulbs_list"]}
        assert "hs" in caps["mock:hs"] and "ct" not in caps["mock:hs"] and "hs" not in caps["mock:plain"]
        assert (await cmd(d, "status", which="mock:hs"))["color"]["hue"] == 200.0
        await cmd(d, "off", which="mock:hs")
        await cmd(d, "set", "hue=100")
        await settle(d)
        assert colours(d, "mock:hs")[-1]["hue"] == 200.0  # off: not sent
        await cmd(d, "on", which="mock:hs")
        await settle(d)
        assert colours(d, "mock:hs")[-1] == {"mode": "hs", "hue": 100.0, "sat": 80.0}  # on power-on
        n = len(colours(d, "mock:hs"))
        await cmd(d, "mock-offline", "mock:hs")
        await cmd(d, "mock-return", "mock:hs", "none")
        await settle(d)
        assert len(colours(d, "mock:hs")) == n + 1  # resent after the return
        await cmd(d, "bulb-set", "mock:hs", "use=off")
        await cmd(d, "set", "hue=300")
        await settle(d)
        assert len(colours(d, "mock:hs")) == n + 1  # idle: never
    run(tmp_path, mock_state(mock_hs={"config": {"color": "hs"}}, mock_ct={"config": {"color": "ct"}},
                             mock_plain={}), body)


def test_night_light_change_wakes_and_resends(tmp_path):
    async def body(d):
        await cmd(d, "set", "follow_ct=on")
        await cmd(d, "on")
        await settle(d)
        # Night Light unknown: as if not following, the profile's hue/sat
        assert colours(d, "mock:ct") == colours(d, "mock:hs") == [{"mode": "hs", "hue": 30.0, "sat": 0.0}]
        d.desktop.env.screen_kelvin = 4000
        d.desktop.env.on_change()
        await asyncio.sleep(0.15)  # under the 0.3 s round: woken at once
        assert colours(d, "mock:ct")[1:] == [{"mode": "ct", "kelvin": 4000}]
        hs = colours(d, "mock:hs")[1:]
        assert len(hs) == 1 and hs[0]["mode"] == "hs" and 20 < hs[0]["hue"] < 40 and 30 < hs[0]["sat"] < 60
        d.desktop.env.screen_kelvin = 3990  # under the step
        d.desktop.env.on_change()
        await settle(d)
        assert len(colours(d, "mock:ct")) == 2
        await cmd(d, "set", "shift=200")
        await settle(d)
        assert colours(d, "mock:ct")[-1] == {"mode": "ct", "kelvin": 2878}
        g = await cmd(d, "get")
        assert g["screen_kelvin"] == 3990
        intended = d.intended_of(d.bulbs["mock:ct"])["color"]
        assert intended["kelvin"] == 2878 and intended["rgb"].startswith("#ff")
    run(tmp_path, mock_state(mock_hs={"config": {"color": "hs"}}, mock_ct={"config": {"color": "both"}}), body)


def test_shutdown_with_a_watch_client_connected(tmp_path):
    path, sock = tmp_path / "state.json", tmp_path / "x.sock"
    save_state(path, mock_state(mock_1={}))

    async def main():
        d = Daemon(path, sock, desktop=FakeDesktop())
        task = asyncio.create_task(d.run())
        for _ in range(100):
            if sock.exists():
                break
            await asyncio.sleep(0.02)
        r, w = await asyncio.open_unix_connection(str(sock))
        w.write(b'{"cmd": "watch"}\n')
        await w.drain()
        assert (await next_line(r))["event"] == "snapshot"
        task.cancel()  # what SIGTERM does
        assert await asyncio.wait_for(task, 3.0) == 0
        assert not sock.exists() and not d.watchers
        assert await asyncio.wait_for(r.read(), 1.0) == b""  # the client sees the end
        w.close()
        d2 = Daemon(path, sock, desktop=FakeDesktop())  # the lock is free again: a restart works
        task = asyncio.create_task(d2.run())
        await asyncio.sleep(0.2)
        task.cancel()
        assert await asyncio.wait_for(task, 3.0) == 0

    asyncio.run(main())
