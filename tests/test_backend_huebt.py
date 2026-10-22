"""Hue Bluetooth (huebt) backend with bleak and HueBLE mocked: nothing here
touches the Bluetooth adapter."""

import asyncio
from types import SimpleNamespace

import pytest

from haus.backends import huebt
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

    def save(self):
        self.events.append(("save",))


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


def test_ble_scan_matches_adverts(no_radio, monkeypatch):
    import bleak

    class Scanner:
        def __init__(self, detection_callback):
            self.cb = detection_callback

        async def __aenter__(self):
            self.cb(SimpleNamespace(address="aa:00:00:00:00:01", name=None),
                    SimpleNamespace(local_name="Hue color lamp",
                                    service_uuids=["0000FE0F-0000-1000-8000-00805F9B34FB"]))
            self.cb(SimpleNamespace(address="aa:00:00:00:00:02", name="x"),
                    SimpleNamespace(local_name="x", service_uuids=[]))
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(bleak, "BleakScanner", Scanner)
    found = asyncio.run(huebt.HueBtBackend(FakeCtx()).discover(0))
    assert found == [{"kind": "huebt", "address": "AA:00:00:00:00:01", "name": "Hue color lamp",
                      "config": {"address": "AA:00:00:00:00:01"}}]


# ------------------------------------------------------------------ huebt


class FakeHueLight:
    instances = []
    fail_connect = False
    features = (False, False)  # (colour temperature, xy colour) of the next light

    def __init__(self, dev):
        self.dev = dev
        self.connected = False
        self.writes = []
        self.supports_colour_temp, self.supports_colour_xy = FakeHueLight.features
        self.minimum_mireds = 153 if self.supports_colour_temp else None
        self.maximum_mireds = 500 if self.supports_colour_temp else None
        FakeHueLight.instances.append(self)

    async def connect(self, **kw):
        import HueBLE
        if FakeHueLight.fail_connect:
            raise HueBLE.ConnectionError("pairing refused")
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def set_power(self, on):
        self.writes.append(("power", on))

    async def set_brightness(self, value):
        self.writes.append(("bri", value))

    async def set_colour_xy(self, x, y):
        self.writes.append(("xy", x, y))

    async def set_colour_temp(self, mired):
        self.writes.append(("ct", mired))

    async def poll_power_state(self):
        return True

    async def poll_brightness(self):
        return 127


@pytest.fixture
def hue(monkeypatch, no_radio):
    import HueBLE
    FakeHueLight.instances = []
    FakeHueLight.fail_connect = False
    FakeHueLight.features = (False, False)
    monkeypatch.setattr(HueBLE, "HueBleLight", FakeHueLight)
    monkeypatch.setattr(HueBLE, "DEFAULT_MAX_RECONNECT_ATTEMPTS", -1)
    seen = {"dev": SimpleNamespace(address=ADDR)}

    async def lookup(address, timeout=None):
        return seen["dev"]

    monkeypatch.setattr(huebt, "ble_device", lookup)
    monkeypatch.setattr(huebt, "IDLE_S", 0.4)
    return seen


def test_huebt_apply_links_on_demand_and_idles_out(hue):
    async def main():
        ctx = FakeCtx()
        b = huebt.HueBtBackend(ctx)
        bulb = make_bulb("huebt")
        assert b.status(bulb) == {"connected": False, "online": None}
        await b.apply(bulb, 50)
        await b.apply(bulb, 0.3)
        await b.apply(bulb, 0)
        light = FakeHueLight.instances[0]
        assert light.writes == [("bri", 127), ("power", True), ("bri", 1), ("power", False)]
        assert b.status(bulb) == {"connected": True, "online": True}
        import HueBLE
        assert HueBLE.DEFAULT_MAX_RECONNECT_ATTEMPTS == 0  # no reconnect loop of HueBLE's own
        await asyncio.sleep(0.6)  # idle: the link is given back
        assert b.status(bulb)["connected"] is False and not light.connected
        await b.apply(bulb, 100)
        assert len(FakeHueLight.instances) == 2
        assert FakeHueLight.instances[1].writes == [("bri", 254), ("power", True)]
        assert await b.query(bulb) == {"on": True, "percent": 50.0}
        await b.stop()

    asyncio.run(main())


