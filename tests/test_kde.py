"""KDE integration with a fake session bus: shortcut registration and
mapping, OSD calls, the panel script and its idempotence."""

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from haus import kde
from haus.kde import ACTIONS, IS_DEFAULT, SET_PRESENT, Kde, panel_disabled, panel_script
from test_core import cmd, mock_state, run, settle


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(kde, "_", lambda s: s)  # the translation is chosen at import


class FakeBus:
    def __init__(self, script_reply="[]"):
        self.calls = []
        self.on_signal = None
        self.script_reply = script_reply
        self.matches = []

    async def call(self, dest, path, iface, member, signature="", body=None):
        self.calls.append((member, body))
        if member == "getComponent":
            return ["/component/hauslicht"]
        if member == "evaluateScript":
            return [self.script_reply]
        return []

    async def add_match(self, rule):
        self.matches.append(rule)

    def of(self, member):
        return [b for m, b in self.calls if m == member]


def press(k, action):
    k._signal(SimpleNamespace(member="globalShortcutPressed", path="/component/hauslicht",
                              body=["hauslicht", action, 0]))


def with_kde(tmp_path, state, body, panel=False):
    async def wrapped(d):
        bus = FakeBus()
        k = Kde(d, panel=panel, bus=bus)
        d.kde = k
        await k.start()
        return await body(d, k, bus)
    return run(tmp_path, state, wrapped)


