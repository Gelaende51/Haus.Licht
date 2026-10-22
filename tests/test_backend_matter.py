"""Matter backend against a fake Matter server WebSocket (aiohttp)."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends.matter import MatterBackend  # noqa: E402
from haus.common import UserError  # noqa: E402


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "en")


def nodes():
    return {
        5: {"node_id": 5, "available": True, "attributes": {
            "0/40/3": "Nanoleaf Essentials A19", "0/40/5": "Desk",
            "1/6/0": False, "1/6/16387": 1, "1/8/0": 1, "1/8/2": 2}},
        7: {"node_id": 7, "available": True, "attributes": {
            "0/40/3": "Plug", "1/6/0": True}},  # on/off only
        9: {"node_id": 9, "available": True, "attributes": {
            "0/40/3": "Bulb", "1/6/0": True, "1/8/0": 100}},  # no StartUpOnOff
        11: {"node_id": 11, "available": True, "attributes": {  # HS + XY + CT, 153-454 mired
            "0/40/3": "Colour", "1/6/0": False, "1/8/0": 1, "1/768/16394": 0x19,
            "1/768/16395": 153, "1/768/16396": 454}},
        12: {"node_id": 12, "available": True, "attributes": {  # XY + CT, no physical range
            "0/40/3": "XY", "1/6/0": False, "1/8/0": 1, "1/768/16394": 0x18}},
        13: {"node_id": 13, "available": True, "attributes": {  # CT only
            "0/40/3": "White", "1/6/0": False, "1/8/0": 1, "1/768/16394": 0x10,
            "1/768/16395": 200, "1/768/16396": 370}},
    }


class FakeServer:
    def __init__(self) -> None:
        self.nodes = nodes()
        self.commands: list[dict] = []
        self.sockets: list = []

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"fabric_id": 1, "compressed_fabric_id": 2, "schema_version": 11,
                            "min_supported_schema_version": 9, "sdk_version": "fake", "wifi_credentials_set": False,
                            "thread_credentials_set": False, "bluetooth_enabled": False})
        async for msg in ws:
            req = msg.json()
            self.commands.append(req)
            mid, cmd, args = req["message_id"], req["command"], req.get("args", {})
            if cmd == "start_listening":
                await ws.send_json({"message_id": mid, "result": list(self.nodes.values())})
            elif cmd == "device_command":
                node = self.nodes[args["node_id"]]
                if not node["available"]:
                    await ws.send_json({"message_id": mid, "error_code": 0, "details": "node unavailable"})
                    continue
                ep = args["endpoint_id"]
                if args["cluster_id"] == 0x0300:  # colour: no on/off or level change
                    await ws.send_json({"message_id": mid, "result": None})
                    continue
                if args["command_name"] == "Off":
                    node["attributes"][f"{ep}/6/0"] = False
                else:
                    node["attributes"][f"{ep}/6/0"] = True
                    node["attributes"][f"{ep}/8/0"] = args["payload"]["level"]
                await ws.send_json({"message_id": mid, "result": None})
                for path in (f"{ep}/6/0", f"{ep}/8/0"):
                    await ws.send_json({"event": "attribute_updated",
                                        "data": [args["node_id"], path, node["attributes"][path]]})
            elif cmd == "write_attribute":
                self.nodes[args["node_id"]]["attributes"][args["attribute_path"]] = args["value"]
                await ws.send_json({"message_id": mid, "result": [{"Path": {}, "Status": 0}]})
            else:
                await ws.send_json({"message_id": mid, "error_code": 1, "details": "unknown"})
        return ws

    async def event(self, event, data):
        for ws in self.sockets:
            if not ws.closed:
                await ws.send_json({"event": event, "data": data})


class FakeCtx:
    def __init__(self) -> None:
        self.list, self.returns, self.changes = [], [], 0

    def bulbs(self):
        return self.list

    def changed(self):
        self.changes += 1

    def power_returned(self, bid, gap):
        self.returns.append((bid, gap))

    def save(self):
        pass


def run(body):
    async def main():
        fake = FakeServer()
        app = web.Application()
        app.router.add_get("/ws", fake.handler)
        server = TestServer(app)
        await server.start_server()
        ctx = FakeCtx()
        backend = MatterBackend(ctx)
        try:
            return await body(fake, backend, ctx, f"ws://127.0.0.1:{server.port}/ws")
        finally:
            await backend.stop()
            await server.close()
    return asyncio.run(main())


def test_add_apply_powerup():
    async def body(fake, backend, ctx, url):
        with pytest.raises(UserError, match="5/1, 9/1"):
            await backend.add({"url": url, "node": 7, "endpoint": 0})
        with pytest.raises(UserError, match="not reachable"):
            await backend.add({"url": "ws://127.0.0.1:9/ws", "node": 5, "endpoint": 0})
        config = await backend.add({"url": url, "node": 5, "endpoint": 0})
        assert config == {"url": url, "node": 5, "endpoint": 1, "color": [], "min_level": 2, "powerup": True,
                          "id": "5-1"}
        bulb = SimpleNamespace(id="matter:5-1", name="Desk", config=config)
        ctx.list.append(bulb)
        assert backend.caps(bulb) == {"onoff", "dim", "powerup"}
        assert backend.min_lit(bulb) == pytest.approx(0.79)
        assert backend.status(bulb) == {"connected": True, "online": True}
        await backend.apply(bulb, 50)
        await backend.apply(bulb, 0.1)  # floored at MinLevel
        await backend.apply(bulb, 0)
        sent = [c["args"] for c in fake.commands if c["command"] == "device_command"]
        assert sent[0]["command_name"] == "MoveToLevelWithOnOff" and sent[0]["payload"]["level"] == 127
        assert sent[0]["cluster_id"] == 8 and sent[0]["endpoint_id"] == 1
        assert sent[1]["payload"]["level"] == 2
        assert sent[2]["command_name"] == "Off" and sent[2]["cluster_id"] == 6
        assert await backend.set_powerup(bulb, "restore") is True
        assert fake.nodes[5]["attributes"]["1/6/16387"] is None
        n = len(fake.commands)
        assert await backend.set_powerup(bulb, "restore") is True and len(fake.commands) == n
        assert await backend.set_powerup(bulb, "off") is True
        assert fake.nodes[5]["attributes"]["1/6/16387"] == 0
        await backend.apply(bulb, 100)
        await asyncio.sleep(0.05)
        assert await backend.query(bulb) == {"on": True, "percent": 100.0}
        plain = await backend.add({"url": url, "node": 9, "endpoint": 1})
        assert plain["powerup"] is False
        assert await backend.set_powerup(SimpleNamespace(id="matter:9-1", config=plain), "on") is False
        found = await backend.discover(2.0)
        assert [(f["config"]["node"], f["name"]) for f in found] == [(9, "Bulb"), (11, "Colour"), (12, "XY"),
                                                                     (13, "White")]
    run(body)


def test_unavailable_return_and_rate():
    async def body(fake, backend, ctx, url):
        config = await backend.add({"url": url, "node": 5, "endpoint": 1})
        bulb = SimpleNamespace(id="matter:5-1", name="Desk", config=config)
        ctx.list.append(bulb)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for p in (10, 20, 30):
            await backend.apply(bulb, p)
        assert loop.time() - t0 >= 0.4
        fake.nodes[5]["available"] = False
        await fake.event("node_updated", fake.nodes[5])
        await asyncio.sleep(0.05)
        assert backend.status(bulb)["online"] is False
        with pytest.raises(Offline):
            await backend.apply(bulb, 40)
        fake.nodes[5]["available"] = True
        await fake.event("node_updated", fake.nodes[5])
        await asyncio.sleep(0.05)
        assert backend.status(bulb)["online"] is True
        assert len(ctx.returns) == 1 and ctx.returns[0][0] == bulb.id
        for ws in fake.sockets:  # the server restarts: reconnect on the next command
            await ws.close()
        await asyncio.sleep(0.05)
        assert backend.status(bulb)["connected"] is False
        await backend.apply(bulb, 60)
        assert backend.status(bulb)["connected"] is True
    run(body)


def test_colour_caps_per_light():
    async def body(fake, backend, ctx, url):
        want = {9: ({"color": []}, set()),
                11: ({"color": ["ct", "hs"], "kelvin": [2203, 6536]}, {"ct", "hs"}),
                12: ({"color": ["ct", "hs"], "kelvin": [2000, 6536], "xy_only": True}, {"ct", "hs"}),
                13: ({"color": ["ct"], "kelvin": [2703, 5000]}, {"ct"})}
        for node, (stored, caps) in want.items():
            config = await backend.add({"url": url, "node": node, "endpoint": 1})
            assert {k: config[k] for k in ("color", "kelvin", "xy_only") if k in config} == stored, node
            b = SimpleNamespace(id=f"matter:{node}-1", name="x", config=config)
            assert backend.caps(b) - {"onoff", "dim", "powerup"} == caps
            if "kelvin" in stored:
                assert backend.ct_range(b) == tuple(stored["kelvin"])
    run(body)


def test_colour_messages_and_dark_light():
    async def body(fake, backend, ctx, url):
        config = await backend.add({"url": url, "node": 11, "endpoint": 1})
        for key in ("color", "kelvin"):
            config.pop(key)  # an older config
        b = SimpleNamespace(id="matter:11-1", name="Colour", config=config)
        ctx.list.append(b)
        hs = {"mode": "hs", "hue": 180, "sat": 50}

        def colour_sent():
            return [c["args"] for c in fake.commands
                    if c["command"] == "device_command" and c["args"]["cluster_id"] == 0x0300]
        assert await backend.apply_color(b, hs) is False  # not driven yet
        await backend.apply(b, 0)
        assert config["color"] == ["ct", "hs"] and config["kelvin"] == [2203, 6536]  # learned
        assert await backend.apply_color(b, hs) is False and colour_sent() == []  # dark stays dark
        await backend.apply(b, 50)
        await asyncio.sleep(0.05)
        assert await backend.apply_color(b, hs) is True
        assert await backend.apply_color(b, {"mode": "ct", "kelvin": 1800}) is True
        sent = colour_sent()
        assert [(s["command_name"], s["payload"]) for s in sent] == [
            ("MoveToHueAndSaturation", {"hue": 127, "saturation": 127, "transitionTime": 2,
                                        "optionsMask": 0, "optionsOverride": 0}),
            ("MoveToColorTemperature", {"colorTemperatureMireds": 454, "transitionTime": 2,
                                        "optionsMask": 0, "optionsOverride": 0})]
        assert sent[0]["node_id"] == 11 and sent[0]["endpoint_id"] == 1
        assert fake.nodes[11]["attributes"]["1/8/0"] == 127  # the level stays
        fake.nodes[11]["attributes"]["1/6/0"] = False  # switched off elsewhere
        await fake.event("attribute_updated", [11, "1/6/0", False])
        await asyncio.sleep(0.05)
        assert await backend.apply_color(b, hs) is False and len(colour_sent()) == 2
        xy = await backend.add({"url": url, "node": 12, "endpoint": 1})
        x = SimpleNamespace(id="matter:12-1", name="XY", config=xy)
        await backend.apply(x, 30)
        await asyncio.sleep(0.05)
        assert await backend.apply_color(x, {"mode": "hs", "hue": 0, "sat": 100}) is True
        last = colour_sent()[-1]
        assert last["command_name"] == "MoveToColor"
        assert last["payload"]["colorX"] == round(0.735 * 65536) and last["payload"]["colorY"] == round(0.265 * 65536)
    run(body)
