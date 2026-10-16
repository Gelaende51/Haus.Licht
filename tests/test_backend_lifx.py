"""LIFX backend (plain UDP) against lifx-emulator on 127.0.0.1."""

import asyncio
import socket
from types import SimpleNamespace

import pytest

from haus.backends import Offline, lifx
from haus.common import UserError

emu = pytest.importorskip("lifx_emulator")
from lifx_emulator.devices.manager import DeviceManager  # noqa: E402
from lifx_emulator.repositories.device_repository import DeviceRepository  # noqa: E402

SERIAL = "d073d5000001"


class FakeCtx:
    def __init__(self):
        self.events = []
        self.items = []

    def bulbs(self):
        return self.items

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


async def emulator():
    port = free_udp_port()
    device = emu.create_color_light(SERIAL, mdns_enabled=False)
    server = emu.EmulatedLifxServer([device], DeviceManager(DeviceRepository()), "127.0.0.1", port,
                                    ipv6_bind_address="::1")
    await server.start()
    return server, device, port


def test_lifx_against_emulator(monkeypatch):
    async def main():
        server, device, port = await emulator()
        ctx = FakeCtx()
        b = lifx.LifxBackend(ctx)
        try:
            cfg = await b.add({"host": "127.0.0.1", "port": port})
            assert cfg["id"] == SERIAL and cfg["mac"] == SERIAL and cfg["port"] == port
            bulb = SimpleNamespace(id=f"lifx:{SERIAL}", name="L", config=cfg)
            ctx.items.append(bulb)

            await b.apply(bulb, 25)
            assert device.state.power_level == 65535
            assert abs(device.state.color.brightness - round(0.25 * 65535)) <= 1
            q = await b.query(bulb)
            assert q["on"] is True and abs(q["percent"] - 25) < 0.1
            assert b.status(bulb) == {"connected": True, "online": True}

            await b.apply(bulb, 0.5)  # any level > 0 is lit
            assert device.state.color.brightness == round(0.005 * 65535)
            await b.apply(bulb, 0)
            assert device.state.power_level == 0
            assert (await b.query(bulb))["on"] is False

            uptime = await b.uptime_of(bulb)
            assert 0 <= uptime < 60

            found = []
            monkeypatch.setattr(lifx, "BROADCAST", "127.0.0.1")
            monkeypatch.setattr(lifx, "PORT", port)
            found = await b.discover(0.3)
            assert [f["config"]["mac"] for f in found] == [SERIAL]
        finally:
            await b.stop()
            await server.stop()

    asyncio.run(main())


def test_lifx_offline_and_reboot_evidence(monkeypatch):
    async def main():
        server, device, port = await emulator()
        ctx = FakeCtx()
        b = lifx.LifxBackend(ctx)
        b.presence.interval = 0.05
        cfg = await b.add({"host": "127.0.0.1", "port": port})
        bulb = SimpleNamespace(id=f"lifx:{SERIAL}", name="L", config=cfg)
        ctx.items.append(bulb)
        await b.apply(bulb, 40)
        await server.stop()  # power cut
        monkeypatch.setattr(lifx, "REPLY_TIMEOUT_S", 0.05)
        with pytest.raises(Offline):
            await b.apply(bulb, 50)
        assert b.status(bulb)["online"] is False
        await asyncio.sleep(0.2)
        server, device, port2 = await emulator()  # back, freshly booted
        bulb.config["port"] = port2
        for _ in range(100):
            if b.status(bulb)["online"]:
                break
            await asyncio.sleep(0.05)
        returned = [e for e in ctx.events if e[0] == "returned" and e[2] is not None]
        assert returned and returned[0][1] == bulb.id and returned[0][2] < 15
        await b.apply(bulb, 50)  # colour read afresh after the reboot
        assert device.state.power_level == 65535
        await b.stop()
        await server.stop()

    asyncio.run(main())


def test_lifx_flick_from_uptime(monkeypatch):
    """The watcher: uptime going backwards between two looks is a flick."""
    monkeypatch.setattr(lifx, "POLL_S", 0.01)

    async def main():
        ctx = FakeCtx()
        b = lifx.LifxBackend(ctx)
        bulb = SimpleNamespace(id="lifx:x", name="L", config={"host": "127.0.0.1"})
        ctx.items.append(bulb)
        seq = iter([100.0, 102.0, 1.5, 3.5] + [5.0] * 1000)

        async def uptime_of(_bulb):
            return next(seq)

        b.uptime_of = uptime_of
        await b.start()
        await asyncio.sleep(0.15)
        await b.stop()
        assert ctx.events.count(("flick", "lifx:x")) == 1

    asyncio.run(main())


def test_lifx_add_refuses_silence(monkeypatch):
    monkeypatch.setattr(lifx, "REPLY_TIMEOUT_S", 0.05)

    async def main():
        b = lifx.LifxBackend(FakeCtx())
        with pytest.raises(UserError):
            await b.add({"host": "127.0.0.1", "port": free_udp_port()})
        await b.stop()

    asyncio.run(main())


def test_lifx_packet_layout():
    data = lifx.packet(1234, 7, lifx.SET_LIGHT_POWER, b"\xff\xff\0\0\0\0", bytes.fromhex(SERIAL) + bytes(2), ack=True)
    assert len(data) == 36 + 6 and data[0] == 42
    source, target, seq, msg_type, payload = lifx.parse(data)
    assert (source, target[:6].hex(), seq, msg_type, payload) == (1234, SERIAL, 7, 117, b"\xff\xff\0\0\0\0")
    assert int.from_bytes(data[2:4], "little") == 1024 | 0x1000  # addressed, not tagged
    assert data[22] == 2  # ack_required
