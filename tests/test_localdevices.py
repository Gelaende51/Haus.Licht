"""Directly attached lights: sysled (a fake /sys/class/leds tree), usblight
(a faked busylight-core), serial (pyserial's loop:// and a fake port).
Nothing here touches the real /sys, USB or a serial device."""

import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from haus import backends as registry
from haus.backends import NeedsAccess, Offline, _bulbkit, sysled, usblight
from haus.backends.mock import MockBackend
from haus.common import UserError
from haus.profiles import new_state
from test_core import run as run_daemon

ROOT = Path(__file__).resolve().parent.parent


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


def bulb(bid, config):
    return SimpleNamespace(id=bid, name=bid, config=config)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ sysled

def led(root: Path, name: str, max_b=255, trigger="none kbd-capslock [phy0rx] timer", multi=None,
        writable=True, intensity_writable=True):
    d = root / name
    d.mkdir(parents=True)
    (d / "brightness").write_text("0\n")
    (d / "max_brightness").write_text(f"{max_b}\n")
    (d / "trigger").write_text(trigger + "\n")
    if multi:
        (d / "multi_index").write_text(multi + "\n")
        (d / "multi_intensity").write_text(" ".join("0" for _ in multi.split()) + "\n")
        if not intensity_writable:
            os.chmod(d / "multi_intensity", 0o444)
    if not writable:
        os.chmod(d / "brightness", 0o444)
    return d


class FakeLogind:
    def __init__(self):
        self.calls = []

    async def set_brightness(self, subsystem, name, value):
        self.calls.append((subsystem, name, value))

    async def close(self):
        pass


@pytest.fixture
def leds(tmp_path, monkeypatch):
    root = tmp_path / "leds"
    root.mkdir()
    monkeypatch.setattr(sysled, "SYSFS", root)
    return root


def test_sysled_discover_skips_own_uleds_and_shows_trigger(leds):
    led(leds, "phy0-led")
    led(leds, "input3::capslock", max_b=1, trigger="[none] kbd-capslock")
    led(leds, "hauslicht::kbd_backlight")
    found = run(sysled.SysLedBackend(FakeCtx()).discover(0))
    assert [f["address"] for f in found] == ["input3::capslock", "phy0-led"]
    by = {f["address"]: f for f in found}
    assert by["phy0-led"]["trigger"] == "phy0rx" and "phy0rx" in by["phy0-led"]["name"]
    assert by["phy0-led"]["config"] == {"led": "phy0-led"}
    assert sysled.SysLedBackend.autoconnect is False and sysled.SysLedBackend.bluetooth is False


def test_sysled_own_uleds_is_refused(leds):
    led(leds, "hauslicht::kbd_backlight")
    with pytest.raises(UserError):
        run(sysled.SysLedBackend(FakeCtx()).add({"led": "hauslicht::kbd_backlight"}))


def test_sysled_plain_led_writes_brightness(leds):
    d = led(leds, "tpacpi::lid_logo_dot", max_b=255)
    be = sysled.SysLedBackend(FakeCtx())
    be.logind = FakeLogind()
    cfg = run(be.add({"led": "tpacpi::lid_logo_dot"}))
    assert cfg["id"] == "tpacpi::lid_logo_dot"
    b = bulb("sysled:x", cfg)
    assert be.caps(b) == {"onoff", "dim"}
    assert be.step(b) == pytest.approx(100 / 255)
    run(be.apply(b, 50))
    assert (d / "brightness").read_text().strip() == "128"
    run(be.apply(b, 0.1))
    assert (d / "brightness").read_text().strip() == "1"  # lit stays lit
    run(be.apply(b, 0))
    assert (d / "brightness").read_text().strip() == "0"
    assert be.logind.calls == []
    assert be.details(b)["trigger"] == "phy0rx"


def test_sysled_one_level_is_onoff(leds):
    d = led(leds, "input3::capslock", max_b=1)
    be = sysled.SysLedBackend(FakeCtx())
    b = bulb("sysled:c", {"led": "input3::capslock"})
    assert be.caps(b) == {"onoff"}
    assert be.step(b) == 100.0
    run(be.apply(b, 30))
    assert (d / "brightness").read_text().strip() == "1"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only files")
