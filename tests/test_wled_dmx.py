"""wled (a fake WLED JSON API on 127.0.0.1) and dmx (local UDP sockets for
Art-Net / sACN, a fake OLA, fake serial ports for the Enttec dongles).
Nothing here reaches a real device on the LAN or USB."""

import asyncio
import os
import socket
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("sacn")
from aiohttp import web  # noqa: E402

from haus import backends as registry  # noqa: E402
from haus.backends import NeedsAccess, Offline, dmx, wled  # noqa: E402
from haus.backends import serial as sbackend  # noqa: E402
from haus.common import UserError  # noqa: E402
from lan_fakes import FakeCtx, bulb, serve  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ wled

class FakeWled:
    def __init__(self, lc=1, legacy=None):
        self.leds = {"count": 60, "lc": lc} if legacy is None else {"count": 30, **legacy}
        self.state = {"on": False, "bri": 128}
        self.posts: list[dict] = []

    def app(self):
        async def info(request):
            return web.json_response({"ver": "0.15.0", "mac": "a4cf12fdaea9", "ip": "192.0.2.7",
                                      "name": "Desk", "uptime": 1234, "leds": self.leds})

        async def state(request):
            if request.method == "POST":
                body = await request.json()
                self.posts.append(body)
                self.state.update({k: v for k, v in body.items() if k in ("on", "bri")})
                return web.json_response({"success": True})
            return web.json_response(self.state)

        app = web.Application()
        app.router.add_get("/json/info", info)
        app.router.add_get("/json/state", state)
        app.router.add_post("/json/state", state)
        return app


def test_wled_add_caps_details_apply_fade_colour():
    async def main():
        dev = FakeWled(lc=1)
        runner, host = await serve(dev.app())
        ctx = FakeCtx()
        be = wled.WledBackend(ctx)
        try:
            config = await be.add({"host": host})
            assert config["id"] == "a4cf12fdaea9" and config["mac"] == "a4cf12fdaea9"
            assert config["lc"] == 1 and config["leds"] == 60 and config["ver"] == "0.15.0"
            b = bulb("wled", config["id"], **config)
            assert be.caps(b) == {"onoff", "dim", "hs"}  # no ct without white, never powerup/flick
            assert be.step(b) == pytest.approx(100 / 255)
            assert be.details(b) == {"model": "WLED 0.15.0", "address": host, "ip": "192.0.2.7",
                                     "mac": "a4cf12fdaea9", "leds": 60}
            await be.apply(b, 50)
            assert dev.posts[-1] == {"on": True, "bri": 128}
            await be.apply(b, 0.1)  # never 0 while lit
            assert dev.posts[-1] == {"on": True, "bri": 1}
            await be.apply(b, 0)
            assert dev.posts[-1] == {"on": False}
            assert await be.fade(b, 100, 2.5)
            assert dev.posts[-1] == {"on": True, "bri": 255, "tt": 25}
            assert await be.fade(b, 10, 600)
            assert dev.posts[-1]["tt"] == 655
            assert await be.apply_color(b, {"mode": "hs", "hue": 120, "sat": 100})
            assert dev.posts[-1] == {"seg": {"col": [[0, 255, 0]]}}
            assert not await be.apply_color(b, {"mode": "ct", "kelvin": 3000})
            assert be.status(b) == {"connected": True, "online": True}
            assert await be.query(b) == {"on": True, "percent": 10.2}
            assert ("returned", b.id, None) in ctx.events
        finally:
            await be.stop()
            await runner.cleanup()
    run(main())


