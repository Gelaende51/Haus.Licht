"""ESPHome backend with aioesphomeapi's APIClient replaced by a fake (the
library's real entity and state classes, no socket)."""

import asyncio

import pytest

pytest.importorskip("aioesphomeapi")
from aioesphomeapi import (  # noqa: E402
    APIConnectionError,
    DeviceInfo,
    InvalidEncryptionKeyAPIError,
    LightInfo,
    LightState,
    RequiresEncryptionAPIError,
    SwitchInfo,
)

from haus.backends import Offline  # noqa: E402
from haus.backends import esphome as esphome_mod  # noqa: E402
from haus.backends.esphome import EsphomeBackend  # noqa: E402
from haus.common import UserError  # noqa: E402
from lan_fakes import FakeCtx, bulb  # noqa: E402

KEY = "px7tsbK3C7bpXHr2OevEV2ZMg/FojJ0gHvebQJ0ITk8="


class FakeClient:
    def __init__(self, config, device):
        self.config = config
        self.device = device

    async def connect(self, on_stop=None, login=False):
        dev = self.device
        if dev.down:
            raise APIConnectionError("connection refused")
        if dev.key and not self.config.get("encryption_key"):
            raise RequiresEncryptionAPIError("needs encryption")
        if dev.key and self.config.get("encryption_key") != dev.key:
            raise InvalidEncryptionKeyAPIError("bad key")
        self.on_stop = on_stop
        dev.clients.append(self)

    async def device_info(self):
        return DeviceInfo(name="desk", mac_address="AA:BB:CC:00:11:22")

    async def list_entities_services(self):
        return list(self.device.entities), []

    def subscribe_states(self, cb):
        self.device.subscribers.append(cb)
        cb(LightState(key=7, state=self.device.on, brightness=self.device.level))

    def light_command(self, key, state=None, brightness=None, **kw):
        dev = self.device
        if dev.down:
            raise APIConnectionError("not connected")
        dev.commands.append((key, state, brightness))
        dev.on = bool(state)
        if brightness is not None:
            dev.level = brightness
        for cb in dev.subscribers:
            cb(LightState(key=key, state=dev.on, brightness=dev.level))

    async def disconnect(self, force=False):
        pass


class FakeDevice:
    def __init__(self, key=KEY, light=True):
        self.key = key
        self.down = False
        self.on = False
        self.level = 1.0
        self.commands = []
        self.clients = []
        self.subscribers = []
        self.entities = [SwitchInfo(key=3, object_id="relay", name="Relay")]
        if light:
            self.entities.append(LightInfo(key=7, object_id="desk_lamp", name="Desk lamp"))


def backend(dev, ctx=None):
    be = EsphomeBackend(ctx or FakeCtx())
    be._client = lambda config: FakeClient(config, dev)
    return be


def run(coro):
    return asyncio.run(coro)


def test_add_and_apply():
    async def main():
        dev = FakeDevice()
        be = backend(dev)
        with pytest.raises(UserError, match="encryption key"):
            await be.add({"host": "desk.local"})
        with pytest.raises(UserError, match="wrong API encryption key"):
            await be.add({"host": "desk.local", "encryption_key": "x" + KEY[1:]})
        config = await be.add({"host": "desk.local", "encryption_key": KEY, "port": 6053, "entity": ""})
        assert config["id"] == "aabbcc001122"
        assert not be.links  # add leaves no connection behind
        b = bulb("esphome", config["id"], **config)
        await be.apply(b, 25)
        assert dev.commands[-1] == (7, True, 0.25)
        await be.apply(b, 0)
        assert dev.commands[-1] == (7, False, None)
        assert await be.query(b) == {"on": False, "percent": 0.0}
        assert be.status(b) == {"connected": True, "online": True}
        assert be.caps(b) == {"onoff", "dim"} and await be.set_powerup(b, "on") is False
        await be.stop()
    run(main())


def test_entity_choice_and_no_light():
    async def main():
        be = backend(FakeDevice(light=False))
        with pytest.raises(UserError, match="no light entity"):
            await be.add({"host": "h", "encryption_key": KEY})
        be = backend(FakeDevice())
        with pytest.raises(UserError, match="'nope'"):
            await be.add({"host": "h", "encryption_key": KEY, "entity": "nope"})
        config = await be.add({"host": "h", "encryption_key": KEY, "entity": "desk_lamp"})
        assert config["id"] == "aabbcc001122-desk_lamp"
    run(main())


def test_drop_and_reconnect(monkeypatch):
    monkeypatch.setattr(esphome_mod, "RETRY_S", (0.01,))

    async def main():
        dev = FakeDevice()
        ctx = FakeCtx()
        be = backend(dev, ctx)
        b = bulb("esphome", "aabbcc001122", host="h", encryption_key=KEY, port=6053, entity="")
        ctx._bulbs.append(b)
        await be.start()
        assert be.status(b)["connected"]
        dev.down = True
        await dev.clients[-1].on_stop(False)
        assert be.status(b) == {"connected": False, "online": False}
        with pytest.raises(Offline):
            await be.apply(b, 50)
        dev.down = False
        for _ in range(50):
            await asyncio.sleep(0.01)
            if be.status(b)["connected"]:
                break
        assert be.status(b) == {"connected": True, "online": True}
        await be.apply(b, 50)
        assert dev.commands[-1] == (7, True, 0.5)
        await be.stop()

    run(main())


def test_unreachable_is_offline():
    async def main():
        dev = FakeDevice()
        dev.down = True
        be = backend(dev)
        b = bulb("esphome", "x", host="h", encryption_key=KEY)
        with pytest.raises(Offline):
            await be.apply(b, 10)
        assert be.status(b)["online"] is False
    run(main())


def test_discover(monkeypatch):
    async def fake_browse(types, timeout):
        assert types == ["_esphomelib._tcp.local."]
        return [{"type": types[0], "name": "desk", "addresses": ["192.0.2.30"], "port": 6053,
                 "server": "desk.local", "properties": {"friendly_name": "Desk lamp", "mac": "aabbcc001122"}}]

    monkeypatch.setattr(esphome_mod, "mdns_browse", fake_browse)
    found = run(EsphomeBackend(FakeCtx()).discover(0.1))
    assert found == [{"kind": "esphome", "address": "192.0.2.30", "name": "Desk lamp",
                      "config": {"host": "192.0.2.30"}}]