def test_huebt_unreachable_raises_for_backoff(hue):
    async def main():
        b = huebt.HueBtBackend(FakeCtx())
        bulb = make_bulb("huebt")
        hue["dev"] = None
        with pytest.raises(ConnectionError, match="not seen"):
            await b.apply(bulb, 50)
        assert b.status(bulb)["online"] is False
        hue["dev"] = SimpleNamespace(address=ADDR)
        FakeHueLight.fail_connect = True
        with pytest.raises(ConnectionError, match="pairing refused"):
            await b.apply(bulb, 50)
        FakeHueLight.fail_connect = False
        await b.apply(bulb, 50)
        assert b.status(bulb)["online"] is True
        await b.stop()

    asyncio.run(main())


def test_huebt_add_validates_address():
    b = huebt.HueBtBackend(FakeCtx())
    cfg = asyncio.run(b.add({"address": " aa:bb:cc:dd:ee:ff"}))
    assert cfg == {"address": ADDR, "id": "aabbccddeeff"}
    with pytest.raises(UserError):
        asyncio.run(b.add({"address": "nope"}))
    assert b.experimental and b.caps(SimpleNamespace(id="huebt:x", config=cfg)) == {"onoff", "dim"}
    assert all(f["label_de"] for f in b.fields)


@pytest.mark.parametrize("features,caps", [((False, False), []), ((True, False), ["ct"]),
                                           ((True, True), ["ct", "hs"]), ((False, True), ["hs"])])
def test_huebt_colour_caps_learned_on_first_link(hue, features, caps):
    async def main():
        FakeHueLight.features = features
        b = huebt.HueBtBackend(FakeCtx())
        bulb = make_bulb("huebt")
        assert b.caps(bulb) == {"onoff", "dim"}  # add does not connect: nothing known yet
        await b.apply(bulb, 50)
        assert bulb.config["color"] == caps
        assert b.caps(bulb) == {"onoff", "dim"} | set(caps)
        if "ct" in caps:
            assert bulb.config["kelvin"] == [2000, 6536] and b.ct_range(bulb) == (2000, 6536)
        else:
            assert "kelvin" not in bulb.config
        await b.stop()

    asyncio.run(main())


def test_huebt_colour_writes_once_and_never_to_a_dark_bulb(hue):
    async def main():
        FakeHueLight.features = (True, True)
        b = huebt.HueBtBackend(FakeCtx())
        bulb = make_bulb("huebt")
        red = {"mode": "hs", "hue": 0, "sat": 100}
        assert await b.apply_color(bulb, red) is False  # no link, not driven
        await b.apply(bulb, 0)
        light = FakeHueLight.instances[0]
        assert await b.apply_color(bulb, red) is False  # dark: never switched on
        assert light.writes == [("power", False)]
        await b.apply(bulb, 50)
        assert await b.apply_color(bulb, red) is True
        assert await b.apply_color(bulb, red) is True  # unchanged: no second write
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 2500}) is True
        assert await b.apply_color(bulb, {"mode": "ct", "kelvin": 1000}) is True  # clamped to 500 mired
        assert light.writes[1:] == [("bri", 127), ("power", True), ("xy", 0.735, 0.265), ("ct", 400), ("ct", 500)]
        await b.apply(bulb, 0)
        n = len(light.writes)
        assert await b.apply_color(bulb, red) is False and len(light.writes) == n
        await b.apply(bulb, 30)
        await b._close(bulb.id)  # the link went: its state with it
        assert await b.apply_color(bulb, red) is False
        await b.apply(bulb, 30)  # a new link: the colour is written again
        assert await b.apply_color(bulb, red) is True
        assert FakeHueLight.instances[-1].writes[-1] == ("xy", 0.735, 0.265)
        await b.stop()

    asyncio.run(main())
