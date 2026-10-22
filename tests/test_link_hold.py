"""The connection indicator: `disconnect` holds bulbs off their link (the
idle path: OnPowerUp "on", then the link may go), nothing reconnects a held
bulb, the hold survives a restart, `connect` ends it and retries a failed
link at once."""

import asyncio
import json
import time

import pytest

from haus import cli
from haus.backends.mock import MockBackend
from haus.common import UserError, _, save_state
from haus.daemon import BACKOFF_S, Daemon
from test_cli import _logger_levels, with_cli  # noqa: F401  (autouse: cli.main sets logger levels)
from test_core import FakeDesktop, cmd, mock, mock_state, run, settle


def lit_pair():
    return mock_state(mock_1={"power": True}, mock_2={"power": True})


def since(m, n):
    return m.calls[n:]


def test_disconnect_takes_the_idle_path_and_leaves_the_light_alone(tmp_path):
    async def body(d):
        m = mock(d)
        await settle(d)
        assert m.applied == {"mock:1": 50.0, "mock:2": 50.0}
        assert m.powerup == {"mock:1": "off", "mock:2": "off"}  # flick mode: armed to the toggle
        assert set(d.armed_marker) == {"mock:1", "mock:2"}
        n = len(m.calls)
        g = await cmd(d, "disconnect")
        await settle(d)
        after = since(m, n)
        assert ("powerup", "mock:1", "on") in after and ("powerup", "mock:2", "on") in after
        assert after[-1] == ("idle", None, True)  # the link goes only after the last "on"
        assert not [c for c in after if c[0] in ("apply", "color", "fade")]  # the light as it was
        assert d.armed_marker == {}  # nothing for ExecStopPost's disarm to do
        assert all(d.bulbs[b].power for b in ("mock:1", "mock:2"))
        assert all(d.state["bulbs"][b]["held"] is True for b in ("mock:1", "mock:2"))
        g = await cmd(d, "get")
        assert g["link"] == "held" and g["in_widget"] == ["mock:1", "mock:2"]  # still the widget's
        assert [(b["held"], b["link"], b["output"]) for b in g["bulbs_list"]] == [
            (True, "held", 50.0), (True, "held", 50.0)]
    run(tmp_path, lit_pair(), body)


def test_nothing_reconnects_a_held_bulb(tmp_path):
    async def body(d):
        m = mock(d)
        await cmd(d, "disconnect")
        await settle(d)
        n = len(m.calls)
        for refused in (("bulb", "30"), ("dim", "20"), ("on",), ("toggle",), ("nudge", "5"), ("pause",),
                        ("audio", "on"), ("video", "on")):
            with pytest.raises(UserError, match="hauslicht connect"):
                await cmd(d, *refused)
        with pytest.raises(UserError, match="hauslicht connect"):
            await cmd(d, "identify", "mock:1")
        await cmd(d, "set", "level=80")  # profile changes
        await cmd(d, "set", "mode=off")
        d.bulbs["mock:1"].entry["audio"] = True  # follow audio switched on before the hold
        assert not d.audio_following(d.bulbs["mock:1"])
        d.bulbs["mock:2"].entry["video"] = True
        assert not d.video_following(d.bulbs["mock:2"])
        d.clap_mode = lambda w: "both"
        d.on_clap({"label": "Clap"})  # clap
        await cmd(d, "mock-flick", "mock:1")  # flick handling: the wall switch as with any bulb
        await cmd(d, "mock-return", "mock:2", "3")
        d.desktop.screen, d.desktop.closed = 90.0, True  # the screen and the lid
        found = await cmd(d, "discover", "mock")  # scans
        assert len(found["discovered"]) == 2
        await settle(d, 6)
        assert since(m, n) == []  # no apply, no OnPowerUp, no idle(False)
        assert d.bulbs["mock:1"].power is True and d.bulbs["mock:2"].power is True
        assert (await cmd(d, "get"))["link"] == "held"
    run(tmp_path, lit_pair(), body)


def test_a_shared_link_stays_up_for_the_unheld_bulb(tmp_path):
    async def body(d):
        m = mock(d)
        await settle(d)
        n = len(m.calls)
        await cmd(d, "disconnect", which="mock:1")
        await settle(d)
        assert ("powerup", "mock:1", "on") in since(m, n)
        assert ("idle", None, True) not in m.calls  # mock:2 still needs the link
        g = await cmd(d, "get")
        assert g["link"] == "held"  # the selection (all) has a held bulb: a press connects it
        assert {b["id"]: b["link"] for b in g["bulbs_list"]} == {"mock:1": "held", "mock:2": "connected"}
        n = len(m.calls)
        d.desktop.screen = 80.0
        await cmd(d, "on")  # all: the unheld one only
        await settle(d)
        assert [c for c in since(m, n) if c[0] == "apply"] == [("apply", "mock:2", 80.0)]
        g = await cmd(d, "@1", "select", "mock:2")
        assert g["link"] == "connected"
    run(tmp_path, lit_pair(), body)


