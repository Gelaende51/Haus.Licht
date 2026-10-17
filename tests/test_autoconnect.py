"""Autoconnect with its ignore list, default bulb names, per-backend brightness steps."""

import asyncio
from types import SimpleNamespace

import pytest

from haus import backends as registry
from haus.backends.mock import MockBackend
from haus.common import UserError
from haus.profiles import migrate, new_state
from test_core import cmd, mock, run


def ids(g):
    return [b["id"] for b in g["bulbs_list"]]


def test_autoconnect_is_off_by_default_and_migrated(tmp_path):
    state = new_state()
    assert state["settings"]["autoconnect"] is False and state["settings"]["autoconnect_ignore"] == []
    state["settings"]["autoconnect"] = "on"
    state["settings"]["autoconnect_ignore"] = ["mock:x", {"address": "AA"}, 7, {"name": "no id"}]
    migrate(state)
    assert state["settings"]["autoconnect"] is True
    assert state["settings"]["autoconnect_ignore"] == [{"id": "mock:x", "address": "", "name": ""},
                                                       {"id": "", "address": "AA", "name": ""}]

    async def body(d):
        await asyncio.sleep(0.8)  # the loop runs, but autoconnect is off
        g = await cmd(d, "get")
        assert g["autoconnect"] is False and g["autoconnect_ignore"] == [] and ids(g) == []
    run(tmp_path, new_state(), body)


def test_switching_on_adds_new_devices_in_use(tmp_path):
    async def body(d):
        g = await cmd(d, "set", "autoconnect=on")
        assert g["autoconnect"] is True
        for _ in range(40):  # the round starts at once, discovery takes 0.5 s
            if len(d.bulbs) == 2:
                break
            await asyncio.sleep(0.05)
        g = await cmd(d, "get")
        assert ids(g) == ["mock:mock-1", "mock:mock-2"]
        one = g["bulbs_list"][0]
        assert one["use"] is True and one["autoconnected"] is True and one["name"] == "Mock bulb - mock-1"
        assert g["in_widget"] == ["mock:mock-1", "mock:mock-2"]
        assert await d.autoconnect_round() == []  # known now: not added twice
        g = await cmd(d, "autoconnect", "off")
        assert g["autoconnect"] is False
        with pytest.raises(ValueError):
            await cmd(d, "set", "autoconnect=maybe")
    run(tmp_path, new_state(), body)


def test_removed_autoconnected_bulbs_are_ignored_until_unignored(tmp_path):
    async def body(d):
        await cmd(d, "add", "mock", "address=CC", "name=Manual")
        assert await d.autoconnect_round() == ["mock:mock-1", "mock:mock-2"]
        g = await cmd(d, "remove", "mock:mock-1")
        assert g["autoconnect_ignore"] == [{"id": "mock:mock-1", "address": "mock-1", "name": "Mock bulb - mock-1"}]
        await cmd(d, "remove", "mock:cc")  # added by hand: not ignored
        assert len((await cmd(d, "get"))["autoconnect_ignore"]) == 1
        d.auto_seen.clear()  # as after a restart
        assert await d.autoconnect_round() == []
        assert "mock:mock-1" not in d.bulbs
        with pytest.raises(UserError):
            await cmd(d, "unignore", "mock:nothing")
        g = await cmd(d, "unignore", "mock:mock-1")
        assert g["autoconnect_ignore"] == []
        assert await d.autoconnect_round() == ["mock:mock-1"]
        # by address as well
        await cmd(d, "remove", "mock:mock-1")
        await cmd(d, "unignore", "mock-1")
        assert (await cmd(d, "get"))["autoconnect_ignore"] == []
    run(tmp_path, new_state(), body)


def test_a_kind_that_forbids_it_is_never_discovered_or_added(tmp_path, monkeypatch):
    from haus.backends.mesh import MeshBackend
    assert MeshBackend.autoconnect is False  # adopting a mesh bulb changes the device: by hand only

    calls = []

    class NoAuto(MockBackend):
        kind = "noauto"
        autoconnect = False

        async def discover(self, timeout):
            calls.append(timeout)
            return [{"kind": "noauto", "address": "x", "name": "x", "config": {"address": "x"}}]

    reg = dict(registry.registry())
    reg["noauto"] = registry.Info("noauto", NoAuto, [])
    monkeypatch.setattr(registry, "_registry", reg)

    async def body(d):
        added = await d.autoconnect_round()
        assert added == ["mock:mock-1", "mock:mock-2"] and calls == []
        found = await cmd(d, "discover", "noauto")  # manual discovery still works
        assert [x["address"] for x in found["discovered"]] == ["x"]
    run(tmp_path, new_state(), body)


