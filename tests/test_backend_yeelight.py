"""Yeelight backend against a mock bulb: asyncio TCP JSON lines, the 60/min
quota, music mode calling back, and SSDP answers (127.0.0.1 only)."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from haus.backends import Offline, yeelight
from haus.common import UserError


class FakeCtx:
    def __init__(self):
        self.events = []

    def bulbs(self):
        return []

    def flick(self, bid):
        self.events.append(("flick", bid))

    def power_returned(self, bid, gap):
        self.events.append(("returned", bid, gap))

    def changed(self):
        self.events.append(("changed",))

    def save(self):
        self.events.append(("save",))


class MockYeelight:
    def __init__(self, quota=60, music=True, kind="color"):
        self.power, self.bright = "off", 50
        self.kind = kind  # "color" (RGB + white), "ct" (white range), "mono" (brightness only)
        self.ct, self.hue, self.sat = 4000, 0, 0
        self.sent = []  # every (method, params) the bulb took
        self.quota = quota
        self.music_ok = music
        self.control = []  # commands on the control connection
        self.music = []    # commands in music mode
        self.server = None

    def run(self, cmd):
        method, params = cmd["method"], cmd["params"]
        self.sent.append((method, params))
        if method == "set_power":
            self.power = params[0]
        elif method == "set_bright":
            self.bright = params[0]
        elif method in ("set_hsv", "set_ct_abx"):
            if self.power != "on":  # the spec: only accepted while on
                return None
            if method == "set_hsv":
                self.hue, self.sat = params[0], params[1]
            else:
                self.ct = params[0]
        elif method == "get_prop":
            props = {"power": self.power, "bright": str(self.bright),
                     "ct": str(self.ct) if self.kind != "mono" else "",
                     "hue": str(self.hue) if self.kind == "color" else ""}
            return [props[p] for p in params]
        return ["ok"]

    async def handle(self, reader, writer):
        writer.write(b'{"method":"props","params":{"power":"' + self.power.encode() + b'"}}\r\n')
        while line := await reader.readline():
            cmd = json.loads(line)
            self.control.append(cmd["method"])
            if len(self.control) > self.quota:
                writer.write(json.dumps({"id": cmd["id"], "error": {"code": -1, "message": "client quota exceeded"}}).encode() + b"\r\n")
            elif cmd["method"] == "set_music":
                writer.write(json.dumps({"id": cmd["id"], "result": ["ok"]}).encode() + b"\r\n")
                if self.music_ok:
                    _on, host, port = cmd["params"]
                    asyncio.get_running_loop().create_task(self.music_client(host, port))
            else:
                writer.write(json.dumps({"id": cmd["id"], "result": self.run(cmd)}).encode() + b"\r\n")
            await writer.drain()
        writer.close()

    async def music_client(self, host, port):
        reader, writer = await asyncio.open_connection(host, port)
        while line := await reader.readline():
            cmd = json.loads(line)
            self.music.append(cmd["method"])
            self.run(cmd)  # no answers in music mode
        writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


def make_bulb(port, **config):
    return SimpleNamespace(id="yeelight:1", name="Y",
                           config={"host": "127.0.0.1", "port": port, "color": [], **config})


async def until(cond, tries=100):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def test_yeelight_music_mode_has_no_quota(monkeypatch):
    monkeypatch.setattr(yeelight, "MUSIC_INTERVAL_S", 0.0)

    async def main():
        mock = MockYeelight(quota=5)
        port = await mock.start()
        b = yeelight.YeelightBackend(FakeCtx())
        bulb = make_bulb(port)
        for n in range(1, 101):  # far past 60 per minute
            await b.apply(bulb, n)
        assert await until(lambda: mock.bright == 100)
        assert mock.power == "on"
        assert mock.control == ["set_music"]  # everything else went through music mode
        assert mock.music.count("set_bright") == 100 and mock.music.count("set_power") == 1
        assert b.status(bulb)["music"] is True
        await b.apply(bulb, 0)
        assert await until(lambda: mock.power == "off")
        await b.stop()
        mock.server.close()

    asyncio.run(main())


def test_yeelight_without_music_stays_under_quota(monkeypatch):
    monkeypatch.setattr(yeelight, "CONTROL_INTERVAL_S", 0.05)
    monkeypatch.setattr(yeelight, "TIMEOUT_S", 0.3)
    # When each apply's first command went out (the throttle spaces those).
    # Not the wall time around two applies: the first one's own sends count
    # towards the second one's wait, so under load it need not wait at all.
    sent = []
    command = yeelight.YeeBulb.command

    async def timed(self, method, params):
        sent.append((method, time.monotonic()))
        return await command(self, method, params)
    monkeypatch.setattr(yeelight.YeeBulb, "command", timed)

    async def main():
        mock = MockYeelight(music=False)
        port = await mock.start()
        b = yeelight.YeelightBackend(FakeCtx())
        bulb = make_bulb(port)
        await b.apply(bulb, 30)
        assert (mock.power, mock.bright) == ("on", 30)
        assert b.status(bulb)["music"] is False
        await b.apply(bulb, 31)
        await b.apply(bulb, 32)
        firsts = [t for m, t in sent if m == "set_power"][:1] + [t for m, t in sent if m == "set_bright"][1:]
        assert len(firsts) == 3
        assert all(later - earlier >= 0.05 - 0.002 for earlier, later in zip(firsts, firsts[1:]))  # throttled
        assert mock.control.count("set_music") == 1  # not retried after the bulb never called back
        assert await b.query(bulb) == {"on": True, "percent": 32.0}
        await b.stop()
        mock.server.close()

    asyncio.run(main())


def test_yeelight_quota_error_is_a_retry_not_offline(monkeypatch):
    monkeypatch.setattr(yeelight, "CONTROL_INTERVAL_S", 0.0)
    monkeypatch.setattr(yeelight, "TIMEOUT_S", 0.3)

    async def main():
        mock = MockYeelight(quota=3, music=False)
        port = await mock.start()
        b = yeelight.YeelightBackend(FakeCtx())
        bulb = make_bulb(port)
        await b.apply(bulb, 10)  # set_music, set_power, set_bright
        with pytest.raises(yeelight.YeeError, match="quota"):
            await b.apply(bulb, 20)
        assert b.status(bulb)["online"] is True
        mock.server.close()

    asyncio.run(main())


def test_yeelight_offline_and_add(monkeypatch):
    monkeypatch.setattr(yeelight, "TIMEOUT_S", 0.3)

    async def main():
        mock = MockYeelight()
        port = await mock.start()
        b = yeelight.YeelightBackend(FakeCtx())
        cfg = await b.add({"host": "127.0.0.1", "port": port, "device_id": "0x000000000015243f"})
        assert cfg["id"] == "15243f"
        mock.server.close()
        await mock.server.wait_closed()
        with pytest.raises(UserError):
            await b.add({"host": "127.0.0.1", "port": port})
        bulb = make_bulb(port)
        with pytest.raises(Offline):
            await b.apply(bulb, 10)
        assert b.status(bulb)["online"] is False
        await b.stop()

    asyncio.run(main())


def test_yeelight_discover_parses_answers(monkeypatch):
    async def main():
        loop = asyncio.get_running_loop()
        got = []

        class Responder(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                got.append(data)
                self.transport.sendto(b"HTTP/1.1 200 OK\r\nLocation: yeelight://192.0.2.5:55443\r\n"
                                      b"id: 0x0000000002dfb19a\r\nmodel: color\r\nname: Desk\r\n"
                                      b"support: get_prop set_default set_power toggle set_bright start_cf "
                                      b"stop_cf set_scene cron_add set_ct_abx set_rgb set_hsv set_music\r\n", addr)

        transport, _ = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
        monkeypatch.setattr(yeelight, "SSDP", ("127.0.0.1", transport.get_extra_info("sockname")[1]))
        found = await yeelight.YeelightBackend(FakeCtx()).discover(0.2)
        transport.close()
        assert b"ST: wifi_bulb" in got[0]
        assert found == [{"kind": "yeelight", "address": "192.0.2.5", "name": "Desk",
                          "config": {"host": "192.0.2.5", "device_id": "0x0000000002dfb19a", "model": "color",
                                     "color": ["ct", "hs"], "kelvin": [1700, 6500]}}]

    asyncio.run(main())


def test_yeelight_contract_attributes():
    b = yeelight.YeelightBackend(FakeCtx())
    assert b.experimental is True
    assert b.caps(SimpleNamespace(config={})) == {"onoff", "dim"}  # set_default is no off/on/restore choice
    assert b.min_lit(None) == 1.0


def test_yeelight_support_and_props_give_caps(monkeypatch):
    assert yeelight.support_color("get_prop set_power set_bright") == {"color": []}
    assert yeelight.support_color("set_power set_bright set_ct_abx") == {"color": ["ct"], "kelvin": [2700, 6500]}
    monkeypatch.setattr(yeelight, "TIMEOUT_S", 0.3)

    async def main():
        b = yeelight.YeelightBackend(FakeCtx())
        for kind, caps, kelvin in (("color", {"ct", "hs"}, (1700, 6500)), ("ct", {"ct"}, (2700, 6500)),
                                   ("mono", set(), (1700, 6500))):
            mock = MockYeelight(kind=kind)
            port = await mock.start()
            cfg = await b.add({"host": "127.0.0.1", "port": port})  # by address: get_prop tells
            bulb = SimpleNamespace(id="yeelight:1", config=cfg)
            assert b.caps(bulb) == {"onoff", "dim"} | caps, kind
            assert b.ct_range(bulb) == kelvin
            mock.server.close()
        await b.stop()

    asyncio.run(main())


def test_yeelight_colour_messages_and_dark_bulb(monkeypatch):
    monkeypatch.setattr(yeelight, "MUSIC_INTERVAL_S", 0.0)

    async def main():
        mock = MockYeelight()
        port = await mock.start()
        ctx = FakeCtx()
        b = yeelight.YeelightBackend(ctx)
        bulb = make_bulb(port)
        del bulb.config["color"]  # an older config: asked once on the control connection
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 10, "sat": 20}) is False  # never lit
        await b.apply(bulb, 40)
        assert bulb.config["color"] == ["ct", "hs"] and ("save",) in ctx.events
        assert mock.control == ["get_prop", "set_music"]
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 359.6, "sat": 80.2})
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 359.6, "sat": 80.2})  # unchanged: not again
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 1000})  # clamped to 1700
        assert await until(lambda: mock.ct == 1700)
        assert [m for m in mock.sent if m[0].startswith("set_") and m[0] not in ("set_music",)] == [
            ("set_power", ["on", "smooth", 200]), ("set_bright", [40, "smooth", 200]),
            ("set_hsv", [0, 80, "smooth", 200]), ("set_ct_abx", [1700, "smooth", 200])]
        assert (mock.power, mock.bright) == ("on", 40)  # the level stays
        await b.apply(bulb, 0)
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 3000}) is False  # dark: nothing sent
        await asyncio.sleep(0.05)
        assert mock.power == "off" and mock.ct == 1700
        await b.stop()
        mock.server.close()

    asyncio.run(main())
