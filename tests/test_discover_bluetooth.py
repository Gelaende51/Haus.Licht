"""discover bluetooth: every Bluetooth kind at once, per-kind errors, known."""

import pytest

from haus import backends as registry
from haus.backends import _bulbkit
from haus.backends.mock import MockBackend
from haus.common import UserError
from haus.profiles import new_state
from test_core import cmd, run


class BtMock(MockBackend):
    kind = "btmock"
    bluetooth = True

    async def discover(self, timeout):
        return [{"address": "AA:BB:CC:DD:EE:01", "name": "One", "config": {"address": "AA:BB:CC:DD:EE:01"}}]


class BtBroken(MockBackend):
    kind = "btbroken"
    bluetooth = True

    async def discover(self, timeout):
        raise UserError("radio on fire")


class BtHeld(MockBackend):
    """Scans like huebt/ledble: through ble_scan, refused when BlueZ lost the adapter."""
    kind = "btheld"
    bluetooth = True

    async def discover(self, timeout):
        if _bulbkit.RADIO_HELD.get():
            raise _bulbkit.ScanRefused("No Bluetooth adapters found.")
        return []


@pytest.fixture
def bt_registry(monkeypatch):
    reg = dict(registry.registry())
    for cls in (BtMock, BtBroken, BtHeld):
        reg[cls.kind] = registry.Info(cls.kind, cls, [])
    monkeypatch.setattr(registry, "_registry", reg)
    return reg


def test_bluetooth_flag_is_listed():
    from haus.backends.huebt import HueBtBackend
    from haus.backends.ledble import LedBleBackend
    from haus.backends.mesh import MeshBackend
    assert MeshBackend.bluetooth and HueBtBackend.bluetooth and LedBleBackend.bluetooth
    assert MockBackend.bluetooth is False
    assert registry.Info("x", BtMock, []).describe()["bluetooth"] is True


def test_discover_bluetooth_merges_kinds_and_reports_errors(tmp_path, bt_registry):
    async def body(d):
        out = await cmd(d, "discover", "bluetooth")
        assert {x["kind"] for x in out["discovered"]} == {"btmock"}  # mock is not Bluetooth
        assert out["discovered"][0]["known"] is False
        assert out["errors"] == {"btbroken": "radio on fire"}
        # every kind: the network mock too, errors still per kind
        out = await cmd(d, "discover")
        assert {x["kind"] for x in out["discovered"]} == {"btmock", "mock"}
        assert set(out["errors"]) == {"btbroken"}
    run(tmp_path, new_state(), body)


def test_discover_marks_known(tmp_path, bt_registry):
    async def body(d):
        await cmd(d, "add", "mock", "address=mock-1")
        out = await cmd(d, "discover", "mock")
        known = {x["address"]: x["known"] for x in out["discovered"]}
        assert known == {"mock-1": True, "mock-2": False}
        assert out["errors"] == {}
    run(tmp_path, new_state(), body)


def test_exclusive_gives_a_clear_error(tmp_path, bt_registry, monkeypatch):
    async def body(d):
        out = await cmd(d, "discover", "bluetooth")
        assert "btheld" not in out["errors"]
        monkeypatch.setattr(d, "exclusive_state", lambda: {"active": True, "adapter": "hci0", "note": ""})
        out = await cmd(d, "discover", "bluetooth")
        assert out["errors"]["btheld"].startswith(("No Bluetooth adapter", "Kein Bluetooth-Adapter"))
        assert [x["kind"] for x in out["discovered"]] == ["btmock"]
    run(tmp_path, new_state(), body)


def test_ble_scan_refused_while_held(monkeypatch):
    import asyncio
    import sys
    import types

    class Refusing:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise RuntimeError("No Bluetooth adapters found.")

        async def __aexit__(self, *a):
            return False
    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakScanner=Refusing))

    async def held():
        _bulbkit.RADIO_HELD.set(True)
        return await _bulbkit.ble_scan(lambda d, a: None, 0.01)
    with pytest.raises(_bulbkit.ScanRefused):
        asyncio.run(held())