def test_a_refused_device_is_tried_once_per_run(tmp_path, monkeypatch):
    async def body(d):
        tries = []
        backend = d.instance("mock")

        async def refuse(config):
            tries.append(config)
            raise UserError("needs a key")
        monkeypatch.setattr(backend, "add", refuse)
        assert await d.autoconnect_round() == []
        assert await d.autoconnect_round() == []
        assert len(tries) == 2  # two devices, each once
    run(tmp_path, new_state(), body)


def test_default_names_product_dash_id(tmp_path):
    async def body(d):
        g = await cmd(d, "add", "mock")
        assert d.bulbs[g["added"]].name == "Mock bulb - 1"
        g = await cmd(d, "add", "mock", "address=AA:BB")
        assert d.bulbs[g["added"]].name == "Mock bulb - aabb"
        g = await cmd(d, "add", "mock", "name=Desk")
        assert d.bulbs[g["added"]].name == "Desk"
    run(tmp_path, new_state(), body)

    from haus.backends.mesh import MeshBackend
    mesh = MeshBackend.__new__(MeshBackend)
    assert mesh.product_name({"unicast": "0x0002", "scale": 100}) == "Hauslicht"
    assert mesh.product_name({"unicast": "0x0003", "scale": 65535}) == "Mesh light"


def test_brightness_step_per_backend(tmp_path):
    def bulb(**config):
        return SimpleNamespace(id="x", name="x", config=config)

    from haus.backends.mesh import MeshBackend
    mesh = MeshBackend.__new__(MeshBackend)
    assert mesh.step(bulb(unicast="0x0002", scale=100)) == 1.0
    assert mesh.min_lit(bulb(unicast="0x0002", scale=100)) == 0.5  # the glow floor stays below the step
    assert mesh.step(bulb(unicast="0x0002", scale=65535)) == pytest.approx(100 / 65535)
    import importlib
    by_kind = {}
    for module in ("lifx", "matter", "tuya", "mqtt", "ledble", "wiz", "homeassistant", "hue", "huebt"):
        mod = importlib.import_module(f"haus.backends.{module}")
        cls = next(c for c in vars(mod).values() if isinstance(c, type) and issubclass(c, registry.Backend)
                   and c is not registry.Backend and c.__module__ == mod.__name__)
        by_kind[module] = cls.__new__(cls)
    assert by_kind["lifx"].step(bulb()) == pytest.approx(100 / 65535)
    assert by_kind["matter"].step(bulb()) == pytest.approx(100 / 254)
    assert by_kind["tuya"].step(bulb(bulb_type="B")) == pytest.approx(0.1)
    assert by_kind["tuya"].step(bulb(bulb_type="A")) == pytest.approx(100 / 255)
    assert by_kind["mqtt"].step(bulb(mode="zigbee2mqtt")) == pytest.approx(100 / 254)
    assert by_kind["mqtt"].step(bulb(mode="generic", payload="{value}", scale=1000)) == pytest.approx(0.1)
    assert by_kind["mqtt"].step(bulb(mode="generic", payload="{percent}")) == 1.0
    assert by_kind["ledble"].step(bulb()) == pytest.approx(100 / 255)
    assert by_kind["homeassistant"].step(bulb()) == pytest.approx(100 / 255)
    assert by_kind["hue"].step(bulb()) == pytest.approx(100 / 254)
    assert by_kind["huebt"].step(bulb()) == pytest.approx(100 / 254)
    assert by_kind["wiz"].step(bulb()) == 1.0  # unknown / whole percent: the base default

    async def body(d):
        await cmd(d, "add", "mock")
        g = await cmd(d, "get")
        assert g["bulbs_list"][0]["step"] == 1.0
    run(tmp_path, new_state(), body)