def test_register_without_keys(tmp_path):
    async def body(d, k, bus):
        regs = bus.of("doRegister")
        assert len(regs) == len(ACTIONS) == 15
        assert regs[0] == [["hauslicht", "select-all", "Haus.Licht", "Select all bulbs"]]
        sets = bus.of("setShortcut")
        assert all(b[1] == [] for b in sets)  # never a key
        assert {b[2] for b in sets} == {IS_DEFAULT, SET_PRESENT}  # never NoAutoloading
        assert "path='/component/hauslicht'" in bus.matches[0]
        assert not bus.of("evaluateScript")  # panel off
    with_kde(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_select_actions_and_osd(tmp_path):
    async def body(d, k, bus):
        press(k, "select-2")
        await settle(d)
        assert d.selection(1) == "mock:2"
        assert bus.of("showText")[-1] == ["brightness-high", "mock_2"]
        press(k, "select-9")  # no 9th bulb: ignored, no OSD
        await settle(d)
        assert d.selection(1) == "mock:2" and len(bus.of("showText")) == 1
        press(k, "select-all")
        await settle(d)
        assert d.selection(1) == "all"
        assert bus.of("showText")[-1] == ["brightness-high", "All bulbs"]
    with_kde(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_select_follows_widget_list_order(tmp_path):
    async def body(d, k, bus):
        await cmd(d, "order", "mock:2", "mock:1")
        press(k, "select-1")
        await settle(d)
        assert d.selection(1) == "mock:2"
    with_kde(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_toggle(tmp_path):
    async def body(d, k, bus):
        press(k, "toggle")
        await settle(d)
        assert all(b.power for b in d.bulbs.values())
        assert bus.of("showText")[-1] == ["brightness-high", "All bulbs · on"]
        press(k, "toggle")
        await settle(d)
        assert not any(b.power for b in d.bulbs.values())
        assert bus.of("showText")[-1][1] == "All bulbs · off"
    with_kde(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_remote_moves_slider_like_the_wheel(tmp_path):
    async def body(d, k, bus):
        await cmd(d, "set", "mode=maximum")
        before = d.settings["slider"]
        press(k, "remote-down")
        await settle(d)
        assert d.settings["slider"] == max(0, before - 5)
        press(k, "remote-up")
        await settle(d)
        assert d.settings["slider"] == max(0, before - 5) + 5
        p, name, icon = bus.of("mediaPlayerVolumeChanged")[-1]
        assert p == d.settings["slider"] and name == "All bulbs"
    with_kde(tmp_path, mock_state(mock_1={}), body)


def test_direct_sets_manual_level(tmp_path):
    async def body(d, k, bus):
        await cmd(d, "on")
        await cmd(d, "dim", "40")
        press(k, "direct-up")
        await settle(d)
        b = d.bulbs["mock:1"]
        assert b.paused and b.manual == 45.0
        assert bus.of("mediaPlayerVolumeChanged")[-1][0] == 45
        await cmd(d, "dim", "3")
        press(k, "direct-down")
        await settle(d)
        assert b.manual == 0.0
        assert bus.of("showText")[-1] == ["brightness-high", "All bulbs · 0 %"]
    with_kde(tmp_path, mock_state(mock_1={}), body)


def test_direct_moves_level_when_not_tracking(tmp_path):
    async def body(d, k, bus):
        await cmd(d, "set", "track=never")
        level = d.profiles["default"]["level"]
        press(k, "direct-up")
        await settle(d)
        assert d.profiles["default"]["level"] == min(100.0, level + 5)
    with_kde(tmp_path, mock_state(mock_1={}), body)


def test_osd_failure_is_silent(tmp_path):
    async def body(d, k, bus):
        async def broken(*a, **kw):
            raise RuntimeError("no plasmashell")
        bus.call = broken
        press(k, "toggle")
        await settle(d)
        assert d.bulbs["mock:1"].power
    with_kde(tmp_path, mock_state(mock_1={}), body)


def test_panel_synced_at_start_and_on_widget_count(tmp_path):
    async def body(d, k, bus):
        await asyncio.sleep(0.05)
        scripts = bus.of("evaluateScript")
        assert len(scripts) == 1 and "var N = 1," in scripts[0][0]
        await cmd(d, "set", "widgets=3")
        await asyncio.sleep(0.05)
        assert "var N = 3," in bus.of("evaluateScript")[-1][0]
    with_kde(tmp_path, mock_state(mock_1={}), body, panel=True)


def test_panel_disabled_env(monkeypatch):
    monkeypatch.delenv("HAUSLICHT_NO_PANEL", raising=False)
    assert panel_disabled(test_instance=True) and not panel_disabled(test_instance=False)
    monkeypatch.setenv("HAUSLICHT_NO_PANEL", "1")
    assert panel_disabled(test_instance=False)
    monkeypatch.setenv("HAUSLICHT_NO_PANEL", "0")
    assert not panel_disabled(test_instance=True)


# ---- the generated script, run against a tiny fake of Plasma's scripting API (node)

FAKE_SHELL = r"""
var nextId = 100, state = JSON.parse(process.argv[1]), out = null;
function Widget(type, cfg) { this.type = type; this.id = nextId++; this.cfg = cfg || {};
  this.geometry = {x: 500, y: 4}; }
Widget.prototype.readConfig = function (k, d) { return k in this.cfg ? this.cfg[k] : d; };
Widget.prototype.writeConfig = function (k, v) { this.cfg[k] = v; };
Widget.prototype.reloadConfig = function () {};
Widget.prototype.remove = function () { var l = this.panel.list; l.splice(l.indexOf(this), 1); };
function Panel(p) { var self = this; this.id = p.id; this.screen = p.screen;
  this.list = p.widgets.map(function (w) { var x = new Widget(w.type, w.cfg); x.panel = self; return x; }); }
Panel.prototype.widgets = function (t) { return this.list.filter(function (w) { return !t || w.type === t; }); };
Panel.prototype.addWidget = function (t, x, y) {
  var w = new Widget(t, {}); w.panel = this;
  var at = this.list.findIndex(function (o) { return o.type === "org.kde.plasma.systemtray"; });
  this.list.splice(at < 0 ? this.list.length : at, 0, w); return w; };
var ps = state.map(function (p) { return new Panel(p); });
function panels() { return ps; }
function print(s) { out = s; }
eval(process.argv[2]);
console.log(JSON.stringify({done: JSON.parse(out), panels: ps.map(function (p) {
  return p.list.map(function (w) { return w.type === "haus.licht.tray" ? "H" + w.readConfig("widgetIndex", 1) :
    w.type.split(".").pop(); }); })}));
"""


def shell(panels, n):
    try:
        res = subprocess.run(["node", "-e", FAKE_SHELL, json.dumps(panels), panel_script(n)],
                             capture_output=True, text=True, check=True)
    except FileNotFoundError:
        pytest.skip("node not installed")
    return json.loads(res.stdout)


def as_state(layout, screen=0, pid=1):
    return [{"id": pid, "screen": screen, "widgets": [
        {"type": "haus.licht.tray", "cfg": {"widgetIndex": int(w[1:])}} if w.startswith("H")
        else {"type": "org.kde.plasma." + w, "cfg": {}} for w in layout]}]


def test_panel_script_adds_next_to_tray_and_is_idempotent():
    start = ["kickoff", "taskmanager", "systemtray", "digitalclock"]
    r = shell(as_state(start), 3)
    assert r["panels"][0] == ["kickoff", "taskmanager", "H2", "H3", "systemtray", "digitalclock"]
    again = shell(as_state(r["panels"][0]), 3)
    assert again["done"] == [] and again["panels"] == r["panels"]


def test_panel_script_removes_above_n_and_duplicates_but_never_index_1():
    r = shell(as_state(["H1", "H2", "H2", "H5", "systemtray"]), 2)
    assert r["panels"][0] == ["H1", "H2", "systemtray"]
    r = shell(as_state(["H1", "H2", "systemtray"]), 1)
    assert r["panels"][0] == ["H1", "systemtray"]


def test_panel_script_skips_panels_without_tray_and_only_tidies_offscreen():
    r = shell(as_state(["taskmanager", "H3"]), 1)
    assert r["panels"][0] == ["taskmanager", "H3"]  # no tray: not ours
    r = shell(as_state(["H4", "systemtray"], screen=-1), 2)
    assert r["panels"][0] == ["systemtray"]  # removed, nothing added off screen


def test_sync_panel_reports_script_output(tmp_path):
    async def body(d, k, bus):
        bus.script_reply = '[{"panel": 2, "added": 2}]'
        assert await k.sync_panel(2) == [{"panel": 2, "added": 2}]
        bus.script_reply = "TypeError: boom"
        assert await k.sync_panel(2) is None
    with_kde(tmp_path, mock_state(mock_1={}), body)


def test_friendly_names_translate(monkeypatch):
    monkeypatch.setattr(kde, "_", lambda s: {"Select bulb {n}": "Lampe {n} auswählen"}.get(s, s))
    assert kde.friendly("Select bulb 3") == "Lampe 3 auswählen"