def test_sysled_unwritable_goes_through_logind(leds):
    d = led(leds, "platform::micmute", max_b=1, writable=False)
    be = sysled.SysLedBackend(FakeCtx())
    be.logind = FakeLogind()
    b = bulb("sysled:m", {"led": "platform::micmute"})
    run(be.apply(b, 80))
    run(be.apply(b, 0))
    assert be.logind.calls == [("leds", "platform::micmute", 1), ("leds", "platform::micmute", 0)]
    assert (d / "brightness").read_text().strip() == "0"


def test_logind_calls_set_brightness_on_display_session(monkeypatch):
    """The real Logind helper against a fake bus: user/self's Display, then SetBrightness."""
    dbus_fast = pytest.importorskip("dbus_fast")
    sent = []

    class Bus:
        connected = True

        async def call(self, msg):
            sent.append((msg.path, msg.member, list(msg.body)))
            if msg.member == "Get":
                return SimpleNamespace(message_type=dbus_fast.MessageType.METHOD_RETURN,
                                       body=[SimpleNamespace(value=["3", "/org/freedesktop/login1/session/_33"])])
            return SimpleNamespace(message_type=dbus_fast.MessageType.METHOD_RETURN, body=[])

        def disconnect(self):
            pass

    lg = sysled.Logind()
    lg.bus = Bus()
    run(lg.set_brightness("leds", "kbd", 2))
    assert sent == [(sysled.USER_SELF, "Get", ["org.freedesktop.login1.User", "Display"]),
                    ("/org/freedesktop/login1/session/_33", "SetBrightness", ["leds", "kbd", 2])]


def test_sysled_multicolor(leds):
    d = led(leds, "rgb:status", max_b=100, multi="red green blue")
    be = sysled.SysLedBackend(FakeCtx())
    b = bulb("sysled:rgb", {"led": "rgb:status"})
    assert be.caps(b) == {"onoff", "dim", "hs"}
    run(be.apply(b, 100))
    assert run(be.apply_color(b, {"mode": "hs", "hue": 120, "sat": 100})) is True
    assert (d / "multi_intensity").read_text().split() == ["0", "100", "0"]
    assert run(be.apply_color(b, {"mode": "ct", "kelvin": 3000})) is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes read-only files")
def test_sysled_multicolor_without_writable_intensity_has_no_hs(leds):
    led(leds, "rgb:ro", max_b=100, multi="red green blue", intensity_writable=False)
    be = sysled.SysLedBackend(FakeCtx())
    assert "hs" not in be.caps(bulb("sysled:ro", {"led": "rgb:ro"}))


def test_sysled_gone_is_offline(leds):
    be = sysled.SysLedBackend(FakeCtx())
    b = bulb("sysled:g", {"led": "gone"})

    async def main():
        with pytest.raises(Offline):
            await be.apply(b, 50)
        assert be.status(b)["online"] is False
        await be.stop()
    run(main())


# ------------------------------------------------------------------ usblight

class FakeLight:
    def __init__(self, hw):
        self.hw = hw
        self.shown = []
        self.released = False
        self.cancelled = 0

    def on(self, rgb):
        if self.hw.unplugged:
            raise OSError("LightUnavailableError")
        self.shown.append(tuple(rgb))

    def off(self):
        if self.hw.unplugged:
            raise OSError("LightUnavailableError")
        self.shown.append((0, 0, 0))

    def cancel_tasks(self):
        self.cancelled += 1

    def release(self):
        self.released = True


class FakeCls:
    supported_device_ids = {(0x20A0, 0x41E5): "BlinkStick"}

    @classmethod
    def vendor(cls):
        return "Agile Innovative"


def hw(serial="BS012345-3.0", path=b"0001:0005:00", **kw):
    return SimpleNamespace(vendor_id=0x20A0, product_id=0x41E5, serial_number=serial, path=path,
                           unplugged=False, **kw)


@pytest.fixture
def usb(monkeypatch, tmp_path):
    state = {"devices": [hw()], "opened": []}

    def fake_open(cls, h):
        if h.unplugged:
            raise OSError("no device")
        light = FakeLight(h)
        state["opened"].append(light)
        return light

    monkeypatch.setattr(usblight, "_hardware", lambda: [(FakeCls, h) for h in state["devices"] if not h.unplugged])
    monkeypatch.setattr(usblight, "_open", fake_open)
    monkeypatch.setattr(usblight, "USB_DEV", str(tmp_path / "usb"))  # no node there: not checked
    monkeypatch.setattr(_bulbkit, "PROBE_S", 0.02)
    return state


