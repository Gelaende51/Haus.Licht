"""Flick modes (schema 4), the root-setup status and run, manual levels across a restart."""

import asyncio
import copy

import pytest

from haus import rootsetup
from haus.common import UserError, read_state, save_state
from haus.daemon import Daemon
from haus.profiles import migrate, new_state
from test_core import FakeDesktop, cmd, mock, mock_state, run

CURRENT = {"status": "current", "installed": 3, "required": 3}
MISSING = {"status": "missing", "installed": None, "required": 3}


# ------------------------------------------------------------------ migration

@pytest.mark.parametrize("old, new", [(True, "on"), (False, "off"), ("sometimes", "on"), ("fast", "fast")])
def test_schema_3_flick_bool_becomes_a_mode(old, new):
    state = new_state()
    state["version"] = 3
    state["settings"]["flick"] = old
    migrate(state)
    assert state["version"] == 4 and state["settings"]["flick"] == new
    again = copy.deepcopy(state)
    assert migrate(again) is False


def test_stored_manual_levels_are_checked():
    state = mock_state(mock_1={"manual": {"level": 140, "exact": "yes"}},
                       mock_2={"manual": {"level": "x"}}, mock_3={"manual": {"level": None, "exact": True}})
    migrate(state)
    assert state["bulbs"]["mock:1"]["manual"] == {"level": 100.0, "exact": False}
    assert "manual" not in state["bulbs"]["mock:2"]
    assert state["bulbs"]["mock:3"]["manual"] == {"level": None, "exact": True}


# ------------------------------------------------------------------ setup status

def test_setup_status_from_the_stamp(tmp_path, monkeypatch):
    version = tmp_path / "setup-version"
    version.write_text("3\n")
    monkeypatch.setattr(rootsetup, "VERSION_FILE", version)
    stamp, legacy = tmp_path / "stamp", tmp_path / "conn-update"
    assert rootsetup.status(stamp, [legacy]) == {"status": "missing", "installed": None, "required": 3}
    legacy.write_text("")  # a run from before the stamp
    assert rootsetup.status(stamp, [legacy])["status"] == "outdated"
    stamp.write_text("2\n")
    assert rootsetup.status(stamp, [legacy]) == {"status": "outdated", "installed": 2, "required": 3}
    stamp.write_text("3\n")
    assert rootsetup.status(stamp, [legacy]) == {"status": "current", "installed": 3, "required": 3}
    stamp.write_text("garbage")
    assert rootsetup.status(stamp, [])["status"] == "missing"


def test_the_repo_version_is_an_integer_the_script_reads():
    assert rootsetup.required() >= 3
    assert 'version=$(cat "$here/setup-version")' in rootsetup.SCRIPT.read_text()
    assert rootsetup.command(False) == ["pkexec", str(rootsetup.SCRIPT)]
    assert rootsetup.command(True) == ["sudo", str(rootsetup.SCRIPT)]


# ------------------------------------------------------------------ modes

def test_modes_fast_and_exclusive_toggle_like_on(tmp_path, monkeypatch):
    monkeypatch.setattr(rootsetup, "status", lambda *a: dict(CURRENT))

    async def body(d):
        m = mock(d)
        for mode in ("fast", "exclusive"):
            g = await cmd(d, "flick", mode)
            assert g["flick"] == mode and g["setup"]["status"] == "current" and not g["setup"]["running"]
            assert d.setup_task is None  # setup current: nothing to run
            before = d.bulbs["mock:1"].power
            await cmd(d, "mock-flick", "mock:1")
            assert d.bulbs["mock:1"].power is (not before)
        g = await cmd(d, "set", "flick=on")
        assert g["flick"] == "on"
        with pytest.raises(UserError):
            await cmd(d, "flick", "sometimes")
        assert d.settings["flick"] == "on"
        await asyncio.sleep(0.1)
        assert m.powerup["mock:1"] in ("on", "off")
    run(tmp_path, mock_state(mock_1={}), body)


class FakeProc:
    def __init__(self, rc: int, out: bytes = b"") -> None:
        self.returncode, self.out = rc, out

    async def communicate(self):
        return self.out, None