def test_wled_white_variants():
    async def main():
        cct_dev, rgbw_dev = FakeWled(lc=7), FakeWled(legacy={"rgbw": True})
        r1, h1 = await serve(cct_dev.app())
        r2, h2 = await serve(rgbw_dev.app())
        be = wled.WledBackend(FakeCtx())
        try:
            c1 = await be.add({"host": h1})
            c2 = await be.add({"host": h2})
            b1, b2 = bulb("wled", "a", **c1), bulb("wled", "b", **c2)
            assert c1["lc"] == 7 and c2["lc"] == 3
            assert be.caps(b1) == be.caps(b2) == {"onoff", "dim", "hs", "ct"}
            assert await be.apply_color(b1, {"mode": "ct", "kelvin": 6500})
            assert cct_dev.posts[-1] == {"seg": {"cct": 255, "col": [[0, 0, 0, 255]]}}
            assert await be.apply_color(b1, {"mode": "ct", "kelvin": 2700})
            assert cct_dev.posts[-1]["seg"]["cct"] == 0
            assert await be.apply_color(b1, {"mode": "hs", "hue": 0, "sat": 0})
            assert cct_dev.posts[-1] == {"seg": {"col": [[0, 0, 0, 255]]}}  # white goes to W
            assert await be.apply_color(b2, {"mode": "ct", "kelvin": 2700})
            col = rgbw_dev.posts[-1]["seg"]["col"][0]
            assert len(col) == 4 and col[3] > 0 and col[0] == 255 - col[3]  # blackbody, white part on W
            white_only = bulb("wled", "w", host=h1, lc=2)
            assert be.caps(white_only) == {"onoff", "dim"}
        finally:
            await be.stop()
            await r1.cleanup()
            await r2.cleanup()
    run(main())


def test_wled_unreachable_is_offline_and_add_refuses():
    async def main():
        dev = FakeWled()
        runner, host = await serve(dev.app())
        ctx = FakeCtx()
        be = wled.WledBackend(ctx)
        try:
            config = await be.add({"host": host})
            b = bulb("wled", config["id"], **config)
            await be.apply(b, 20)
            await runner.cleanup()
            with pytest.raises(Offline):
                await be.apply(b, 30)
            assert be.status(b)["online"] is False
            with pytest.raises(Offline):  # no retry storm while it is away
                await be.apply(b, 40)
            assert not [e for e in ctx.events if e[0] == "returned" and e[2] is not None]
            with pytest.raises(UserError):
                await be.add({"host": host})
        finally:
            await be.stop()
    run(main())


def test_wled_discover_uses_mdns_mac(monkeypatch):
    async def browse(types, timeout):
        assert types == ["_wled._tcp.local."]
        return [{"type": types[0], "name": "Desk", "server": "wled-desk.local", "addresses": ["192.0.2.7"],
                 "port": 80, "properties": {"mac": "a4cf12fdaea9"}},
                {"type": types[0], "name": "Shelf", "server": "", "addresses": ["192.0.2.8"], "port": 80,
                 "properties": {}}]
    monkeypatch.setattr(wled, "mdns_browse", browse)
    found = run(wled.WledBackend(FakeCtx()).discover(0.1))
    assert found == [
        {"kind": "wled", "address": "a4cf12fdaea9", "name": "Desk", "config": {"host": "192.0.2.7"}},
        {"kind": "wled", "address": "192.0.2.8", "name": "Shelf", "config": {"host": "192.0.2.8"}}]
    assert wled.WledBackend.autoconnect is True


# ------------------------------------------------------------------ dmx: layouts

def test_layout_parsing_and_rejection():
    assert dmx.parse_layout("dimmer, R,g,b ,w") == [("dimmer", None), ("r", None), ("g", None), ("b", None), ("w", None)]
    assert dmx.parse_layout("x=0,dimmer,x=255") == [("x", 0), ("dimmer", None), ("x", 255)]
    for bad in ("", "dimmer,strobe", "x=256", "x=a", "r,g", "dimmer,dimmer", "x=0"):
        with pytest.raises(UserError):
            dmx.parse_layout(bad)
    lay = dmx.parse_layout("dimmer,r,g,b,w,x=7")
    assert dmx.fixture_values(lay, 50, None) == [128, 0, 0, 0, 255, 7]          # white goes to w
    assert dmx.fixture_values(lay, 100, (1.0, 0.0, 0.0)) == [255, 255, 0, 0, 0, 7]
    assert dmx.fixture_values(dmx.parse_layout("r,g,b"), 50, (1.0, 0.5, 0.0)) == [128, 64, 0]
    assert dmx.fixture_values(dmx.parse_layout("w"), 20, (1.0, 0.0, 0.0)) == [51]  # no rgb: w is brightness
    assert dmx.fixture_values(dmx.parse_layout("dimmer,w"), 20, None) == [51, 255]