def test_usblight_discover_add_colour_scaling(usb):
    async def main():
        be = usblight.UsbLightBackend(FakeCtx())
        found = await be.discover(0)
        assert found == [{"kind": "usblight", "address": "20a0:41e5:BS012345-3.0",
                          "name": "Agile Innovative BlinkStick", "config": {"device": "20a0:41e5:BS012345-3.0"}}]
        cfg = await be.add(dict(found[0]["config"]))
        assert cfg["id"] == "BS012345-3.0" and cfg["serial"] == "BS012345-3.0"
        assert usb["opened"][0].released and usb["opened"][0].shown == []  # add shows nothing
        b = bulb("usblight:BS012345-3.0", cfg)
        assert be.caps(b) == {"onoff", "dim", "hs"}
        await be.apply(b, 50)
        light = usb["opened"][-1]
        assert light.shown[-1] == (128, 128, 128)  # white until a colour is set
        await be.apply_color(b, {"mode": "hs", "hue": 0, "sat": 100})
        assert light.shown[-1] == (128, 0, 0)
        await be.apply(b, 100)
        assert light.shown[-1] == (255, 0, 0)
        await be.apply(b, 0)
        assert light.shown[-1] == (0, 0, 0)
        await be.stop()
        assert light.released and light.shown[-1] == (0, 0, 0)  # stop sends nothing
    run(main())


def test_usblight_unplug_offline_and_replug_reopens(usb):
    async def main():
        ctx = FakeCtx()
        be = usblight.UsbLightBackend(ctx)
        be.presence.interval = 0.02
        b = bulb("usblight:x", {"device": "20a0:41e5:BS012345-3.0"})
        await be.apply(b, 100)
        assert be.status(b) == {"connected": True, "online": True}
        usb["devices"][0].unplugged = True
        with pytest.raises(Offline):
            await be.apply(b, 40)
        assert be.status(b) == {"connected": False, "online": False}
        with pytest.raises(Offline):  # still away: the core waits
            await be.apply(b, 40)
        usb["devices"][0].unplugged = False
        for _ in range(100):
            await asyncio.sleep(0.01)
            if be.status(b)["online"]:
                break
        assert be.status(b)["online"] is True  # the probe re-opened it
        await be.apply(b, 40)
        assert usb["opened"][-1].shown[-1] == (102, 102, 102)
        await be.stop()
    run(main())


def test_usblight_id_without_serial_and_access(usb, tmp_path):
    usb["devices"] = [hw(serial="", path=b"0003:0007:00")]
    node = tmp_path / "usb" / "003" / "007"
    node.parent.mkdir(parents=True)
    node.write_text("")
    os.chmod(node, 0o600)

    async def main():
        be = usblight.UsbLightBackend(FakeCtx())
        found = await be.discover(0)
        assert found[0]["address"] == "20a0:41e5:@0003:0007:00"
        cfg = await be.add({"device": found[0]["address"]})
        assert cfg["id"] == "20a041e5-0003_0007_00"
        if os.geteuid() != 0:
            os.chmod(node, 0o400)
            with pytest.raises(NeedsAccess) as err:
                await be.discover(0)
            assert err.value.found and err.value.found[0]["access"] is False
            assert str(node) in str(err.value)
            with pytest.raises(NeedsAccess):
                await be.add({"device": found[0]["address"]})
    run(main())


def test_usblight_node_of():
    assert usblight.node_of("/dev/hidraw3") == "/dev/hidraw3"
    assert usblight.node_of("0001:000a:00").endswith("/001/010")


def test_setup_rule_covers_busylight_hid_ids():
    pytest.importorskip("busylight_core")
    from busylight_core import Light
    script = (ROOT / "install" / "setup-root.sh").read_text()
    listed = set(re.search(r'USBLIGHT_IDS="([^"]*)"', script).group(1).split())
    serial_only = {"MuteSync", "BusyTag", "FitStatUSB"}  # CDC serial lights: the serial kind's rule
    want = {f"{v:04x}:{p:04x}" for cls in Light.subclasses() if cls.__name__ not in serial_only
            for (v, p) in cls.supported_device_ids}
    assert want <= listed
    serial = re.search(r'SERIAL_IDS="([^"]*)"', script).group(1).split()
    assert {"1a86:7523", "10c4:ea60", "0403:6001", "0403:6015", "2341:*", "303a:*"} <= set(serial)
    assert (ROOT / "install" / "setup-version").read_text().strip() == "5"