def fake_setup(monkeypatch, rc: int, becomes_current: bool):
    """pkexec mocked: exit code rc; the status turns current only when it ran through."""
    calls, st = [], {"now": dict(MISSING)}

    async def exec_(*cmd, **kw):
        calls.append(cmd)
        if becomes_current:
            st["now"] = dict(CURRENT)
        return FakeProc(rc, b"some output\nlast line\n")

    monkeypatch.setattr(rootsetup, "status", lambda *a: dict(st["now"]))
    monkeypatch.setattr(rootsetup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(rootsetup.asyncio, "create_subprocess_exec", exec_)
    return calls


def test_fast_without_setup_is_refused_and_never_runs_pkexec(tmp_path, monkeypatch):
    """The widget runs pkexec itself (login session); the daemon only checks."""
    calls = fake_setup(monkeypatch, 0, becomes_current=True)

    async def body(d):
        for mode in ("fast", "exclusive"):
            with pytest.raises(UserError, match="setup-root"):
                await cmd(d, "flick", mode)
        assert d.settings["flick"] == "on" and d.setup_task is None and calls == []
        g = await cmd(d, "get")
        assert g["setup"]["status"] == "missing" and g["setup"]["script"] == str(rootsetup.SCRIPT)
    run(tmp_path, mock_state(mock_1={}), body)


@pytest.mark.parametrize("rc", [126, 127, 1])
def test_setup_root_failure_leaves_a_note_and_the_mode(tmp_path, monkeypatch, rc):
    calls = fake_setup(monkeypatch, rc, becomes_current=False)

    async def body(d):
        g = await cmd(d, "setup-root")
        assert g["setup"]["running"] is True
        await d.setup_task
        assert calls == [("pkexec", str(rootsetup.SCRIPT))]
        g = await cmd(d, "setup-status")
        assert g["flick"] == "on" and g["setup"]["running"] is False and g["setup"]["note"]
    run(tmp_path, mock_state(mock_1={}), body)


def test_after_the_setup_fast_is_taken(tmp_path, monkeypatch):
    calls = fake_setup(monkeypatch, 0, becomes_current=True)

    async def body(d):
        await cmd(d, "setup-root")
        await d.setup_task
        g = await cmd(d, "flick", "exclusive")
        assert g["flick"] == "exclusive" and g["setup"]["status"] == "current" and g["setup"]["note"] == ""
        assert len(calls) == 1
    run(tmp_path, mock_state(mock_1={}), body)


def test_flick_last_remembers_the_mode_before_off(tmp_path, monkeypatch):
    monkeypatch.setattr(rootsetup, "status", lambda *a: dict(CURRENT))

    async def body(d):
        assert (await cmd(d, "get"))["flick_last"] == "on"
        await cmd(d, "flick", "fast")
        g = await cmd(d, "flick", "off")
        assert g["flick"] == "off" and g["flick_last"] == "fast"
        assert read_state(d.state_path)["settings"]["flick_last"] == "fast"
    run(tmp_path, mock_state(mock_1={}), body)


def test_flick_last_migration():
    state = new_state()
    assert state["settings"]["flick_last"] == "on"
    state["settings"].update(flick="exclusive", flick_last="nonsense")
    migrate(state)
    assert state["settings"]["flick_last"] == "exclusive"
    state["settings"].update(flick="off", flick_last="off")
    migrate(state)
    assert state["settings"]["flick_last"] == "on"


def test_setup_root_command_runs_once_at_a_time(tmp_path, monkeypatch):
    calls = fake_setup(monkeypatch, 0, becomes_current=True)

    async def body(d):
        await cmd(d, "setup-root")
        await cmd(d, "setup-root")  # still running: not a second prompt
        await d.setup_task
        assert len(calls) == 1 and d.settings["flick"] == "on"
    run(tmp_path, mock_state(mock_1={}), body)


# ------------------------------------------------------------------ manual level across a restart

def test_manual_level_survives_a_restart_and_a_dark_bulb_stays_dark(tmp_path):
    path = tmp_path / "state.json"
    state = mock_state(mock_1={"power": True}, mock_2={"power": False})
    state["settings"]["mode"] = "off"  # the brightness slider sets a manual level while tracking
    save_state(path, state)

    async def first():
        d = Daemon(path, tmp_path / "sock", desktop=FakeDesktop())
        await d.start()
        follow = asyncio.create_task(d.follow())
        try:
            await cmd(d, "select", "mock:1")
            await cmd(d, "bulb", "11")
            await cmd(d, "select", "mock:2")
            await cmd(d, "bulb", "30")  # dark bulb: the level is kept, it stays dark
            await asyncio.sleep(0.5)
            assert mock(d).applied["mock:1"] == 11.0 and mock(d).applied["mock:2"] == 0.0
            assert d.bulbs["mock:1"].entry["manual"] == {"level": 11.0, "exact": False}
            assert "manual" not in read_state(path)["bulbs"]["mock:1"]  # debounced
        finally:
            follow.cancel()
            await d.stop()  # writes it now

    async def second():
        d = Daemon(path, tmp_path / "sock", desktop=FakeDesktop())
        sent = []
        await d.start()
        m = mock(d)
        orig = m.apply

        async def apply(bulb, percent):
            sent.append((bulb.id, percent))
            await orig(bulb, percent)
        m.apply = apply
        follow = asyncio.create_task(d.follow())
        try:
            await asyncio.sleep(0.5)
            assert d.bulbs["mock:1"].paused and d.bulbs["mock:1"].manual == 11.0
            assert ("mock:1", 11.0) in sent
            assert all(p == 0.0 for bid, p in sent if bid == "mock:2")  # off: never lit
            assert d.bulbs["mock:2"].power is False
            await cmd(d, "resume", which="mock:1")
            await asyncio.sleep(0.5)
            assert "manual" not in d.bulbs["mock:1"].entry
        finally:
            follow.cancel()
            await d.stop()
        assert "manual" not in read_state(path)["bulbs"]["mock:1"]

    asyncio.run(first())
    assert read_state(path)["bulbs"]["mock:1"]["manual"] == {"level": 11.0, "exact": False}
    asyncio.run(second())
