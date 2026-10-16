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
    assert b.caps(bulb) == {"onoff", "dim"}  # no local power-on setting
    assert b.min_lit(bulb) == 10.0
    assert all(f["label"] and f["label_de"] for f in b.fields)