def test_dmx_add_validates_and_ids():
    async def main():
        ctx = FakeCtx()
        be = dmx.DmxBackend(ctx)
        c = await be.add({"output": "artnet", "host": "127.0.0.1", "universe": 3, "address": 10, "layout": "r,g,b"})
        assert c["id"] == "artnet-127.0.0.1-3-10" and c["layout"] == "r,g,b"
        assert be.caps(bulb("dmx", "x", **c)) == {"onoff", "dim", "hs"}
        assert be.caps(bulb("dmx", "y", output="sacn", universe=1, address=1, layout="dimmer,w")) == {"onoff", "dim"}
        s = await be.add({"output": "sacn", "layout": "dimmer"})
        assert s["universe"] == 1 and s["priority"] == 100 and s["id"] == "sacn-1-1"
        for bad in ({"output": "artnet", "address": 511, "layout": "r,g,b"},
                    {"output": "artnet", "address": 0},
                    {"output": "sacn", "universe": 0},
                    {"output": "sacn", "priority": 201},
                    {"output": "artnet", "layout": "dimmer,fog"},
                    {"output": "enttec-pro", "port": "/dev/nonexistent-dmx"}):
            with pytest.raises(UserError):
                await be.add(dict(bad))
        ctx._bulbs.append(bulb("dmx", "artnet-3-10", **c))
        with pytest.raises(UserError, match="overlap|überschneiden"):
            await be.add({"output": "artnet", "host": "127.0.0.1", "universe": 3, "address": 12, "layout": "dimmer"})
        ok = await be.add({"output": "artnet", "host": "127.0.0.1", "universe": 3, "address": 13, "layout": "dimmer"})
        assert ok["id"] == "artnet-127.0.0.1-3-13"
    run(main())


# ------------------------------------------------------------------ dmx: network outputs

def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    return s


def drain(s, wait=0.0):
    time.sleep(wait)
    s.setblocking(False)
    out = []
    try:
        while True:
            out.append(s.recv(2048))
    except BlockingIOError:
        pass
    s.settimeout(1.0)
    return out


def test_artdmx_header_sequence_and_merge(monkeypatch):
    rx = listener()
    monkeypatch.setattr(dmx, "ARTNET_PORT", rx.getsockname()[1])
    monkeypatch.setattr(dmx, "MIN_GAP_S", 0)

    async def main():
        be = dmx.DmxBackend(FakeCtx())
        a = bulb("dmx", "artnet-291-1", output="artnet", host="127.0.0.1", universe=0x123, address=1, layout="dimmer,r,g,b")
        c = bulb("dmx", "artnet-291-10", output="artnet", host="127.0.0.1", universe=0x123, address=10, layout="dimmer")
        try:
            await be.apply(a, 100)
            p1 = rx.recv(2048)
            await be.apply(c, 50)
            p2 = rx.recv(2048)
            assert len(be.universes) == 1  # one frame, one sender for both
            u = next(iter(be.universes.values()))
            assert set(u.fixtures) == {a.id, c.id}
        finally:
            await be.stop()
        return p1, p2

    p1, p2 = run(main())
    rx.close()
    assert len(p1) == 18 + 512
    assert p1[:8] == b"Art-Net\x00" and p1[8:10] == b"\x00\x50" and p1[10:12] == b"\x00\x0e"
    assert p1[12] == 1 and p2[12] == 2 and p1[13] == 0
    assert p1[14] == 0x23 and p1[15] == 0x01 and p1[16:18] == b"\x02\x00"
    assert list(p1[18:22]) == [255, 255, 255, 255] and p1[27] == 0
    assert list(p2[18:22]) == [255, 255, 255, 255] and p2[27] == 128  # the first fixture kept in the merge


