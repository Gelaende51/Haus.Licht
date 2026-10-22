"""LIFX backend (plain UDP) against lifx-emulator on 127.0.0.1."""

import asyncio
import socket
import struct
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

    def save(self):
        self.events.append(("save",))


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def emulator(make=None):
    port = free_udp_port()
    device = (make or emu.create_color_light)(SERIAL, mdns_enabled=False)
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


def test_lifx_product_table_matches_products_json():
    """WHITE_PRODUCTS / COLOR_RANGES against the registry lifx-emulator
    generates from LIFX's products.json."""
    from lifx_emulator.products.registry import get_registry
    for p in get_registry()._products.values():
        if p.temperature_range is None:
            continue  # switches: no light
        got = lifx.product_color(p.pid)
        lo, hi = p.temperature_range.min, p.temperature_range.max
        want = (["ct", "hs"] if p.has_color else ["ct"] if lo < hi else [])
        assert got["color"] == want, (p.pid, p.name)
        if "ct" in want:
            assert got["kelvin"] == [lo, hi], (p.pid, p.name)
    assert lifx.product_color(9999) == {"color": ["ct", "hs"], "kelvin": [1500, 9000]}  # newer: colour


@pytest.mark.parametrize("make, caps, kelvin", [
    ("create_color_light", {"ct", "hs"}, (1500, 9000)),             # product 91, LIFX Color 800lm
    ("create_color_temperature_light", {"ct"}, (2500, 9000)),       # product 50, LIFX Mini DD
])
def test_lifx_colour_caps_per_product(make, caps, kelvin):
    async def main():
        server, device, port = await emulator(getattr(emu, make))
        b = lifx.LifxBackend(FakeCtx())
        try:
            cfg = await b.add({"host": "127.0.0.1", "port": port})
            assert cfg["product"] == device.state.product
            bulb = SimpleNamespace(id=f"lifx:{SERIAL}", name="L", config=cfg)
            assert b.caps(bulb) == {"onoff", "dim", "flick"} | caps
            assert b.ct_range(bulb) == kelvin
        finally:
            await b.stop()
            await server.stop()

    asyncio.run(main())


def test_lifx_colour_keeps_level_and_never_switches_on():
    async def main():
        server, device, port = await emulator()
        ctx = FakeCtx()
        b = lifx.LifxBackend(ctx)
        try:
            # an older config (no `color`): the product is asked once, on the first contact
            bulb = SimpleNamespace(id=f"lifx:{SERIAL}", name="L",
                                   config={"host": "127.0.0.1", "port": port, "mac": SERIAL})
            assert b.caps(bulb) == {"onoff", "dim", "flick"}
            assert await b.apply_color(bulb, {"mode": "hs", "hue": 0, "sat": 100}) is False  # not lit
            await b.apply(bulb, 30)
            assert bulb.config["color"] == ["ct", "hs"] and ("save",) in ctx.events
            level = device.state.color.brightness
            sent = []
            real = b._ask

            async def spy(bl, msg_type, payload=b"", want=(lifx.ACK,)):
                sent.append((msg_type, payload))
                return await real(bl, msg_type, payload, want)

            b._ask = spy
            assert await b.apply_color(bulb, {"mode": "hs", "hue": 180, "sat": 50})
            assert sent == [(lifx.SET_COLOR, struct.pack("<BHHHHI", 0, 32768, 32768, level, 3500, 150))]
            c = device.state.color
            assert (c.hue, c.saturation, c.brightness, c.kelvin) == (32768, 32768, level, 3500)
            assert await b.apply_color(bulb, {"mode": "hs", "hue": 180, "sat": 50})  # unchanged: not again
            assert len(sent) == 1
            assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 12000})  # clamped to 9000
            assert sent[-1] == (lifx.SET_COLOR, struct.pack("<BHHHHI", 0, 32768, 0, level, 9000, 150))
            assert device.state.power_level == 65535 and device.state.color.brightness == level
            await b.apply(bulb, 0)
            n = len(sent)
            assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 2700}) is False
            assert len(sent) == n and device.state.power_level == 0  # dark stays dark
        finally:
            await b.stop()
            await server.stop()

    asyncio.run(main())
