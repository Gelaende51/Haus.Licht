"""Hue backend against a fake Hue Bridge (CLIP v2 over plain HTTP, aiohttp)."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends.hue import HueBackend  # noqa: E402
from haus.common import UserError  # noqa: E402

KEY = "abcdefKEY"
LIGHTS = [
    {"id": "3f2a8c1e-0000-4000-8000-000000000001", "type": "light", "metadata": {"name": "Desk"},
     "on": {"on": False}, "dimming": {"brightness": 0.0, "min_dim_level": 0.2},
     "color": {"xy": {"x": 0.4573, "y": 0.41}, "gamut_type": "C"},
     "color_temperature": {"mirek": 366, "mirek_valid": True,
                           "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 454}},
     "powerup": {"preset": "safety", "configured": True, "on": {"mode": "on", "on": {"on": True}}}},
    {"id": "77aa0000-0000-4000-8000-000000000002", "type": "light", "metadata": {"name": "Shelf"},
     "on": {"on": True}, "dimming": {"brightness": 50.0}},
]



@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "en")

class FakeBridge:
    def __init__(self) -> None:
        self.lights = {li["id"]: copy.deepcopy(li) for li in LIGHTS}
        self.button = False
        self.puts: list[tuple] = []

    def authorised(self, request) -> bool:
        return request.headers.get("hue-application-key") == KEY

    async def pair(self, request):
        body = await request.json()
        assert body["generatekey"] is True and body["devicetype"].startswith("hauslicht#")
        if not self.button:
            return web.json_response([{"error": {"type": 101, "address": "", "description": "link button not pressed"}}])
        return web.json_response([{"success": {"username": KEY, "clientkey": "00"}}])

    async def list_lights(self, request):
        if not self.authorised(request):
            return web.json_response({"errors": [{"description": "unauthorized user"}], "data": []}, status=403)
        return web.json_response({"errors": [], "data": list(self.lights.values())})

    async def light(self, request):
        if not self.authorised(request):
            return web.json_response({"errors": [{"description": "unauthorized user"}], "data": []}, status=403)
        lid = request.match_info["id"]
        if lid not in self.lights:
            return web.json_response({"errors": [{"description": "not found"}], "data": []}, status=404)
        if request.method == "GET":
            return web.json_response({"errors": [], "data": [self.lights[lid]]})
        body = await request.json()
        self.puts.append((lid, body))
        light = self.lights[lid]
        if "on" in body:
            light["on"] = body["on"]
        if "dimming" in body:
            light["dimming"]["brightness"] = body["dimming"]["brightness"]
        if "powerup" in body:
            light["powerup"] = {**body["powerup"], "configured": True}
        if "color" in body:  # xy mode
            light["color"]["xy"] = body["color"]["xy"]
            light["color_temperature"].update(mirek=None, mirek_valid=False)
        if "color_temperature" in body:  # white mode
            light["color_temperature"].update(mirek=body["color_temperature"]["mirek"], mirek_valid=True)
        return web.json_response({"errors": [], "data": [{"rid": lid, "rtype": "light"}]})


class FakeCtx:
    def __init__(self) -> None:
        self.list, self.state, self.saves, self.changes = [], {}, 0, 0

    def bulbs(self):
        return self.list

    def save(self):
        self.saves += 1

    def changed(self):
        self.changes += 1


def run(body):
    async def main():
        fake = FakeBridge()
        app = web.Application()
        app.router.add_post("/api", fake.pair)
        app.router.add_get("/clip/v2/resource/light", fake.list_lights)
        app.router.add_route("*", "/clip/v2/resource/light/{id}", fake.light)
        server = TestServer(app)
        await server.start_server()
        ctx = FakeCtx()
        backend = HueBackend(ctx)
        backend.browse = lambda timeout: _no_mdns()  # never multicast in tests
        try:
            return await body(fake, backend, ctx, f"http://127.0.0.1:{server.port}")
        finally:
            await backend.stop()
            await server.close()
    return asyncio.run(main())


async def _no_mdns():
    return []


def test_pair_add_apply_powerup():
    async def body(fake, backend, ctx, host):
        with pytest.raises(UserError, match="link button"):
            await backend.add({"host": host, "app_key": "", "light": "Desk"})
        fake.button = True
        with pytest.raises(UserError, match="Desk, Shelf"):  # paired, but no light picked
            await backend.add({"host": host, "app_key": "", "light": ""})
        assert ctx.state["hue"]["keys"][host] == KEY
        fake.button = False  # the key is kept: no second press
        config = await backend.add({"host": host, "app_key": "", "light": "desk"})
        assert config["light_id"] == LIGHTS[0]["id"] and config["id"] == "3f2a8c1e"
        assert config["min_dim"] == 0.2 and config["powerup"] is True
        bulb = SimpleNamespace(id="hue:3f2a8c1e", name="Desk", config=config)
        ctx.list.append(bulb)
        assert backend.caps(bulb) == {"onoff", "dim", "powerup", "ct", "hs"}
        assert backend.ct_range(bulb) == (2203, 6536)  # its mirek_schema 454..153
        assert backend.min_lit(bulb) == 0.2
        await backend.apply(bulb, 42.5)
        await backend.apply(bulb, 0)
        assert fake.puts[0][1] == {"on": {"on": True}, "dimming": {"brightness": 42.5}, "dynamics": {"duration": 200}}
        assert fake.puts[1][1]["on"] == {"on": False}
        assert backend.status(bulb) == {"connected": True, "online": True}
        assert await backend.set_powerup(bulb, "off") is True
        assert fake.lights[LIGHTS[0]["id"]]["powerup"]["on"]["on"] == {"on": False}
        n = len(fake.puts)
        assert await backend.set_powerup(bulb, "off") is True and len(fake.puts) == n  # cached: no write
        assert await backend.set_powerup(bulb, "restore") is True
        assert fake.lights[LIGHTS[0]["id"]]["powerup"]["preset"] == "powerfail"
        await backend.apply(bulb, 60)
        assert await backend.query(bulb) == {"on": True, "percent": 60}
        shelf = await backend.add({"host": host, "app_key": "", "light": LIGHTS[1]["id"]})
        assert shelf["powerup"] is False and shelf["min_dim"] == 1.0
        assert await backend.set_powerup(SimpleNamespace(id="hue:x", config=shelf), "on") is False
        found = await backend.discover(2.0)
        assert [f["name"] for f in found] == ["Shelf"] and "app_key" not in found[0]["config"]
    run(body)


def test_errors_and_rate_limit():
    async def body(fake, backend, ctx, host):
        with pytest.raises(UserError, match="refused"):
            await backend.add({"host": host, "app_key": "wrong", "light": "Desk"})
        config = await backend.add({"host": host, "app_key": KEY, "light": "Shelf"})
        bulb = SimpleNamespace(id="hue:77aa0000", name="Shelf", config=config)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for p in (10, 20, 30):
            await backend.apply(bulb, p)
        assert loop.time() - t0 >= 0.4
        del fake.lights[config["light_id"]]  # deleted on the bridge
        with pytest.raises(Offline):
            await backend.apply(bulb, 40)
        unreachable = SimpleNamespace(id="hue:y", config={**config, "host": "http://127.0.0.1:9"})
        with pytest.raises(ConnectionError):
            await backend.apply(unreachable, 40)
        assert backend.status(unreachable) == {"connected": False, "online": False}
        with pytest.raises(UserError, match="not reachable"):
            await backend.add({"host": "http://127.0.0.1:9", "app_key": KEY, "light": "Desk"})
    run(body)


def test_colour_xy_and_mirek_keep_level_and_dark():
    async def body(fake, backend, ctx, host):
        desk = await backend.add({"host": host, "app_key": KEY, "light": "Desk"})
        shelf = await backend.add({"host": host, "app_key": KEY, "light": "Shelf"})
        assert shelf["color"] == [] and backend.caps(SimpleNamespace(config=shelf)) == {"onoff", "dim"}
        bulb = SimpleNamespace(id="hue:3f2a8c1e", name="Desk", config=desk)
        assert await backend.apply_color(bulb, {"mode": "ct", "kelvin": 3000}) is False  # not lit: nothing
        assert fake.puts == []
        await backend.apply(bulb, 35)
        assert await backend.apply_color(bulb, {"mode": "hs", "hue": 0, "sat": 100})
        assert fake.puts[-1][1] == {"color": {"xy": {"x": 0.735, "y": 0.265}}, "dynamics": {"duration": 200}}
        assert await backend.apply_color(bulb, {"mode": "hs", "hue": 0, "sat": 0})
        assert fake.puts[-1][1]["color"] == {"xy": {"x": 0.3127, "y": 0.329}}  # white: D65
        assert await backend.apply_color(bulb, {"mode": "ct", "kelvin": 2000})  # clamped to 454 mirek
        assert fake.puts[-1][1] == {"color_temperature": {"mirek": 454}, "dynamics": {"duration": 200}}
        light = fake.lights[desk["light_id"]]
        assert light["color_temperature"]["mirek_valid"] and light["on"] == {"on": True}
        assert light["dimming"]["brightness"] == 35  # the level stays
        await backend.apply(bulb, 0)
        n = len(fake.puts)
        assert await backend.apply_color(bulb, {"mode": "hs", "hue": 90, "sat": 50}) is False
        assert len(fake.puts) == n and light["on"] == {"on": False}
        # an older config without `color`: read once after the first change
        old = {k: v for k, v in desk.items() if k not in ("color", "kelvin")}
        bulb2 = SimpleNamespace(id="hue:old", name="Desk", config=old)
        assert backend.caps(bulb2) == {"onoff", "dim", "powerup"}
        await backend.apply(bulb2, 10)
        assert old["color"] == ["ct", "hs"] and old["kelvin"] == [2203, 6536] and ctx.saves
    run(body)