def test_sacn_packet_unicast(monkeypatch):
    rx = listener()
    monkeypatch.setattr(dmx, "SACN_PORT", rx.getsockname()[1])
    monkeypatch.setattr(dmx, "MIN_GAP_S", 0)
    ctx = FakeCtx()

    async def main():
        be = dmx.DmxBackend(ctx)
        b = bulb("dmx", "sacn-7-5", output="sacn", host="127.0.0.1", universe=7, address=5, layout="dimmer", priority=150)
        try:
            await be.apply(b, 100)
            p1 = rx.recv(2048)
            await be.apply(b, 0)
            p2 = rx.recv(2048)
        finally:
            await be.stop()
        return p1, p2, be.cid()

    p1, p2, cid = run(main())
    rx.close()
    assert len(p1) == 638
    assert p1[0:2] == b"\x00\x10" and p1[4:16] == b"ASC-E1.17\x00\x00\x00"
    assert p1[18:22] == b"\x00\x00\x00\x04" and p1[22:38] == cid
    assert p1[40:44] == b"\x00\x00\x00\x02" and p1[44:54] == b"Haus.Licht"
    assert p1[108] == 150 and p1[113:115] == b"\x00\x07"
    assert p2[111] == (p1[111] + 1) & 0xFF and p1[112] == 0  # no stream-terminated bit
    assert p1[117] == 0x02 and p1[123:125] == (513).to_bytes(2, "big") and p1[125] == 0
    assert p1[126 + 4] == 255 and p2[126 + 4] == 0 and p1[126:130] == b"\x00" * 4
    assert ctx.state["dmx"]["cid"]  # kept for the next start
    assert dmx.sacn_multicast(0x1234) == "239.255.18.52"


def test_keepalive_resends_until_idle_and_stop_sends_nothing(monkeypatch):
    rx = listener()
    monkeypatch.setattr(dmx, "ARTNET_PORT", rx.getsockname()[1])
    monkeypatch.setattr(dmx, "KEEPALIVE_S", 0.05)
    monkeypatch.setattr(dmx, "MIN_GAP_S", 0)

    async def main():
        be = dmx.DmxBackend(FakeCtx())
        b = bulb("dmx", "artnet-0-1", output="artnet", host="127.0.0.1", universe=0, address=1, layout="dimmer")
        await be.apply(b, 0)  # dark is still driven: resent
        await asyncio.sleep(0.28)
        sent = drain(rx)
        await be.idle(True)
        await asyncio.sleep(0.05)
        drain(rx)
        await asyncio.sleep(0.2)
        after_idle = drain(rx)
        await be.idle(False)
        await be.apply(b, 40)
        await asyncio.sleep(0.12)
        drain(rx)
        await be.stop()
        await asyncio.sleep(0.15)
        return sent, after_idle, drain(rx)

    sent, after_idle, after_stop = run(main())
    rx.close()
    assert len(sent) >= 4 and all(p[18] == 0 for p in sent)
    assert after_idle == [] and after_stop == []


def test_ola_set_dmx_and_discover(monkeypatch):
    async def main():
        posts = []

        async def set_dmx(request):
            posts.append(dict(await request.post()))
            return web.json_response({})

        async def plugins(request):
            return web.json_response({"universes": [{"id": 2, "name": "Stage"}], "plugins": []})

        app = web.Application()
        app.router.add_post("/set_dmx", set_dmx)
        app.router.add_get("/json/universe_plugin_list", plugins)
        runner, host = await serve(app)
        monkeypatch.setattr(dmx, "OLA_HOST", host)
        monkeypatch.setattr(dmx, "MIN_GAP_S", 0)

        async def no_poll(self, timeout):
            return []
        monkeypatch.setattr(dmx.DmxBackend, "_artpoll", no_poll)
        monkeypatch.setattr(dmx, "_ports", lambda: [])
        be = dmx.DmxBackend(FakeCtx())
        try:
            found = await be.discover(0.1)
            assert [f["config"]["universe"] for f in found] == [2] and found[0]["config"]["output"] == "ola"
            c = await be.add({"output": "ola", "host": host, "universe": 2, "address": 3, "layout": "dimmer,x=9"})
            b = bulb("dmx", c["id"], **c)
            await be.apply(b, 100)
            values = posts[-1]["d"].split(",")
            assert posts[-1]["u"] == "2" and len(values) == 512 and values[2:4] == ["255", "9"]
            assert next(iter(be.universes.values())).keep is None  # olad repeats on its own
        finally:
            await be.stop()
            await runner.cleanup()
        with pytest.raises(UserError):
            await be.add({"output": "ola", "host": host, "universe": 2})
    run(main())


