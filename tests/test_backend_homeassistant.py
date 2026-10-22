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
            "light.rgb": {"entity_id": "light.rgb", "state": "off", "attributes": {
                "friendly_name": "RGB", "supported_color_modes": ["color_temp", "xy"],
                "min_color_temp_kelvin": 2202, "max_color_temp_kelvin": 6535}},
            "light.white": {"entity_id": "light.white", "state": "off", "attributes": {
                "friendly_name": "White", "supported_color_modes": ["color_temp"],
                "min_mireds": 250, "max_mireds": 454}},
            "light.rgbw": {"entity_id": "light.rgbw", "state": "off", "attributes": {
                "friendly_name": "RGBW", "supported_color_modes": ["rgbw"]}},
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
                    data = req["service_data"]
                    attrs = {**old["attributes"], **{k: v for k, v in data.items() if k != "brightness_pct"}}
                    if "brightness_pct" in data:
                        attrs["brightness"] = round(data["brightness_pct"] * 2.55)
                    new = {**old, "state": "on", "attributes": attrs}
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
        assert [f["config"]["entity"] for f in found] == ["light.rgb", "light.rgbw", "light.shelf", "light.white"]
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


def test_colour_caps_per_entity():
    async def body(fake, backend, ctx, url):
        want = {"light.desk": ({"color": []}, set()),
                "light.rgb": ({"color": ["ct", "hs"], "kelvin": [2202, 6535]}, {"ct", "hs"}),
                "light.white": ({"color": ["ct"], "kelvin": [2203, 4000]}, {"ct"}),
                "light.rgbw": ({"color": ["hs"]}, {"hs"})}
        for entity, (stored, caps) in want.items():
            config = await backend.add({"url": url, "token": TOKEN, "entity": entity})
            assert {k: config[k] for k in ("color", "kelvin") if k in config} == stored, entity
            b = SimpleNamespace(id=f"homeassistant:{entity}", name=entity, config=config)
            assert backend.caps(b) - {"onoff", "dim", "powerup"} == caps
            if "kelvin" in stored:
                assert backend.ct_range(b) == tuple(stored["kelvin"])
        assert backend.ct_range(SimpleNamespace(id="x", config={})) == (2000, 6535)
    run(body)


def test_colour_messages_and_dark_light():
    async def body(fake, backend, ctx, url):
        config = await backend.add({"url": url, "token": TOKEN, "entity": "light.rgb"})
        for key in ("color", "kelvin"):
            config.pop(key)  # an older config
        b = SimpleNamespace(id="homeassistant:rgb", name="RGB", config=config)
        hs = {"mode": "hs", "hue": 30.04, "sat": 80}
        assert await backend.apply_color(b, hs) is False  # not driven yet
        await backend.apply(b, 0)
        assert config["color"] == ["ct", "hs"] and config["kelvin"] == [2202, 6535]  # learned
        n = len(fake.calls)
        assert await backend.apply_color(b, hs) is False and len(fake.calls) == n  # dark stays dark
        await backend.apply(b, 50)
        await asyncio.sleep(0.05)
        assert await backend.apply_color(b, hs) is True
        assert fake.calls[-1] == ("light", "turn_on", "light.rgb", {"hs_color": [30.0, 80.0]})
        assert await backend.apply_color(b, {"mode": "ct", "kelvin": 1500}) is True
        assert fake.calls[-1] == ("light", "turn_on", "light.rgb", {"color_temp_kelvin": 2202})
        await asyncio.sleep(0.05)
        assert fake.states["light.rgb"]["attributes"]["brightness"] == 127  # 50 % as the fake rounds it: the level stays
        await fake.push("light.rgb", {**fake.states["light.rgb"], "state": "off"})  # switched off elsewhere
        await asyncio.sleep(0.05)
        n = len(fake.calls)
        assert await backend.apply_color(b, hs) is False and len(fake.calls) == n
        white = await backend.add({"url": url, "token": TOKEN, "entity": "light.white"})
        w = SimpleNamespace(id="homeassistant:white", name="White", config=white)
        await backend.apply(w, 30)
        await asyncio.sleep(0.05)
        assert await backend.apply_color(w, hs) is False  # no hs on a white light
    run(body)