# ------------------------------------------------------------------ serial

serialmod = pytest.importorskip("serial")
from haus.backends import serial as sbackend  # noqa: E402


class FakePort:
    """What the backend uses of a pyserial port."""

    def __init__(self, greeting=b"Ada\n"):
        self.rx = bytearray(greeting)
        self.tx = []
        self.closed = False
        self.fail = False

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n):
        out, self.rx[:] = bytes(self.rx[:n]), self.rx[n:]
        return out

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, data):
        if self.fail:
            raise serialmod.SerialException("device reports readiness to read but returned no data")
        self.tx.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


def test_adalight_frame_bytes():
    assert sbackend.adalight_frame((10, 20, 30), 1) == b"Ada\x00\x00\x55" + bytes((10, 20, 30))
    f = sbackend.adalight_frame((1, 2, 3), 300)  # 299 = 0x012b: 0x01 ^ 0x2b ^ 0x55 = 0x7f
    assert f[:6] == b"Ada\x01\x2b\x7f" and len(f) == 6 + 900 and f[6:12] == bytes((1, 2, 3, 1, 2, 3))


def test_serial_loop_url_adalight_bytes(monkeypatch):
    """pyserial's own loop:// port: the bytes written come back as sent."""
    monkeypatch.setattr(sbackend, "GREETING_S", 0.05)

    async def main():
        be = sbackend.SerialBackend(FakeCtx())
        cfg = await be.add({"port": "loop://", "protocol": "adalight", "baud": 115200, "leds": 2})
        port = be.pending["loop://"]
        b = bulb("serial:loop", cfg)
        await be.apply(b, 100)
        await asyncio.sleep(0.05)
        assert port.read(port.in_waiting) == b"Ada\x00\x01\x54" + bytes((255, 255, 255)) * 2
        await be.stop()
    run(main())


def test_serial_adalight_keepalive_and_colour(monkeypatch):
    monkeypatch.setattr(sbackend, "GREETING_S", 0.05)
    monkeypatch.setattr(sbackend, "KEEPALIVE_S", 0.03)
    ports = []

    def fake_open(port, baud):
        ports.append((port, baud, FakePort()))
        return ports[-1][2]

    monkeypatch.setattr(sbackend, "_open_port", fake_open)

    async def main():
        be = sbackend.SerialBackend(FakeCtx())
        b = bulb("serial:a", {"port": "loop://", "protocol": "adalight", "baud": 230400, "leds": 1})
        await be.apply(b, 50)
        p = ports[0][2]
        assert ports[0][1] == 230400
        assert p.tx == [b"Ada\x00\x00\x55" + bytes((128, 128, 128))]
        await be.apply_color(b, {"mode": "hs", "hue": 240, "sat": 100})
        assert p.tx[-1] == b"Ada\x00\x00\x55" + bytes((0, 0, 128))
        n = len(p.tx)
        await asyncio.sleep(0.1)
        assert len(p.tx) >= n + 2 and set(p.tx[n:]) == {p.tx[n - 1]}  # the same frame again
        await be.idle(True)
        n = len(p.tx)
        await asyncio.sleep(0.08)
        assert len(p.tx) == n  # idle: no keepalive
        assert len(ports) == 1  # one open: the port stays open
        await be.stop()
        assert p.closed and p.tx[-1] != b"Ada\x00\x00\x55\x00\x00\x00"  # no dark frame at stop
    run(main())


def test_serial_wled_json(monkeypatch):
    monkeypatch.setattr(sbackend, "GREETING_S", 0.02)
    port = FakePort(greeting=b"")
    monkeypatch.setattr(sbackend, "_open_port", lambda p, baud: port)

    async def main():
        be = sbackend.SerialBackend(FakeCtx())
        b = bulb("serial:w", {"port": "loop://", "protocol": "wled", "baud": 115200})
        await be.apply(b, 50)
        await be.apply_color(b, {"mode": "hs", "hue": 0, "sat": 100})
        await be.apply(b, 0)
        lines = [json.loads(x) for x in port.tx]
        assert lines == [{"on": True, "bri": 128},
                         {"on": True, "bri": 128, "seg": [{"col": [[255, 0, 0]]}]},
                         {"on": False}]
        assert all(x.endswith(b"\n") for x in port.tx)
        await asyncio.sleep(0.05)
        assert len(port.tx) == 3  # wled: no keepalive
        await be.stop()
    run(main())