def test_artpoll_discover_with_a_fake_node(monkeypatch):
    node = listener()
    monkeypatch.setattr(dmx, "ARTNET_PORT", node.getsockname()[1])
    monkeypatch.setattr(dmx, "POLL_BIND_PORT", 0)
    monkeypatch.setattr(dmx, "BROADCAST", "127.0.0.1")
    monkeypatch.setattr(dmx, "_ports", lambda: [])

    async def no_ola(self, host=None):
        return None
    monkeypatch.setattr(dmx.DmxBackend, "_ola_universes", no_ola)
    reply = bytearray(239)
    reply[:8] = b"Art-Net\x00"
    reply[8:10] = (0x2100).to_bytes(2, "little")
    reply[10:14] = bytes((192, 0, 2, 9))
    reply[18], reply[19], reply[190] = 0x01, 0x02, 0x05
    reply[26:31] = b"short"
    reply[44:53] = b"Stage box"

    async def main():
        task = asyncio.create_task(dmx.DmxBackend(FakeCtx()).discover(0.5))
        data, addr = await asyncio.get_running_loop().run_in_executor(None, node.recvfrom, 2048)
        assert data == dmx.ARTPOLL and len(data) == 14
        node.sendto(bytes(reply), addr)
        return await task

    found = run(main())
    node.close()
    assert found == [{"kind": "dmx", "address": "artnet/192.0.2.9/293",
                      "name": "Stage box (Art-Net 192.0.2.9, universe 293)",
                      "config": {"output": "artnet", "host": "192.0.2.9", "universe": 293,
                                 "address": 1, "layout": "dimmer"}}]


# ------------------------------------------------------------------ dmx: serial outputs

class FakeSerial:
    def __init__(self):
        self.log: list = []
        self.closed = False

    @property
    def break_condition(self):
        return False

    @break_condition.setter
    def break_condition(self, value):
        self.log.append(("break", value))

    def write(self, data):
        self.log.append(("write", bytes(data)))
        return len(data)

    def flush(self):
        self.log.append(("flush",))

    def close(self):
        self.closed = True


def serial_ports(monkeypatch, tmp_path):
    byid = tmp_path / "by-id"
    byid.mkdir()
    dev = tmp_path / "ttyUSB0"
    dev.write_text("")
    (byid / "usb-ENTTEC_DMX_USB_PRO_EN123-if00-port0").symlink_to(dev)
    monkeypatch.setattr(sbackend, "BY_ID", byid)
    return dev, byid / "usb-ENTTEC_DMX_USB_PRO_EN123-if00-port0"


def test_enttec_pro_framing(monkeypatch, tmp_path):
    dev, link = serial_ports(monkeypatch, tmp_path)
    ports = {}

    def opened(port, output):
        assert output == "enttec-pro"
        ports[port] = FakeSerial()
        return ports[port]
    monkeypatch.setattr(dmx, "_open_port", opened)
    monkeypatch.setattr(dmx, "MIN_GAP_S", 0)

    async def main():
        be = dmx.DmxBackend(FakeCtx())
        c = await be.add({"output": "enttec-pro", "port": str(dev), "address": 2, "layout": "dimmer,r,g,b"})
        assert c["port"] == str(link) and c["id"] == f"enttec-pro-{link.name}-0-2" and not ports  # add opens nothing
        b = bulb("dmx", c["id"], **c)
        await be.apply(b, 100)
        await be.apply_color(b, {"mode": "hs", "hue": 240, "sat": 100})
        await be.stop()
        return ports[str(link)]

    port = run(main())
    frames = [d for kind, *d in port.log if kind == "write"]
    assert len(frames) == 2 and port.closed
    f = frames[-1][0]
    assert len(f) == 518 and f[:5] == bytes((0x7E, 6, 0x01, 0x02, 0)) and f[-1] == 0xE7
    assert f[5:10] == bytes((0, 255, 0, 0, 255))  # channel 1 untouched, then dimmer, r, g, b


