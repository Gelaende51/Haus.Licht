"""WiZ backend against pywizlight's own fake UDP bulb (127.0.0.1 only)."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pywizlight")

from pywizlight.tests.fake_bulb import startup_bulb  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends import _bulbkit, wiz  # noqa: E402
from haus.common import UserError  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.events = []
        self.items = []

    def bulbs(self):
        return self.items

    def flick(self, bid):
        self.events.append(("flick", bid))

    def power_returned(self, bid, gap):
        self.events.append(("returned", bid, gap))

    def changed(self):
        self.events.append(("changed",))

    def save(self):
        self.events.append(("save",))


def test_wiz_add_apply_query_offline_return(monkeypatch):
    monkeypatch.setattr(_bulbkit, "PROBE_S", 0.05)

    async def main():
        shutdown, port = await startup_bulb(module_name="ESP05_SHDW_21", firmware_version="1.25.0")
        ctx = FakeCtx()
        b = wiz.WizBackend(ctx)
        b.presence.interval = 0.05
        cfg = await b.add({"host": "127.0.0.1", "port": port})
        assert cfg["id"] == cfg["mac"] and len(cfg["id"]) == 12
        bulb = SimpleNamespace(id=f"wiz:{cfg['id']}", name="W", config=cfg)
        ctx.items.append(bulb)
        assert b.status(bulb)["online"] is None

        await b.apply(bulb, 47.4)
        assert await b.query(bulb) == {"on": True, "percent": 47}
        assert b.status(bulb) == {"connected": True, "online": True}
        assert ("returned", bulb.id, None) in ctx.events  # first contact

        await b.apply(bulb, 2)  # below the firmware's 10
        assert (await b.query(bulb))["percent"] == 10
        await b.apply(bulb, 0)
        assert await b.query(bulb) == {"on": False, "percent": 0}

        shutdown()
        with pytest.raises(Offline):
            await b.apply(bulb, 30)
        assert b.status(bulb)["online"] is False
        with pytest.raises(Offline):  # known gone: no new attempt
            await b.apply(bulb, 30)

        shutdown2, port2 = await startup_bulb(module_name="ESP05_SHDW_21", firmware_version="1.25.0")
        bulb.config["port"] = port2  # "the same bulb" back (the fake can't reuse its port)
        await b.lights.pop(bulb.id).async_close()
        for _ in range(100):
            if b.status(bulb)["online"]:
                break
            await asyncio.sleep(0.05)
        assert b.status(bulb)["online"] is True
        # no reboot evidence over WiZ: a return is no power_returned with a gap
        assert not [e for e in ctx.events if e[0] == "returned" and e[2] is not None]
        await b.apply(bulb, 60)
        assert (await b.query(bulb))["percent"] == 60
        await b.stop()
        shutdown2()

    asyncio.run(main())


def test_wiz_add_refuses_silence():
    async def main():
        b = wiz.WizBackend(FakeCtx())
        shutdown, port = await startup_bulb()
        shutdown()  # nobody there
        with pytest.raises(UserError):
            await b.add({"host": "127.0.0.1", "port": port})

    asyncio.run(main())


def test_wiz_discover_uses_library(monkeypatch):
    from pywizlight import discovery
    seen = {}

    async def fake_find(wait_time, broadcast_address):
        seen.update(wait=wait_time, addr=broadcast_address)
        return [SimpleNamespace(ip_address="10.0.0.7", mac_address="a8bb50aabbcc")]

    monkeypatch.setattr(discovery, "find_wizlights", fake_find)
    found = asyncio.run(wiz.WizBackend(FakeCtx()).discover(0.1))
    assert found == [{"kind": "wiz", "address": "10.0.0.7", "name": "WiZ a8bb50aabbcc",
                      "config": {"host": "10.0.0.7", "mac": "a8bb50aabbcc"}}]
    assert seen == {"wait": 0.1, "addr": "255.255.255.255"}


def test_wiz_contract_attributes():
    b = wiz.WizBackend(FakeCtx())
    bulb = SimpleNamespace(id="wiz:1", config={})
    assert b.caps(bulb) == {"onoff", "dim"}  # no local power-on setting; an old config: no colour yet
    assert b.min_lit(bulb) == 10.0
    assert all(f["label"] and f["label_de"] for f in b.fields)


@pytest.mark.parametrize("module, fw, caps, kelvin", [
    ("ESP05_SHDW_21", "1.25.0", set(), None),               # dimmable white
    ("ESP21_SHTW_01", "1.25.0", {"ct"}, (2700, 5000)),      # tunable white: its cctRange
    ("ESP01_SHRGB_03", "1.25.0", {"ct", "hs"}, (2200, 6500)),
])
def test_wiz_colour_caps_per_bulb_type(module, fw, caps, kelvin):
    async def main():
        shutdown, port = await startup_bulb(module_name=module, firmware_version=fw)
        b = wiz.WizBackend(FakeCtx())
        cfg = await b.add({"host": "127.0.0.1", "port": port})
        bulb = SimpleNamespace(id="wiz:1", name="W", config=cfg)
        assert b.caps(bulb) == {"onoff", "dim"} | caps
        assert b.ct_range(bulb) == (kelvin or wiz.CT_RANGE)
        await b.stop()
        shutdown()

    asyncio.run(main())


def test_wiz_colour_messages_keep_level_and_never_switch_on():
    async def main():
        shutdown, port = await startup_bulb(module_name="ESP01_SHRGB_03", firmware_version="1.25.0")
        ctx = FakeCtx()
        b = wiz.WizBackend(ctx)
        # an older config without `color`: the bulb type is asked once, on the first contact
        bulb = SimpleNamespace(id="wiz:1", name="W", config={"host": "127.0.0.1", "port": port})
        sent = []
        real = b._send

        async def spy(bl, params):
            sent.append(dict(params))
            return await real(bl, params)

        b._send = spy
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 3000}) is False  # never lit: nothing sent
        assert sent == []
        await b.apply(bulb, 40)
        assert bulb.config["color"] == ["ct", "hs"] and bulb.config["kelvin"] == [2200, 6500]
        assert ("save",) in ctx.events
        sent.clear()
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 120, "sat": 100})
        assert sent == [{"r": 0, "g": 255, "b": 0, "w": 0, "dimming": 40}]  # PilotBuilder hucolor
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 120, "sat": 100})  # unchanged: not again
        assert len(sent) == 1
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 1500})  # clamped to the bulb's range
        assert sent[-1] == {"temp": 2200, "dimming": 40}
        state = (await real(bulb, {}))["result"]
        assert state["state"] is True and state["dimming"] == 40 and state["temp"] == 2200
        await b.apply(bulb, 0)
        n = len(sent)
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 4000}) is False  # dark: stays dark
        assert len(sent) == n and (await real(bulb, {}))["result"]["state"] is False
        await b.stop()
        shutdown()

    asyncio.run(main())