def test_serial_discover_opens_nothing(monkeypatch, tmp_path):
    byid = tmp_path / "by-id"
    byid.mkdir()
    dev = tmp_path / "ttyUSB0"
    dev.write_text("")
    (byid / "usb-1a86_USB_Serial-if00-port0").symlink_to(dev)
    monkeypatch.setattr(sbackend, "BY_ID", byid)
    port = SimpleNamespace(device=str(dev), vid=0x1A86, pid=0x7523, description="USB Serial")
    monkeypatch.setattr(sbackend, "_ports", lambda: [port])

    def no_open(*a, **kw):
        raise AssertionError("discover opened a port")

    monkeypatch.setattr(sbackend, "_open_port", no_open)
    monkeypatch.setattr(serialmod, "Serial", no_open)
    monkeypatch.setattr(serialmod, "serial_for_url", no_open)
    found = run(sbackend.SerialBackend(FakeCtx()).discover(0))
    assert found == [{"kind": "serial", "address": str(byid / "usb-1a86_USB_Serial-if00-port0"),
                      "name": "USB Serial (1a86:7523)",
                      "config": {"port": str(byid / "usb-1a86_USB_Serial-if00-port0"),
                                 "protocol": "adalight", "baud": 115200, "leds": 1}}]
    assert sbackend.SerialBackend.autoconnect is False and sbackend.SerialBackend.bluetooth is False
    if os.geteuid() != 0:
        os.chmod(dev, 0o400)
        with pytest.raises(NeedsAccess) as err:
            run(sbackend.SerialBackend(FakeCtx()).discover(0))
        assert err.value.found[0]["access"] is False


def test_serial_add_stores_by_id_and_missing_port_is_offline(monkeypatch, tmp_path):
    byid = tmp_path / "by-id"
    byid.mkdir()
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    link = byid / "usb-Arduino__www.arduino.cc__0043_7563-if00"
    link.symlink_to(dev)
    monkeypatch.setattr(sbackend, "BY_ID", byid)
    monkeypatch.setattr(sbackend, "GREETING_S", 0.02)
    port = FakePort()
    monkeypatch.setattr(sbackend, "_open_port", lambda p, baud: port)

    async def main():
        be = sbackend.SerialBackend(FakeCtx())
        cfg = await be.add({"port": str(dev), "protocol": "adalight", "baud": 115200, "leds": 3})
        assert cfg["port"] == str(link) and cfg["id"] == "usb-Arduino__www.arduino.cc__0043_7563-if00"
        b = bulb("serial:" + cfg["id"], cfg)
        await be.apply(b, 100)
        assert port.tx[0][:6] == b"Ada\x00\x02\x57"
        port.fail = True  # unplugged mid-write
        with pytest.raises(Offline):
            await be.apply(b, 50)
        assert be.status(b)["online"] is False and port.closed
        dev.unlink()
        with pytest.raises(Offline):
            await be.apply(b, 50)
        with pytest.raises(UserError):
            await be.add({"port": str(dev), "protocol": "adalight", "baud": 115200, "leds": 1})
        await be.stop()
    run(main())


# ------------------------------------------------------------------ the core's access mark

class LockedKind(MockBackend):
    kind = "locked"

    async def discover(self, timeout):
        raise NeedsAccess("no access", [{"address": "x-1", "name": "X", "config": {"address": "x-1"}}])

    async def add(self, config):
        raise NeedsAccess("no access")


def test_core_marks_access_errors(tmp_path, monkeypatch):
    reg = dict(registry.registry())
    reg["locked"] = registry.Info("locked", LockedKind, [])
    monkeypatch.setattr(registry, "_registry", reg)

    async def body(d):
        out = (await d.command({"cmd": "discover", "args": ["locked"], "which": None}))["results"][0]
        assert out["access"] == ["locked"] and out["errors"] == {"locked": "no access"}
        assert [x["address"] for x in out["discovered"]] == ["x-1"]
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"cmd": "add", "args": ["locked"], "which": None}).encode() + b"\n")
        out = bytearray()

        class Writer:
            def write(self, data):
                out.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

        await d.handle_client(reader, Writer())
        reply = json.loads(out)
        assert reply == {"error": "no access", "access": True}
    run_daemon(tmp_path, new_state(), body)