def run_on(path, body):
    """A daemon on an existing state file (as a restart reads it)."""
    async def main():
        d = Daemon(path, path.parent / "sock", desktop=FakeDesktop())
        await d.start()
        follow = asyncio.create_task(d.follow())
        try:
            await settle(d)
            return await body(d)
        finally:
            follow.cancel()
            await d.stop()
    return asyncio.run(main())


def test_the_hold_survives_a_restart_and_connect_ends_it(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    save_state(path, lit_pair())

    async def first(d):
        await cmd(d, "disconnect")
        await settle(d)
        assert d.armed_marker == {}
    run_on(path, first)
    assert all(e["held"] is True for e in json.loads(path.read_text())["bulbs"].values())

    # after the restart the link is down, as mesh's is until something connects
    linked = {"up": False}
    real_status = MockBackend.status
    monkeypatch.setattr(MockBackend, "status",
                        lambda self, bulb: {**real_status(self, bulb), "connected": linked["up"]})

    async def second(d):
        m = mock(d)
        assert all(b.held and b.armed == "on" for b in d.bulbs.values())  # off the marker: "on" confirmed
        assert m.calls == [("idle", None, True)]  # no link to check it again, no level
        assert (await cmd(d, "get"))["link"] == "held"
        await cmd(d, "connect", which="mock:1")
        assert not d.bulbs["mock:1"].held and "held" not in d.state["bulbs"]["mock:1"]
        assert d.bulbs["mock:2"].held
        linked["up"] = True
        await settle(d)
        after = m.calls[1:]
        assert after[0] == ("idle", None, False)  # told before the first send
        assert ("apply", "mock:1", 50.0) in after and ("powerup", "mock:1", "off") in after
        assert not [c for c in after if c[1] == "mock:2"]
        g = await cmd(d, "get")
        assert {b["id"]: b["link"] for b in g["bulbs_list"]} == {"mock:1": "connected", "mock:2": "held"}
    run_on(path, second)
    saved = json.loads(path.read_text())["bulbs"]
    assert "held" not in saved["mock:1"] and saved["mock:2"]["held"] is True


def test_connect_retries_a_failed_link_at_once(tmp_path):
    async def body(d):
        m = mock(d)
        real_apply, real_status = m.apply, m.status
        failing = {"on": True}

        async def apply(bulb, percent):
            if failing["on"]:
                raise RuntimeError("le-connection-abort-by-local")
            await real_apply(bulb, percent)

        def status(bulb):
            return {**real_status(bulb), "connected": not failing["on"]}

        m.apply, m.status = apply, status
        d.desktop.screen = 70.0
        await settle(d)
        g = await cmd(d, "get")
        assert g["link"] == "failed" and g["link_reason"] == "le-connection-abort-by-local"
        assert g["bulbs_list"][0]["link_reason"] == "le-connection-abort-by-local"
        failing["on"] = False
        await settle(d, 4)
        assert m.applied["mock:1"] == 50.0  # the worker waits out its back-off
        start = time.monotonic()
        await cmd(d, "connect")
        for _ in range(40):
            await asyncio.sleep(0.05)
            if m.applied.get("mock:1") == 70.0:
                break
        assert m.applied.get("mock:1") == 70.0
        assert time.monotonic() - start < BACKOFF_S / 2
        await settle(d)
        g = await cmd(d, "get")
        assert g["link"] == "connected" and g["link_reason"] == "" and d.link_errors == {}
    run(tmp_path, mock_state(mock_1={"power": True}), body)


def test_connect_and_disconnect_are_cli_subcommands(tmp_path, monkeypatch, capsys):
    parser = cli.build_parser()
    for argv, bulb, widget in ((["connect"], None, None), (["disconnect", "mock:2"], "mock:2", None),
                               (["-w", "2", "connect", "all"], "all", 2)):
        args = parser.parse_args(argv)
        assert (args.cmd, args.bulb, args.widget) == (argv[-1 if bulb is None else -2], bulb, widget)
        assert cli.requests_for(args) == [(args.cmd, [])]
    assert cli.request("disconnect", [], "mock:2", 2) == {"cmd": "@2", "args": ["disconnect"], "which": "mock:2"}
    assert {"connect", "disconnect"} <= set(cli.DAEMON_ONLY)

    async def body(d, hl):
        code, out, _err = await hl("disconnect", "mock:1")
        assert code == 0 and "held" in out
        await settle(d)
        code, out, _err = await hl("bulbs")
        assert code == 0 and _("disconnected (held)") in out
        code, _out, err = await hl("on", "mock:1")
        assert code == 1 and "hauslicht connect" in err
        code, out, _err = await hl("connect", "mock:1")
        assert code == 0 and "connected" in out
        assert not d.bulbs["mock:1"].held
    with_cli(tmp_path, monkeypatch, capsys, lit_pair(), body)
