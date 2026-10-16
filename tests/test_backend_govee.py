"""Govee backend against a mock LAN-API device on 127.0.0.1 (scan, turn,
brightness, devStatus; answers to the listen port, as the real ones do)."""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from haus.backends import Offline, govee
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


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockGovee(asyncio.DatagramProtocol):
    def __init__(self, drop_first_brightness=False):
        self.on, self.brightness = 0, 100
        self.got = []
        self.drop = drop_first_brightness
        self.silent = False

    def connection_made(self, transport):
        self.transport = transport

    def reply(self, addr, cmd, data):
        self.transport.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(),
                              (addr[0], govee.LISTEN_PORT))

    def datagram_received(self, data, addr):
        if self.silent:
            return
        msg = json.loads(data)["msg"]
        cmd, value = msg["cmd"], msg["data"].get("value")
        self.got.append((cmd, value))
        if cmd == "scan":
            self.reply(addr, "scan", {"ip": "127.0.0.1", "device": "1F:80:C5:32:32:36:72:4E", "sku": "H618A"})
        elif cmd == "turn":
            self.on = value
        elif cmd == "brightness":
            if self.drop:
                self.drop = False  # a datagram lost on the way
            else:
                self.brightness = value
        elif cmd == "devStatus":
            self.reply(addr, "devStatus", {"onOff": self.on, "brightness": self.brightness,
                                           "color": {"r": 255, "g": 255, "b": 255}, "colorTemInKelvin": 7200})


async def device(**kw):
    mock = MockGovee(**kw)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: mock, local_addr=("127.0.0.1", 0))
    return mock, transport, transport.get_extra_info("sockname")[1]


@pytest.fixture(autouse=True)
def local_ports(monkeypatch):
    monkeypatch.setattr(govee, "LISTEN_PORT", free_udp_port())
    monkeypatch.setattr(govee, "MULTICAST", "127.0.0.1")


def test_govee_apply_confirms_and_resends():
    async def main():
        mock, transport, port = await device(drop_first_brightness=True)
        b = govee.GoveeBackend(FakeCtx())
        bulb = SimpleNamespace(id="govee:1", name="G", config={"host": "127.0.0.1", "port": port})
        await b.apply(bulb, 35.4)
        await asyncio.sleep(0.05)  # the re-send is fire and forget
        assert (mock.on, mock.brightness) == (1, 35)  # devStatus showed 100: sent once more
        assert mock.got.count(("brightness", 35)) == 2
        assert b.status(bulb) == {"connected": True, "online": True}
        await b.apply(bulb, 0.2)
        await asyncio.sleep(0.05)
        assert mock.brightness == 1
        assert mock.got.count(("turn", 1)) == 2  # once at first, once with the re-send; not again
        await b.apply(bulb, 0)
        assert mock.on == 0
        assert await b.query(bulb) == {"on": False, "percent": 0}
        await b.stop()
        transport.close()

    asyncio.run(main())


def test_govee_silence_is_offline_until_it_answers(monkeypatch):
    async def main():
        mock, transport, port = await device()
        b = govee.GoveeBackend(FakeCtx())
        b.presence.interval = 0.05
        bulb = SimpleNamespace(id="govee:1", name="G", config={"host": "127.0.0.1", "port": port})
        await b.apply(bulb, 50)
        mock.silent = True
        monkeypatch.setattr(govee, "STATUS_TIMEOUT_S", 0.2)
        with pytest.raises(Offline):
            await b.apply(bulb, 60)
        assert b.status(bulb)["online"] is False
        mock.silent = False
        for _ in range(100):
            if b.status(bulb)["online"]:
                break
            await asyncio.sleep(0.02)
        assert b.status(bulb)["online"] is True
        await b.apply(bulb, 60)
        assert mock.brightness == 60
        await b.stop()
        transport.close()

    asyncio.run(main())


def test_govee_add_and_discover(monkeypatch):
    async def main():
        mock, transport, port = await device()
        b = govee.GoveeBackend(FakeCtx())
        cfg = await b.add({"host": "127.0.0.1", "scan_port": port})
        assert cfg["id"] == "c5323236724e"  # the last 12 hex digits of the device id
        assert cfg["sku"] == "H618A"
        monkeypatch.setattr(govee, "SCAN_PORT", port)
        found = await b.discover(0.2)
        assert found and found[0]["config"]["host"] == "127.0.0.1" and found[0]["kind"] == "govee"
        mock.silent = True
        with pytest.raises(UserError):
            await b.add({"host": "127.0.0.1", "scan_port": port})
        await b.stop()
        transport.close()

    asyncio.run(main())
