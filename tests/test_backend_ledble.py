"""Triones/LEDBlue (ledble) backend and the shared BLE lookup helpers, with
bleak and led-ble mocked: nothing here touches the Bluetooth adapter."""

import asyncio
from types import SimpleNamespace

import pytest

from haus.backends import _bulbkit, ledble
from haus.common import UserError

ADDR = "AA:BB:CC:DD:EE:FF"


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


def make_bulb(kind):
    return SimpleNamespace(id=f"{kind}:aabbccddeeff", name="B", config={"address": ADDR})


@pytest.fixture
def no_radio(monkeypatch):
    """Any real scanner or BlueZ lookup fails the test."""
    import bleak
    import bleak_retry_connector

    class Forbidden:
        def __init__(self, *a, **kw):
            raise AssertionError("a real BleakScanner was created")

        @classmethod
        async def find_device_by_address(cls, *a, **kw):
            raise AssertionError("a real scan was started")

    async def forbidden_get_device(address):
        raise AssertionError("BlueZ was asked")

    monkeypatch.setattr(bleak, "BleakScanner", Forbidden)
    monkeypatch.setattr(bleak_retry_connector, "get_device", forbidden_get_device)


# ------------------------------------------------------------------ _bulbkit BLE helpers


def test_ble_device_prefers_bluez_cache(no_radio, monkeypatch):
    import bleak_retry_connector
    dev = SimpleNamespace(address=ADDR)

    async def cached(address):
        return dev

    monkeypatch.setattr(bleak_retry_connector, "get_device", cached)
    assert asyncio.run(_bulbkit.ble_device(ADDR)) is dev  # no scan at all


def test_ble_device_short_scan_and_refusal(no_radio, monkeypatch):
    import bleak
    import bleak_retry_connector
    calls = []

    async def unknown(address):
        return None

    class Scanner:
        refuse = False

        @classmethod
        async def find_device_by_address(cls, address, timeout):
            calls.append((address, timeout))
            if cls.refuse:
                raise RuntimeError("org.bluez.Error.InProgress")
            return SimpleNamespace(address=address)

    monkeypatch.setattr(bleak_retry_connector, "get_device", unknown)
    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    assert asyncio.run(_bulbkit.ble_device(ADDR)).address == ADDR
    Scanner.refuse = True
    assert asyncio.run(_bulbkit.ble_device(ADDR)) is None  # the mesh scan runs: not found, no error
    assert calls == [(ADDR, _bulbkit.BLE_FIND_S)] * 2


def test_ble_scan_falls_back_to_bluez_known(no_radio, monkeypatch):
    import bleak

    class Refusing:
        def __init__(self, detection_callback):
            pass

        async def __aenter__(self):
            raise RuntimeError("org.bluez.Error.InProgress")

        async def __aexit__(self, *a):
            return False

    async def known():
        return [(SimpleNamespace(address="11:22:33:44:55:66", name="Triones:ABC"),
                 SimpleNamespace(local_name="Triones:ABC", service_uuids=[])),
                (SimpleNamespace(address="11:22:33:44:55:77", name="Other"),
                 SimpleNamespace(local_name="Other", service_uuids=[]))]

    monkeypatch.setattr(bleak, "BleakScanner", Refusing)
    monkeypatch.setattr(_bulbkit, "_bluez_known", known)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep(asyncio.sleep))
    found = asyncio.run(ledble.LedBleBackend(FakeCtx()).discover(5))
    assert [f["address"] for f in found] == ["11:22:33:44:55:66"]


def _fast_sleep(real):
    async def sleep(t, *a):
        await real(0)
    return sleep


# ------------------------------------------------------------------ ledble


class FakeLED:
    instances = []

    def __init__(self, dev):
        self.dev = dev
        self.on = False
        self.w = 0
        self.rgb_unscaled = (0, 0, 0)
        self.effect = None
        self.brightness = 0
        self.calls = []
        self.fail = None
        FakeLED.instances.append(self)

    async def _maybe_fail(self):
        if self.fail:
            raise self.fail

    async def update(self):
        await self._maybe_fail()
        self.calls.append("update")

    async def turn_on(self):
        await self._maybe_fail()
        self.on = True
        self.calls.append("on")

    async def turn_off(self):
        await self._maybe_fail()
        self.on = False
        self.calls.append("off")

    async def set_brightness(self, level):
        self.brightness = level
        self.calls.append(("bri", level))

    async def set_rgb(self, rgb, level):
        self.rgb_unscaled, self.brightness = rgb, level
        self.calls.append(("rgb", rgb, level))

    async def stop(self):
        self.calls.append("stop")


@pytest.fixture
def leds(monkeypatch, no_radio):
    import led_ble
    FakeLED.instances = []
    monkeypatch.setattr(led_ble, "LEDBLE", FakeLED)
    seen = {"dev": SimpleNamespace(address=ADDR)}

    async def lookup(address, timeout=None):
        return seen["dev"]

    monkeypatch.setattr(ledble, "ble_device", lookup)
    return seen


def test_ledble_apply(leds):
    async def main():
        b = ledble.LedBleBackend(FakeCtx())
        bulb = make_bulb("ledble")
        await b.apply(bulb, 50)
        led = FakeLED.instances[0]
        assert led.calls == ["update", "on", ("rgb", (255, 255, 255), 128)]  # black: white
        led.rgb_unscaled = (255, 0, 0)
        await b.apply(bulb, 0.2)
        assert led.calls[-1] == ("rgb", (255, 0, 0), 1)
        led.w = 200
        await b.apply(bulb, 100)
        assert led.calls[-1] == ("bri", 255)
        await b.apply(bulb, 0)
        assert led.calls[-1] == "off"
        assert led.calls.count("update") == 1
        assert b.status(bulb)["online"] is True
        await b.stop()
        assert led.calls[-1] == "stop"

    asyncio.run(main())


def test_ledble_failure_drops_device_and_raises(leds):
    async def main():
        b = ledble.LedBleBackend(FakeCtx())
        bulb = make_bulb("ledble")
        leds["dev"] = None
        with pytest.raises(ConnectionError):
            await b.apply(bulb, 50)
        assert b.status(bulb)["online"] is False
        leds["dev"] = SimpleNamespace(address=ADDR)
        await b.apply(bulb, 50)
        FakeLED.instances[0].fail = RuntimeError("BleakError: Not connected")
        with pytest.raises(ConnectionError, match="Not connected"):
            await b.apply(bulb, 0)
        assert bulb.id not in b.leds and b.status(bulb)["online"] is False
        await b.apply(bulb, 20)  # a fresh device object
        assert len(FakeLED.instances) == 2 and b.status(bulb)["online"] is True

    asyncio.run(main())


def test_ledble_add_validates_address():
    b = ledble.LedBleBackend(FakeCtx())
    assert asyncio.run(b.add({"address": "aa-bb-cc-dd-ee-ff"}))["id"] == "aabbccddeeff"
    with pytest.raises(UserError):
        asyncio.run(b.add({"address": ""}))
