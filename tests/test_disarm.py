"""The arm marker (common.armed_path) and `hauslicht disarm`, the unit's ExecStopPost."""

import asyncio
import os
import time

import pytest

from haus import backends as registry
from haus import disarm
from haus.backends.mock import MockBackend
from haus.common import armed_path, link_lock, read_armed, read_state, save_state, write_armed
from test_core import cmd, mock, mock_state, run, settle


def test_marker_written_on_arm_and_cleared_on_on(tmp_path):
    path = tmp_path / "state.json"

    async def body(d):
        assert read_armed(path) == {}
        await cmd(d, "on")  # lit: armed "off", so a flick boots it dark
        await settle(d)
        assert mock(d).powerup["mock:1"] == "off"
        assert read_armed(path) == {"mock:1": "off"}
        assert oct(armed_path(path).stat().st_mode & 0o777) == "0o600"
        await cmd(d, "off")  # dark: armed "on" again
        await settle(d)
        assert mock(d).powerup["mock:1"] == "on"
        assert read_armed(path) == {}
        await cmd(d, "on")
        await settle(d)
        assert read_armed(path) == {"mock:1": "off"}
    run(tmp_path, mock_state(mock_1={}), body)
    assert read_armed(path) == {}  # the clean stop set "on" and cleared it
    assert "armed" not in read_state(path)["bulbs"]["mock:1"]  # never in the state file


def test_an_unanswered_set_keeps_the_bulb_on_the_marker(tmp_path):
    path = tmp_path / "state.json"

    async def body(d):
        m = mock(d)
        await cmd(d, "on")
        await settle(d)
        assert read_armed(path) == {"mock:1": "off"}
        real = m.set_powerup

        async def deaf(bulb, value):
            await real(bulb, value)
            return False
        m.set_powerup = deaf  # the shutdown's "on" is not answered either
    run(tmp_path, mock_state(mock_1={}), body)
    assert read_armed(path) == {"mock:1": "off"}  # left for disarm


def test_suspend_logs_each_bulb_and_keeps_an_unanswered_one(tmp_path, caplog):
    path = tmp_path / "state.json"

    async def body(d):
        m = mock(d)
        await cmd(d, "on")
        await settle(d)
        real = m.set_powerup
        answers = [False]

        async def flaky(bulb, value):
            ok = await real(bulb, value)
            return answers.pop(0) if answers else ok
        m.set_powerup = flaky
        caplog.set_level("INFO", logger="hauslicht")
        await d.before_sleep()
        assert "suspend: mock:1 OnPowerUp on (no answer)" in caplog.text
        assert read_armed(path) == {"mock:1": "off"}
        await d.before_sleep()  # still asleep (no worker re-arms); this time it answers
        assert "suspend: mock:1 OnPowerUp on (ok)" in caplog.text
        assert read_armed(path) == {}
        await d.after_wake()
    run(tmp_path, mock_state(mock_1={}), body)


def _instances(monkeypatch):
    made = []

    class Recording(MockBackend):
        def __init__(self, ctx):
            super().__init__(ctx)
            made.append(self)

    real = registry.backend_class
    monkeypatch.setattr(registry, "backend_class", lambda kind: Recording if kind == "mock" else real(kind))
    return made


def test_disarm_with_an_empty_marker_does_nothing(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1={}))
    made = _instances(monkeypatch)
    monkeypatch.setattr(disarm, "_disarm", None)  # never reached
    assert disarm.run(path, if_armed=True) == 0
    write_armed(path, {})
    assert disarm.run(path, if_armed=True) == 0
    assert made == []
    assert not path.with_suffix(".lock").exists()  # not even the lock was taken


def test_disarm_after_a_clean_stop_leaves_the_radio_alone(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1={}))
    write_armed(path, {"mock:1": "off"})  # no power at the wall when the daemon stopped
    made = _instances(monkeypatch)
    monkeypatch.setenv("SERVICE_RESULT", "success")
    assert disarm.run(path, if_armed=True) == 0
    assert made == []
    assert read_armed(path) == {"mock:1": "off"}
    monkeypatch.setenv("SERVICE_RESULT", "signal")
    assert disarm.run(path, if_armed=True) == 0
    assert read_armed(path) == {}


def test_disarm_sets_on_for_the_listed_bulbs_and_clears_them(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1={}, mock_2={}, mock_3={}))
    write_armed(path, {"mock:1": "off", "mock:3": "restore", "mock:9": "off"})  # mock:9 was removed since
    made = _instances(monkeypatch)
    assert disarm.run(path, if_armed=True) == 0
    assert len(made) == 1
    assert [c for c in made[0].calls if c[0] == "powerup"] == [("powerup", "mock:1", "on"), ("powerup", "mock:3", "on")]
    assert read_armed(path) == {}


def test_disarm_keeps_a_bulb_that_did_not_answer(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1={}, mock_2={}))
    write_armed(path, {"mock:1": "off", "mock:2": "off"})

    class Half(MockBackend):
        async def set_powerup(self, bulb, value):
            await super().set_powerup(bulb, value)
            return bulb.id == "mock:2"

    monkeypatch.setattr(registry, "backend_class", lambda kind: Half)
    assert disarm.run(path, if_armed=True) == 1
    assert read_armed(path) == {"mock:1": "off"}