def test_opendmx_break_then_frame_streamed(monkeypatch, tmp_path):
    dev, link = serial_ports(monkeypatch, tmp_path)
    port = FakeSerial()
    monkeypatch.setattr(dmx, "_open_port", lambda p, output: port)
    monkeypatch.setattr(dmx, "MIN_GAP_S", 0)
    monkeypatch.setattr(dmx, "OPENDMX_HZ", 100)

    async def main():
        be = dmx.DmxBackend(FakeCtx())
        c = await be.add({"output": "opendmx", "port": str(dev), "address": 1, "layout": "dimmer"})
        b = bulb("dmx", c["id"], **c)
        await be.apply(b, 60)
        await asyncio.sleep(0.1)
        await be.idle(True)
        streamed = len([e for e in port.log if e[0] == "write"])
        await asyncio.sleep(0.05)
        paused = len([e for e in port.log if e[0] == "write"])
        await be.stop()
        return streamed, paused

    streamed, paused = run(main())
    assert streamed >= 3 and paused == streamed  # streamed while driven, not when idle
    assert port.log[:4] == [("break", True), ("break", False), ("write", b"\x00" + bytes((153,)) + bytes(511)), ("flush",)]
    assert all(e[1][1] == 153 for e in port.log if e[0] == "write")  # stop sent no dark frame
    assert port.closed


def test_dmx_discover_opens_no_port(monkeypatch, tmp_path):
    dev, link = serial_ports(monkeypatch, tmp_path)
    pro = SimpleNamespace(device=str(dev), vid=0x0403, pid=0x6001, description="DMX USB PRO", product="DMX USB PRO")
    monkeypatch.setattr(dmx, "_ports", lambda: [pro])

    def no_open(*a, **kw):
        raise AssertionError("discover opened a port")
    monkeypatch.setattr(dmx, "_open_port", no_open)
    import serial as serialmod
    monkeypatch.setattr(serialmod, "Serial", no_open)
    monkeypatch.setattr(serialmod, "serial_for_url", no_open)

    async def nothing(self, *a):
        return [] if a and isinstance(a[0], float) else None
    monkeypatch.setattr(dmx.DmxBackend, "_artpoll", nothing)
    monkeypatch.setattr(dmx.DmxBackend, "_ola_universes", nothing)
    found = run(dmx.DmxBackend(FakeCtx()).discover(0.1))
    assert found == [{"kind": "dmx", "address": str(link), "name": "DMX USB PRO (enttec-pro)",
                      "config": {"output": "enttec-pro", "port": str(link), "address": 1, "layout": "dimmer"}}]
    assert dmx.DmxBackend.autoconnect is False
    if os.geteuid() != 0:
        os.chmod(dev, 0o400)
        with pytest.raises(NeedsAccess) as err:
            run(dmx.DmxBackend(FakeCtx()).discover(0.1))
        assert err.value.found[0]["access"] is False
        with pytest.raises(NeedsAccess):
            run(dmx.DmxBackend(FakeCtx()).add({"output": "opendmx", "port": str(dev)}))


def test_registry_lists_both_kinds(monkeypatch):
    monkeypatch.setattr(registry, "_registry", None)
    monkeypatch.setenv("HAUSLICHT_BACKENDS", "wled,dmx")
    try:
        kinds = {d["kind"]: d for d in registry.describe()}
    finally:
        registry._registry = None
    for kind in ("wled", "dmx"):
        assert kinds[kind]["available"], kinds[kind]
        assert all(f.get("label_de") for f in kinds[kind]["fields"])
    for cls in (wled.WledBackend, dmx.DmxBackend):
        caps = cls(FakeCtx()).caps(bulb(cls.kind, "x", layout="r,g,b", lc=7))
        assert not caps & {"powerup", "flick"}
