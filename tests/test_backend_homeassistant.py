"""Home Assistant backend against a fake HA WebSocket server (aiohttp)."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends.homeassistant import HomeAssistantBackend  # noqa: E402
from haus.common import UserError  # noqa: E402

TOKEN = "secret"



@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "en")

class FakeHA:
    def __init__(self) -> None:
        self.states = {
            "light.desk": {"entity_id": "light.desk", "state": "off",
                           "attributes": {"friendly_name": "Desk", "brightness": None}},
            "light.shelf": {"entity_id": "light.shelf", "state": "on",
                            "attributes": {"friendly_name": "Shelf", "brightness": 128}},
            "select.desk_power_on_behavior": {"entity_id": "select.desk_power_on_behavior", "state": "on",
                                              "attributes": {"options": ["off", "on", "toggle", "previous"]}},
            "switch.fan": {"entity_id": "switch.fan", "state": "off", "attributes": {}},
        }
        self.calls: list[tuple] = []
        self.sockets: list = []

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "bad"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            req = msg.json()
            if req["type"] == "get_states":
                await ws.send_json({"id": req["id"], "type": "result", "success": True,
                                    "result": list(self.states.values())})
            elif req["type"] == "subscribe_events":
                await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": None})
            elif req["type"] == "call_service":
                entity = req["target"]["entity_id"]
                self.calls.append((req["domain"], req["service"], entity, req["service_data"]))
                await ws.send_json({"id": req["id"], "type": "result", "success": True, "result": {}})
                old = dict(self.states[entity])
                new = dict(old)
                if req["service"] == "turn_off":
                    new["state"] = "off"
                elif req["service"] == "turn_on":
                    new = {**old, "state": "on", "attributes": {
                        **old["attributes"], "brightness": round(req["service_data"]["brightness_pct"] * 2.55)}}
                elif req["service"] == "select_option":
                    new["state"] = req["service_data"]["option"]
                await self.push(entity, new)
        return ws

    async def push(self, entity, new):
        old = self.states.get(entity)
        self.states[entity] = new
        for ws in self.sockets:
            if not ws.closed:
                await ws.send_json({"type": "event", "id": 2, "event": {
                    "event_type": "state_changed",
                    "data": {"entity_id": entity, "old_state": old, "new_state": new}}})


class FakeCtx:
    def __init__(self) -> None:
        self.list: list = []
        self.flicks: list = []
        self.returns: list = []
        self.changes = 0

    def bulbs(self):
        return self.list

    def changed(self):
        self.changes += 1

    def flick(self, bid):
        self.flicks.append(bid)

    def power_returned(self, bid, gap):
        self.returns.append((bid, gap))

    def save(self):
        pass


def run(body):
    async def main():
        fake = FakeHA()
        app = web.Application()
        app.router.add_get("/api/websocket", fake.handler)
        server = TestServer(app)
        await server.start_server()
        ctx = FakeCtx()
        backend = HomeAssistantBackend(ctx)
        try:
            return await body(fake, backend, ctx, f"http://127.0.0.1:{server.port}")
        finally:
            await backend.stop()
            await server.close()
    return asyncio.run(main())


def test_add_apply_powerup_query():
    async def body(fake, backend, ctx, url):
        with pytest.raises(UserError):
            await backend.add({"url": url, "token": "wrong", "entity": "light.desk"})
        with pytest.raises(UserError):
            await backend.add({"url": url, "token": TOKEN, "entity": "switch.fan"})
        config = await backend.add({"url": url, "token": TOKEN, "entity": "desk"})
        assert config["id"] == "desk" and config["entity"] == "light.desk" and config["powerup"]
        bulb = SimpleNamespace(id="homeassistant:desk", name="Desk", config=config)
        ctx.list.append(bulb)
        assert backend.caps(bulb) == {"onoff", "dim", "powerup"}
        assert backend.status(bulb) == {"connected": True, "online": True}
        await backend.apply(bulb, 40)
        await backend.apply(bulb, 0)
        assert fake.calls[:2] == [("light", "turn_on", "light.desk", {"brightness_pct": 40.0}),
                                  ("light", "turn_off", "light.desk", {})]
        assert await backend.set_powerup(bulb, "restore") is True
        assert fake.calls[-1] == ("select", "select_option", "select.desk_power_on_behavior",
                                  {"option": "previous"})
        await backend.apply(bulb, 60)
        await asyncio.sleep(0.05)
        assert await backend.query(bulb) == {"on": True, "percent": 60.0}
        # a second light of the same server: the token is taken over
        shelf = await backend.add({"url": url, "token": "", "entity": "light.shelf"})
        assert shelf["token"] == TOKEN and not shelf["powerup"]
        assert backend.caps(SimpleNamespace(id="x", config=shelf)) == {"onoff", "dim"}
    run(body)


def test_unavailable_and_return_and_discover():
    async def body(fake, backend, ctx, url):
        config = await backend.add({"url": url, "token": TOKEN, "entity": "light.desk"})
        bulb = SimpleNamespace(id="homeassistant:desk", name="Desk", config=config)
        ctx.list.append(bulb)
        found = await backend.discover(2.0)
        assert [f["config"]["entity"] for f in found] == ["light.shelf"]
        assert "token" not in found[0]["config"]
        await fake.push("light.desk", {**fake.states["light.desk"], "state": "unavailable"})
        await asyncio.sleep(0.05)
        assert backend.status(bulb)["online"] is False
        with pytest.raises(Offline):
            await backend.apply(bulb, 30)
        await fake.push("light.desk", {**fake.states["light.desk"], "state": "off"})
        await asyncio.sleep(0.05)
        assert backend.status(bulb)["online"] is True
        assert len(ctx.returns) == 1 and ctx.returns[0][0] == bulb.id and ctx.returns[0][1] >= 0
    run(body)


def test_rate_limit():
    async def body(fake, backend, ctx, url):
        config = await backend.add({"url": url, "token": TOKEN, "entity": "light.desk"})
        bulb = SimpleNamespace(id="homeassistant:desk", name="Desk", config=config)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for p in (10, 20, 30, 40):
            await backend.apply(bulb, p)
        assert loop.time() - t0 >= 0.4  # 3 gaps of 0.15 s
    run(body)


def test_reconnect_after_drop():
    async def body(fake, backend, ctx, url):
        config = await backend.add({"url": url, "token": TOKEN, "entity": "light.desk"})
        bulb = SimpleNamespace(id="homeassistant:desk", name="Desk", config=config)
        for ws in fake.sockets:
            await ws.close()
        await asyncio.sleep(0.05)
        assert backend.status(bulb)["connected"] is False
        await backend.apply(bulb, 50)
        assert backend.status(bulb)["connected"] is True
        assert fake.calls[-1][1] == "turn_on"
    run(body)