def test_disarm_refuses_while_the_link_lock_is_held(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1={}))
    write_armed(path, {"mock:1": "off"})
    made = _instances(monkeypatch)
    with link_lock(path):  # a daemon (or a one-shot command) runs
        assert disarm.run(path, if_armed=True) == 1
    assert made == []
    assert read_armed(path) == {"mock:1": "off"}


def test_disarm_has_a_hard_time_limit(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, mock_state(mock_1={}))
    write_armed(path, {"mock:1": "off"})

    class Hung(MockBackend):
        async def set_powerup(self, bulb, value):
            await asyncio.sleep(60)

        async def stop(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(registry, "backend_class", lambda kind: Hung)
    monkeypatch.setattr(disarm, "DISARM_S", 0.4)
    monkeypatch.setattr(disarm, "CLOSE_S", 0.2)
    start = time.monotonic()
    assert disarm.run(path, if_armed=True) == 1
    assert time.monotonic() - start < 1.5
    assert read_armed(path) == {"mock:1": "off"}


class FakeLink:
    """mesh.MeshLink as the one-shot commands use it: SEQ reserved and saved at connect."""
    made = []

    def __init__(self, state, save):
        self.state, self.save, self.sets, self.closed = state, save, [], False
        FakeLink.made.append(self)

    async def connect(self):
        self.state["seq"] += 64
        self.save()

    async def set_onpowerup(self, addr, value):
        self.sets.append((addr, value))
        return None if addr == 0x0003 else value

    async def close(self):
        self.closed = True


def test_disarm_mesh_uses_the_one_shot_link_and_keeps_the_seq(tmp_path, monkeypatch):
    from haus.backends import mesh
    path = tmp_path / "state.json"
    state = mock_state()
    state.update({"seq": 1000, "netkey": "00" * 16, "appkey": "00" * 16, "devices": {}})
    for a in (2, 3, 4):
        state["bulbs"][f"mesh:{a:04x}"] = {"kind": "mesh", "name": f"m{a}", "profile": "default", "power": True,
                                           "config": {"unicast": f"0x{a:04x}", "scale": 100}}
    save_state(path, state)
    write_armed(path, {"mesh:0002": "off", "mesh:0003": "off"})
    FakeLink.made = []
    monkeypatch.setattr(mesh, "MeshLink", FakeLink)

    async def ready():
        return True
    monkeypatch.setattr(disarm, "_bluetooth_ready", ready)
    assert disarm.run(path, if_armed=True) == 1  # 0x0003 did not answer
    (link,) = FakeLink.made
    assert link.sets == [(2, 1), (3, 1)] and link.closed  # mesh:0004 was not listed
    assert read_armed(path) == {"mesh:0003": "off"}
    assert read_state(path)["seq"] == 1064  # the reservation reached the state file


def test_disarm_skips_the_mesh_without_bluetooth(tmp_path, monkeypatch):
    from haus.backends import mesh
    path = tmp_path / "state.json"
    state = mock_state()
    state["bulbs"]["mesh:0002"] = {"kind": "mesh", "name": "m", "profile": "default", "power": True,
                                   "config": {"unicast": "0x0002"}}
    save_state(path, state)
    write_armed(path, {"mesh:0002": "off"})
    FakeLink.made = []
    monkeypatch.setattr(mesh, "MeshLink", FakeLink)

    async def not_ready():
        return False
    monkeypatch.setattr(disarm, "_bluetooth_ready", not_ready)
    assert disarm.run(path, if_armed=True) == 1
    assert FakeLink.made == []
    assert read_armed(path) == {"mesh:0002": "off"}


@pytest.mark.parametrize("bluez, sysfs, want", [
    ({"hci0": {"powered": True, "devices": []}}, [{"name": "hci0"}], True),
    ({}, [{"name": "hci0"}], True),  # BlueZ not askable: the connect tells
    ({"hci0": {"powered": False, "devices": []}}, [{"name": "hci0"}], False),  # off / rfkilled
    ({"hci1": {"powered": True, "devices": []}}, [{"name": "hci0"}, {"name": "hci1"}], True),  # hci0 still held
    ({"hci0": {"powered": True, "devices": []}}, [], False),  # no adapter at all
])
def test_bluetooth_ready(monkeypatch, bluez, sysfs, want):
    from haus import hci
    monkeypatch.setattr(hci, "scan_sysfs", lambda: sysfs)

    async def adapters():
        return bluez
    monkeypatch.setattr(hci, "bluez_adapters", adapters)
    monkeypatch.setattr(disarm, "ADAPTER_WAIT_S", 0.0)
    assert asyncio.run(disarm._bluetooth_ready()) is want


def test_the_unit_and_the_installer_carry_the_same_stop_post():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    unit = open(os.path.join(root, "hauslicht.service")).read()
    install = open(os.path.join(root, "install", "install.sh")).read()
    assert "\nExecStopPost=-" in unit and unit.count("disarm --if-armed") == 1
    assert "s|^ExecStopPost=.*|ExecStopPost=-$dir/.venv/bin/python $dir/hauslicht.py disarm --if-armed|" in install
