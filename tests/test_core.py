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
        self.env = SimpleNamespace(night=False, day=True, scheme=None, scheme_name=None)
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
        assert {f["key"] for f in b["backends"][0]["fields"]} == {"min_lit", "address", "fail"}
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
