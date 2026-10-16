"""MQTT backend against the in-process amqtt broker and a fake zigbee2mqtt."""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

aiomqtt = pytest.importorskip("aiomqtt")
pytest.importorskip("amqtt")
from amqtt.broker import Broker  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends.mqtt import MqttBackend  # noqa: E402
from haus.common import UserError  # noqa: E402

DEVICES = [
    {"friendly_name": "Coordinator", "type": "Coordinator", "definition": None},
    {"friendly_name": "Desk lamp", "definition": {"exposes": [
        {"type": "light", "features": [{"name": "state"}, {"name": "brightness", "value_max": 254}]},
        {"type": "enum", "name": "power_on_behavior", "property": "power_on_behavior",
         "values": ["off", "on", "toggle", "previous"]}]}},
    {"friendly_name": "Shelf", "definition": {"exposes": [
        {"type": "light", "features": [{"name": "state"}, {"name": "brightness"}]}]}},
    {"friendly_name": "Plug", "definition": {"exposes": [
        {"type": "switch", "features": [{"name": "state"}]}]}},
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]



@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "en")

class FakeCtx:
    def __init__(self) -> None:
        self.list, self.returns, self.changes = [], [], 0

    def bulbs(self):
        return self.list

    def changed(self):
        self.changes += 1

    def power_returned(self, bid, gap):
        self.returns.append((bid, gap))

    def flick(self, bid):
        pass

    def save(self):
        pass


async def fake_z2m(port: int, sets: list, ready: asyncio.Event, control: asyncio.Queue) -> None:
    states = {"Desk lamp": {"state": "OFF", "brightness": 1, "power_on_behavior": "on"}}
    async with aiomqtt.Client("127.0.0.1", port) as c:
        await c.publish("zigbee2mqtt/bridge/devices", json.dumps(DEVICES), retain=True)
        await c.publish("zigbee2mqtt/Desk lamp/availability", json.dumps({"state": "online"}), retain=True)
        await c.publish("zigbee2mqtt/Desk lamp", json.dumps(states["Desk lamp"]), retain=True)
        await c.subscribe("zigbee2mqtt/+/set")
        await c.subscribe("generic/#")
        ready.set()

        async def controls():
            while True:
                topic, payload = await control.get()
                await c.publish(topic, payload, retain=True)

        task = asyncio.create_task(controls())
        try:
            async for msg in c.messages:
                topic = str(msg.topic)
                sets.append((topic, msg.payload.decode()))
                if topic.startswith("zigbee2mqtt/"):
                    name = topic.split("/")[1]
                    states.setdefault(name, {}).update(json.loads(msg.payload))
                    states[name].pop("transition", None)
                    await c.publish(f"zigbee2mqtt/{name}", json.dumps(states[name]), retain=True)
        finally:
            task.cancel()


def run(body):
    async def main():
        port = free_port()
        broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                         "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
        await broker.start()
        sets, ready, control = [], asyncio.Event(), asyncio.Queue()
        z2m = asyncio.create_task(fake_z2m(port, sets, ready, control))
        await asyncio.wait_for(ready.wait(), 5)
        ctx = FakeCtx()
        backend = MqttBackend(ctx)
        try:
            return await body(backend, ctx, port, sets, control)
        finally:
            await backend.stop()
            z2m.cancel()
            await asyncio.gather(z2m, return_exceptions=True)
            await broker.shutdown()
    return asyncio.run(asyncio.wait_for(main(), 60))


def z2m_config(port, **kw):
    return {"mode": "zigbee2mqtt", "host": "127.0.0.1", "port": port, "username": "", "password": "",
            "tls": False, "base_topic": "zigbee2mqtt", "device": "", "command_topic": "", "payload": "{percent}",
            "off_payload": "OFF", "scale": 100, "availability_topic": "", **kw}


async def until(pred, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def test_z2m_add_apply_powerup_availability():
    async def body(backend, ctx, port, sets, control):
        with pytest.raises(UserError):
            await backend.add(z2m_config(port, device="Plug"))
        with pytest.raises(UserError):
            await backend.add(z2m_config(port, device="Nope"))
        config = await backend.add(z2m_config(port, device="Desk lamp"))
        assert config["id"] == "desk-lamp" and "command_topic" not in config
        bulb = SimpleNamespace(id="mqtt:desk-lamp", name="Desk", config=config)
        ctx.list.append(bulb)
        await backend.start()
        assert backend.caps(bulb) == {"onoff", "dim", "powerup"}
        assert await until(lambda: backend.status(bulb) == {"connected": True, "online": True})
        await backend.apply(bulb, 50)
        await backend.apply(bulb, 0)
        assert await until(lambda: len(sets) >= 2)
        assert json.loads(sets[0][1]) == {"state": "ON", "brightness": 127, "transition": 0.2}
        assert json.loads(sets[1][1])["state"] == "OFF"
        assert await backend.set_powerup(bulb, "restore") is True
        assert json.loads(sets[-1][1]) == {"power_on_behavior": "previous"}
        n = len(sets)
        assert await backend.set_powerup(bulb, "restore") is True  # already so: nothing sent
        assert len(sets) == n
        await backend.apply(bulb, 100)
        assert await until(lambda: backend.states.get((("127.0.0.1", port, "", "", False),
                                                       "zigbee2mqtt/Desk lamp"), {}).get("brightness") == 254)
        assert await backend.query(bulb) == {"on": True, "percent": 100.0}
        found = await backend.discover(2.0)
        assert [f["name"] for f in found] == ["Shelf"]
        # availability: offline, then back = a power return
        await control.put(("zigbee2mqtt/Desk lamp/availability", json.dumps({"state": "offline"})))
        assert await until(lambda: backend.status(bulb)["online"] is False)
        with pytest.raises(Offline):
            await backend.apply(bulb, 30)
        await control.put(("zigbee2mqtt/Desk lamp/availability", "online"))
        assert await until(lambda: backend.status(bulb)["online"] is True)
        assert len(ctx.returns) == 1 and ctx.returns[0][0] == bulb.id
    run(body)


def test_z2m_rate_limit():
    async def body(backend, ctx, port, sets, control):
        config = await backend.add(z2m_config(port, device="Shelf"))
        bulb = SimpleNamespace(id="mqtt:shelf", name="Shelf", config=config)
        assert backend.caps(bulb) == {"onoff", "dim"}
        assert await backend.set_powerup(bulb, "off") is False
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for p in (10, 20, 30):
            await backend.apply(bulb, p)
        assert loop.time() - t0 >= 0.5
    run(body)


def test_generic():
    async def body(backend, ctx, port, sets, control):
        with pytest.raises(UserError):
            await backend.add(z2m_config(port, mode="generic"))
        config = await backend.add(z2m_config(port, mode="generic", command_topic="generic/lamp/cmd",
                                              payload='{"dimmer": {value}}', scale=255))
        assert "device" not in config
        bulb = SimpleNamespace(id="mqtt:1", name="Lamp", config=config)
        assert backend.caps(bulb) == {"onoff", "dim"}
        await backend.apply(bulb, 50)
        await backend.apply(bulb, 0)
        assert await until(lambda: len(sets) >= 2)
        assert sets[:2] == [("generic/lamp/cmd", '{"dimmer": 128}'), ("generic/lamp/cmd", "OFF")]
    run(body)


def test_broker_unreachable():
    async def body(backend, ctx, port, sets, control):
        with pytest.raises(UserError):
            await backend.add(z2m_config(free_port(), device="Desk lamp"))
    run(body)
