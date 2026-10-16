"""Yeelight backend against a mock bulb: asyncio TCP JSON lines, the 60/min
quota, music mode calling back, and SSDP answers (127.0.0.1 only)."""

import asyncio
import json
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


class MockYeelight:
    def __init__(self, quota=60, music=True):
        self.power, self.bright = "off", 50
        self.quota = quota
        self.music_ok = music
        self.control = []  # commands on the control connection
        self.music = []    # commands in music mode
        self.server = None

    def run(self, cmd):
        method, params = cmd["method"], cmd["params"]
        if method == "set_power":
            self.power = params[0]
        elif method == "set_bright":
            self.bright = params[0]
        elif method == "get_prop":
            return [{"power": self.power, "bright": str(self.bright)}[p] for p in params]
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


def make_bulb(port):
    return SimpleNamespace(id="yeelight:1", name="Y", config={"host": "127.0.0.1", "port": port})


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

    async def main():
        mock = MockYeelight(music=False)
        port = await mock.start()
        b = yeelight.YeelightBackend(FakeCtx())
        bulb = make_bulb(port)
        await b.apply(bulb, 30)
        assert (mock.power, mock.bright) == ("on", 30)
        assert b.status(bulb)["music"] is False
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await b.apply(bulb, 31)
        await b.apply(bulb, 32)
        assert loop.time() - t0 >= 0.09  # throttled on the control connection
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
                                      b"id: 0x0000000002dfb19a\r\nmodel: color\r\nname: Desk\r\n", addr)

        transport, _ = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
        monkeypatch.setattr(yeelight, "SSDP", ("127.0.0.1", transport.get_extra_info("sockname")[1]))
        found = await yeelight.YeelightBackend(FakeCtx()).discover(0.2)
        transport.close()
        assert b"ST: wifi_bulb" in got[0]
        assert found == [{"kind": "yeelight", "address": "192.0.2.5", "name": "Desk",
                          "config": {"host": "192.0.2.5", "device_id": "0x0000000002dfb19a"}}]

    asyncio.run(main())


def test_yeelight_contract_attributes():
    b = yeelight.YeelightBackend(FakeCtx())
    assert b.experimental is True
    assert b.caps(None) == {"onoff", "dim"}  # set_default is no off/on/restore choice
    assert b.min_lit(None) == 1.0
