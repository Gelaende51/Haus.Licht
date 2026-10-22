"""Kasa/Tapo backend against python-kasa's own fake protocols and real device
dumps (tests/kasa_fakes, copied from python-kasa): a KL110 (IOT) and an L530E
(SMART/Tapo). No network."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("kasa")
FAKES = Path(__file__).parent / "kasa_fakes"
sys.path.insert(0, str(FAKES))

from fakeprotocol_iot import FakeIotProtocol  # noqa: E402
from fakeprotocol_smart import FakeSmartProtocol  # noqa: E402
from kasa import Module  # noqa: E402
from kasa.exceptions import AuthenticationError  # noqa: E402
from kasa.iot import IotBulb  # noqa: E402
from kasa.smart import SmartDevice  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends.kasa import KasaBackend  # noqa: E402
from haus.common import UserError  # noqa: E402
from lan_fakes import FakeCtx, bulb  # noqa: E402


def fixture(name):
    return json.loads((FAKES / name).read_text())


def iot_device(color=False):
    fx = fixture("KL110.json")
    if color:  # a KL130: the same dump with the colour flags (python-kasa reads its range by model)
        fx["system"]["get_sysinfo"].update(model="KL130(EU)", is_color=1, is_variable_color_temp=1)
    proto = FakeIotProtocol(fx, "KL130" if color else "KL110")
    store = {}

    def set_default_behavior(self, params, child_ids):
        store.update(params)
        return {}

    def get_default_behavior(self, params, child_ids):
        return store or {"soft_on": {"mode": "last_status"}, "hard_on": {"mode": "last_status"}}

    svc = proto._transport.proto["smartlife.iot.smartbulb.lightingservice"]
    svc["set_default_behavior"] = set_default_behavior
    svc["get_default_behavior"] = get_default_behavior
    dev = IotBulb("127.0.0.123", protocol=proto)
    dev.behavior = store
    return dev


def smart_device():
    return SmartDevice("127.0.0.124", protocol=FakeSmartProtocol(fixture("L530E.json"), "L530E"))


def backend(make):
    be = KasaBackend(FakeCtx())

    async def _open(config):
        if config.get("refuse"):
            raise AuthenticationError("bad credentials")
        dev = make()
        await dev.update()
        be.opened = dev
        return dev

    be._open = _open
    return be


def run(coro):
    return asyncio.run(coro)


def test_iot_bulb_add_apply_powerup():
    async def main():
        be = backend(iot_device)
        config = await be.add({"host": "kl110.test"})
        assert config["family"] == "iot" and config["model"].startswith("KL110")
        b = bulb("kasa", config.get("id", "1"), **config)
        assert be.caps(b) == {"onoff", "dim", "powerup"}  # a white KL110: no colour
        assert config["color"] == []
        assert await be.apply_color(b, {"mode": "ct", "kelvin": 3000}) is False
        await be.apply(b, 42.3)
        dev = be.devices[b.id]
        await dev.update()
        assert dev.is_on and dev.modules[Module.Light].brightness == 42
        await be.apply(b, 0)
        await dev.update()
        assert not dev.is_on
        assert await be.query(b) == {"on": False, "percent": 0.0}

        assert await be.set_powerup(b, "restore")
        assert dev.behavior["hard_on"] == {"mode": "last_status"}
        assert await be.set_powerup(b, "on")
        assert dev.behavior["hard_on"]["mode"] == "customize_preset"
        assert await be.set_powerup(b, "off") is False  # Kasa cannot stay off
        assert be.status(b)["online"] is True
        await be.stop()
    run(main())


def test_tapo_bulb_add_apply():
    async def main():
        be = backend(smart_device)
        config = await be.add({"host": "l530.test", "username": "u@example.org", "password": "x"})
        assert config["family"] == "smart"
        b = bulb("kasa", config.get("id", "2"), **config)
        assert be.caps(b) == {"onoff", "dim", "ct", "hs"}
        assert be.ct_range(b) == (2500, 6500)
        await be.apply(b, 0)
        dev = be.devices[b.id]
        await dev.update()
        assert not dev.is_on
        await be.apply(b, 7.6)
        await dev.update()
        assert dev.is_on and dev.modules[Module.Light].brightness == 8
        assert await be.set_powerup(b, "on") is False  # no typed Tapo API: not offered
        await be.stop()
    run(main())


def test_refused_account_and_offline():
    async def main():
        be = backend(iot_device)
        with pytest.raises(UserError, match="TP-Link account"):
            await be.add({"host": "x", "refuse": True})

        async def gone(config):
            raise OSError("no route to host")

        be._open = gone
        b = bulb("kasa", "z", host="gone.test", family="iot")
        with pytest.raises(Offline):
            await be.apply(b, 10)
        assert be.status(b) == {"connected": False, "online": False}
    run(main())


def test_discover_uses_library_broadcast(monkeypatch):
    import kasa

    async def fake_discover(**kwargs):
        dev = iot_device()
        await dev.update()
        return {"192.0.2.10": dev}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(fake_discover))
    found = run(KasaBackend(FakeCtx()).discover(0.1))
    assert found[0]["kind"] == "kasa" and found[0]["config"] == {"host": "192.0.2.10"}


def spy(dev):
    sent = []
    real = dev.protocol.query

    async def query(request, *args, **kwargs):
        sent.append(request)
        return await real(request, *args, **kwargs)

    dev.protocol.query = query
    return sent


def test_tapo_colour_messages_keep_level_and_dark():
    async def main():
        be = backend(smart_device)
        config = await be.add({"host": "l530.test", "username": "u", "password": "x"})
        b = bulb("kasa", "2", **config)
        assert await be.apply_color(b, {"mode": "hs", "hue": 10, "sat": 10}) is False  # not lit yet
        await be.apply(b, 30)
        dev = be.devices[b.id]
        sent = spy(dev)
        assert await be.apply_color(b, {"mode": "hs", "hue": 359.7, "sat": 60.4})
        assert await be.apply_color(b, {"mode": "ct", "kelvin": 9000})  # clamped to 6500
        sets = [r for r in sent if "set_device_info" in str(r)]
        assert "{'color_temp': 0, 'hue': 0, 'saturation': 60}" in str(sets[0])
        assert "{'color_temp': 6500}" in str(sets[1])
        assert all("device_on" not in str(r) and "brightness" not in str(r) for r in sets)
        await dev.update()
        light = dev.modules[Module.Light]
        assert dev.is_on and light.brightness == 30 and light.color_temp == 6500
        await be.apply(b, 0)
        n = len(sent)
        assert await be.apply_color(b, {"mode": "hs", "hue": 200, "sat": 50}) is False  # dark: nothing
        assert len(sent) == n
        await be.stop()
    run(main())


def test_iot_colour_carries_the_level():
    async def main():
        be = backend(lambda: iot_device(color=True))
        config = await be.add({"host": "kl130.test"})
        b = bulb("kasa", "3", **config)
        assert be.caps(b) == {"onoff", "dim", "powerup", "ct", "hs"} and be.ct_range(b) == (2500, 9000)
        await be.apply(b, 42)
        dev = be.devices[b.id]
        sent = spy(dev)
        assert await be.apply_color(b, {"mode": "hs", "hue": 120, "sat": 100})
        assert await be.apply_color(b, {"mode": "ct", "kelvin": 4000})
        text = [str(r) for r in sent if "transition_light_state" in str(r)]
        assert "'hue': 120, 'saturation': 100, 'color_temp': 0, 'brightness': 42" in text[0]
        assert "'color_temp': 4000, 'brightness': 42" in text[1]
        await dev.update()
        assert dev.modules[Module.Light].brightness == 42
        await be.stop()
    run(main())


def test_old_config_learns_colour_on_first_open():
    async def main():
        be = backend(smart_device)
        b = bulb("kasa", "4", host="l530.test", family="smart")  # added before colour existed
        assert be.caps(b) == {"onoff", "dim"}
        await be.apply(b, 20)
        assert b.config["color"] == ["ct", "hs"] and b.config["kelvin"] == [2500, 6500]
        assert be.caps(b) == {"onoff", "dim", "ct", "hs"}
        await be.stop()
    run(main())
