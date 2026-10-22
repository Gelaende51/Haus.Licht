"""The hauslicht command line against an in-process daemon with mock bulbs:
every widget feature has a subcommand, replies are readable text, and the
man pages list every subcommand."""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest

from haus import cli
from test_core import mock_state, run

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _logger_levels():
    """cli.main sets the hauslicht, btmesh and bleak logger levels for a
    terminal run; put them back so later tests' caplog still sees INFO."""
    names = ("hauslicht", "btmesh", "bleak", "bleak_retry_connector")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def with_cli(tmp_path, monkeypatch, capsys, state, body):
    """Run body(d, hl) with the daemon serving a socket; hl(*argv) runs the
    CLI against it in a thread and returns (exit code, stdout, stderr)."""
    sock = tmp_path / "cli.sock"
    monkeypatch.setattr(cli, "SOCK_PATH", sock)

    async def wrapped(d):
        server = await asyncio.start_unix_server(d.handle_client, path=str(sock))

        async def hl(*argv):
            capsys.readouterr()
            try:
                code = await asyncio.to_thread(cli.main, [str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
            out = capsys.readouterr()
            return code, out.out, out.err

        async with server:
            return await body(d, hl)

    return run(tmp_path, state, wrapped)


def two_bulbs():
    return mock_state(mock_1={"power": True}, mock_2={"power": True})


def test_info_and_bulbs_are_text(tmp_path, monkeypatch, capsys):
    async def body(d, hl):
        code, out, _err = await hl("info")
        assert code == 0
        assert out.startswith(cli._("widget {w} of {n}, selection {sel} (profile {profile})").format(
            w=1, n=1, sel="all", profile="default"))
        assert "range_hi: 100" in out and "mock:1" in out and "{" not in out.split("bulbs:")[0]
        code, out, _err = await hl("bulbs")
        assert code == 0 and [line.split()[0] for line in out.splitlines()] == ["mock:1", "mock:2"]
        code, out, _err = await hl("--json", "info")
        assert json.loads(out)["selected"] == "all"
    with_cli(tmp_path, monkeypatch, capsys, two_bulbs(), body)


def test_profile_values_from_the_command_line(tmp_path, monkeypatch, capsys):
    async def body(d, hl):
        for argv in (("hue", 200), ("sat", 40), ("shift", 80), ("follow-ct", "on"), ("curve", -20)):
            code, out, err = await hl(*argv)
            assert code == 0, err
        p = d.profiles["default"]
        assert (p["hue"], p["sat"], p["shift"], p["follow_ct"], p["curve"]) == (200, 40, 80, True, -20)
        code, out, _err = await hl("hue")
        assert out.splitlines() == ["profile: default", "hue: 200"]
        # a window above the present one: range_hi goes first, so they never cross
        code, out, err = await hl("range", 120, 180)
        assert code == 0, err
        assert (p["range_lo"], p["range_hi"]) == (120, 180)
        code, out, err = await hl("range", -50, 60)
        assert code == 0, err
        assert (p["range_lo"], p["range_hi"]) == (-50, 60)
        code, _out, err = await hl("range", 10)
        assert code == 2 and "range" in err
        code, out, err = await hl("set", "level=30", "track=never")
        assert code == 0 and "level: 30" in out and "track: never" in out
    with_cli(tmp_path, monkeypatch, capsys, two_bulbs(), body)


def test_level_is_the_widget_slider(tmp_path, monkeypatch, capsys):
    async def body(d, hl):
        await hl("track", "never")  # the screen is ignored: level sets the profile's level
        code, out, err = await hl("level", 70)
        assert code == 0, err
        assert d.profiles["default"]["level"] == 70
    with_cli(tmp_path, monkeypatch, capsys, two_bulbs(), body)


def test_widgets_lists_and_order(tmp_path, monkeypatch, capsys):
    async def body(d, hl):
        assert (await hl("widgets", 2))[1].strip() == "widgets: 2"
        code, out, err = await hl("-w", 2, "widget-bulbs", "mock:2")
        assert code == 0, err
        assert "in_widget: mock:2" in out
        code, out, _err = await hl("-w", 2, "info")
        assert out.startswith(cli._("widget {w} of {n}, selection {sel} (profile {profile})").format(
            w=2, n=2, sel="all", profile="default"))
        code, out, _err = await hl("-w", 2, "widget-bulbs", "none")
        assert "in_widget: -" in out
        code, out, _err = await hl("order", "mock:2", "mock:1")
        assert [line.split()[0] for line in out.splitlines()] == ["mock:2", "mock:1"]
        code, _out, err = await hl("-w", 5, "info")
        assert code == 1 and "5" in err
    with_cli(tmp_path, monkeypatch, capsys, two_bulbs(), body)


def test_profiles_and_bulb_settings(tmp_path, monkeypatch, capsys):
    async def body(d, hl):
        code, out, _err = await hl("profile", "save", "evening")
        assert out.splitlines() == ["* default", "  evening"]
        assert (await hl("profile", "list"))[1] == out
        code, _out, err = await hl("profile", "apply")
        assert code == 2 and "NAME" in err
        code, out, err = await hl("bulb-set", "mock:2", "use=off", "icon_on=lightbulb", "name=Shelf")
        assert code == 0, err
        line = [x for x in out.splitlines() if x.startswith("mock:2")][0]
        assert "'Shelf'" in line and cli._("not in use") in line and "icons on=lightbulb" in line
        code, out, _err = await hl("video-region", "mock:1", "left")
        assert out.strip() == "mock:1: video_region left"
    with_cli(tmp_path, monkeypatch, capsys, two_bulbs(), body)


def test_discover_backends_and_rules_are_text(tmp_path, monkeypatch, capsys):
    async def body(d, hl):
        code, out, _err = await hl("backends")
        assert code == 0 and out.startswith("mock  ") and "    address=  " in out
        code, out, _err = await hl("discover", "mock")
        assert code == 0 and "add: hauslicht add mock" in out and "{" not in out
        rules = [{"name": "Mail", "app": "thunderbird", "pattern": "blink", "count": 2, "intensity": 100,
                  "color": "#3080ff", "bulbs": "all", "dark": False}]
        code, out, err = await hl("notify-rules-set", json.dumps(rules))
        assert code == 0, err
        assert (await hl("notify-rules"))[1] == out
        assert "  Mail: app~'thunderbird' -> blink x2 100 % #3080ff bulbs=all dark=off" in out.splitlines()
        code, out, _err = await hl("autoconnect")
        assert out.splitlines() == ["autoconnect: off", "autoconnect_ignore: -"]
        code, out, _err = await hl("setup-status")
        assert out.startswith("setup:\n  status: ")
    with_cli(tmp_path, monkeypatch, capsys, two_bulbs(), body)


def test_no_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SOCK_PATH", tmp_path / "none.sock")
    for argv in (["info"], ["hue", "20"], ["watch"]):
        assert cli.main(argv) == 1
        assert capsys.readouterr().err.strip() == cli._("the sync daemon is not running")


def test_event_lines():
    assert cli.event_line({"event": "flick", "t": 0, "bulb": "mock:1", "power": False}).endswith(
        " flick mock:1: power=off")
    snap = cli.event_line({"event": "snapshot", "t": 0, "widget": 1, "bulbs_list": [{}],
                           "intended": {"mock:1": {"power": True, "percent": 50.0, "color": None}}})
    assert snap.splitlines()[1].endswith("intended mock:1: power=on, percent=50, color=-")


def subcommands() -> list[str]:
    return [name for _title, cmds in cli.build_parser().groups for name, _help, _p in cmds]


def test_help_groups_every_subcommand_once():
    parser = cli.build_parser()
    names = subcommands()
    assert len(names) == len(set(names))
    assert set(names) == set(parser._subparsers._group_actions[0].choices)


@pytest.mark.parametrize("page", ["man/hauslicht.1", "man/de/hauslicht.1"])
def test_man_page_lists_every_subcommand(page):
    text = (ROOT / page).read_text()
    # dev/gen-man.py writes each command as ".TP" then "\fBname\fR args" (hyphens as \-)
    documented = {m.replace("\\-", "-") for m in re.findall(r'^\.TP\n\\fB([a-z\\-]+)\\fR', text, re.M)}
    missing = [n for n in subcommands() if n not in documented]
    assert not missing, f"{page} lacks {missing}: run dev/gen-man.py"
