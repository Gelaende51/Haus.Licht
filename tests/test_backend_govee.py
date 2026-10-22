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

    def save(self):
        self.events.append(("save",))


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockGovee(asyncio.DatagramProtocol):
    def __init__(self, drop_first_brightness=False, white_only=False):
        self.on, self.brightness = 0, 100
        self.color, self.kelvin = {"r": 255, "g": 255, "b": 255}, 7200
        self.white_only = white_only  # a device whose devStatus shows no colour (made up: none known)
        self.colorwc = []
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
        elif cmd == "colorwc":
            self.colorwc.append(msg["data"])
            self.color, self.kelvin = msg["data"]["color"], msg["data"]["colorTemInKelvin"]
        elif cmd == "devStatus":
            status = {"onOff": self.on, "brightness": self.brightness}
            if not self.white_only:
                status.update(color=self.color, colorTemInKelvin=self.kelvin)
            self.reply(addr, "devStatus", status)


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


def test_govee_colour_caps_from_dev_status():
    async def main():
        b = govee.GoveeBackend(FakeCtx())
        for white_only, caps in ((False, {"ct", "hs"}), (True, set())):
            mock, transport, port = await device(white_only=white_only)
            bulb = SimpleNamespace(id="govee:1", name="G", config={"host": "127.0.0.1", "port": port})
            assert b.caps(bulb) == {"onoff", "dim"}  # not learned yet: brightness only
            await b.apply(bulb, 50)
            assert b.caps(bulb) == {"onoff", "dim"} | caps
            assert b.ct_range(bulb) == (2000, 9000)
            transport.close()
            b.on.clear()
        await b.stop()

    asyncio.run(main())


def test_govee_colorwc_keeps_level_and_dark():
    async def main():
        mock, transport, port = await device()
        b = govee.GoveeBackend(FakeCtx())
        bulb = SimpleNamespace(id="govee:1", name="G", config={"host": "127.0.0.1", "port": port})
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 0, "sat": 100}) is False  # not lit
        assert mock.colorwc == []
        await b.apply(bulb, 40)
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 240, "sat": 50})
        assert mock.colorwc == [{"color": {"r": 128, "g": 128, "b": 255}, "colorTemInKelvin": 0}]
        assert await b.apply_color(bulb, {"mode": "hs", "hue": 240, "sat": 50})  # unchanged: not again
        assert len(mock.colorwc) == 1
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 1500})  # clamped to 2000
        assert mock.colorwc[-1]["colorTemInKelvin"] == 2000 and len(mock.colorwc) == 2
        assert (mock.on, mock.brightness) == (1, 40)
        assert not [c for c, _v in mock.got if c in ("turn", "brightness")][2:]  # no power/level with it
        await b.apply(bulb, 0)
        n = len(mock.colorwc)
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 3000}) is False
        assert len(mock.colorwc) == n and mock.on == 0
        await b.stop()
        transport.close()

    asyncio.run(main())
