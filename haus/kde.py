"""KDE Plasma integration: global shortcuts (kglobalaccel), OSD feedback
(plasmashell's osdService) and the panel widgets 2..N (desktop scripting).

Everything here is optional: outside Plasma, or when a call fails, it logs
and carries on; nothing in here may stop the daemon. The shortcuts act on
widget 1 (the instance inside the system tray). Protocol notes are in
docs/ctl-protocol.md (Hotkeys, Panel widgets).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from .common import _

log = logging.getLogger("hauslicht")

COMPONENT, COMPONENT_NAME = "hauslicht", "Haus.Licht"
KGA_DEST, KGA_PATH, KGA_IFACE = "org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel"
KGA_COMPONENT_IFACE = "org.kde.kglobalaccel.Component"
OSD_DEST, OSD_PATH, OSD_IFACE = "org.kde.plasmashell", "/org/kde/osdService", "org.kde.osdService"
SHELL_DEST, SHELL_PATH, SHELL_IFACE = "org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell"
PLUGIN = "haus.licht.tray"
TRAY = "org.kde.plasma.systemtray"
ICON = "brightness-high"
STEP = 5.0

# kglobalaccel's setShortcut flags (KGlobalAccel::SetShortcutFlag)
IS_DEFAULT, SET_PRESENT, NO_AUTOLOADING = 1, 2, 4

# action id -> friendly name (English; translated through `_`). No default keys.
ACTIONS: list[tuple[str, str]] = (
    [("select-all", "Select all bulbs")]
    + [(f"select-{k}", f"Select bulb {k}") for k in range(1, 10)]
    + [("toggle", "Switch on/off"),
       ("remote-up", "Remote slider up"), ("remote-down", "Remote slider down"),
       ("direct-up", "Brightness up (direct)"), ("direct-down", "Brightness down (direct)")]
)


def friendly(name: str) -> str:
    if name.startswith("Select bulb "):
        return _("Select bulb {n}").format(n=name.rsplit(" ", 1)[1])
    return _(name)


def panel_disabled(test_instance: bool) -> bool:
    """HAUSLICHT_NO_PANEL=1 switches the panel widgets off, =0 on; unset: off
    for a test instance (--no-desktop-writes), on otherwise."""
    value = os.environ.get("HAUSLICHT_NO_PANEL")
    if value is None:
        return test_instance
    return value.strip() not in ("0", "", "no", "off", "false")


def panel_script(n: int) -> str:
    """Desktop script: every panel holding a system tray gets haus.licht.tray
    applets with widgetIndex 2..n exactly once, inserted left of the tray;
    instances above n (and duplicates) go. Applets with widgetIndex 1 (and
    the tray's own instance, which lives in the tray's containment) are
    never touched. Prints a JSON list of what it did."""
    return """
var N = %d, PLUGIN = %s, TRAY = %s, done = [];
function idx(w) {
    w.currentConfigGroup = ["General"];
    var i = parseInt(w.readConfig("widgetIndex", 1), 10);
    return isNaN(i) ? 1 : i;
}
panels().forEach(function (p) {
    var trays = p.widgets(TRAY);
    if (!trays.length) return;
    var have = {};
    p.widgets(PLUGIN).forEach(function (w) {
        var i = idx(w);
        if (i < 2) return;
        if (i > N || have[i]) { done.push({panel: p.id, removed: i}); w.remove(); }
        else have[i] = true;
    });
    if (p.screen < 0) return;  // a panel of a screen not connected: only tidy it
    var g = trays[0].geometry;
    for (var i = 2; i <= N; i++) {
        if (have[i]) continue;
        // a point inside the tray: the panel inserts the new applet just before it
        var w = p.addWidget(PLUGIN, g.x + 1, g.y + 1);
        if (!w) { done.push({panel: p.id, failed: i}); continue; }
        w.currentConfigGroup = ["General"];
        w.writeConfig("widgetIndex", i);
        w.reloadConfig();
        done.push({panel: p.id, added: i});
    }
});
print(JSON.stringify(done));
""" % (int(n), json.dumps(PLUGIN), json.dumps(TRAY))


class DBus:
    """A thin session-bus wrapper (dbus_fast): call() returns the reply body
    or raises; on_signal gets every signal that matches an added rule."""

    def __init__(self) -> None:
        self.bus = None
        self.on_signal = None

    async def connect(self) -> None:
        from dbus_fast.aio import MessageBus
        self.bus = await MessageBus().connect()
        self.bus.add_message_handler(self._message)

    def _message(self, msg):
        if self.on_signal is not None and msg.message_type.name == "SIGNAL":
            with contextlib.suppress(Exception):
                self.on_signal(msg)
        return None

    async def call(self, dest: str, path: str, iface: str, member: str, signature: str = "",
                   body: list | None = None) -> list:
        from dbus_fast import Message, MessageType
        reply = await self.bus.call(Message(destination=dest, path=path, interface=iface, member=member,
                                            signature=signature, body=body or []))
        if reply is None:
            raise RuntimeError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name} {reply.body[:1]}")
        return reply.body

    async def add_match(self, rule: str) -> None:
        await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                        "AddMatch", "s", [rule])

    def close(self) -> None:
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.disconnect()
            self.bus = None


class Kde:
    """Shortcuts, OSD and panel widgets for one daemon. `daemon` is the core
    (haus.daemon.Daemon); `bus` a DBus or a fake with the same methods."""

    def __init__(self, daemon, panel: bool = True, bus=None) -> None:
        self.daemon = daemon
        self.panel = panel
        self.bus = bus
        self.component_path: str | None = None
        self.panel_task: asyncio.Task | None = None
        self.panel_again = False
        self.tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ start / stop

    async def start(self) -> None:
        if self.bus is None:
            if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.environ.get("XDG_RUNTIME_DIR"):
                log.debug("kde: no session bus")
                return
            bus = DBus()
            try:
                await bus.connect()
            except Exception as exc:
                log.debug("kde: no session bus (%s)", exc)
                return
            self.bus = bus
        self.bus.on_signal = self._signal
        try:
            await self.register()
        except Exception as exc:
            log.info("no global shortcuts (%s)", exc)
        self.widgets_changed()

    def close(self) -> None:
        for task in list(self.tasks) + ([self.panel_task] if self.panel_task else []):
            task.cancel()
        if isinstance(self.bus, DBus):
            self.bus.close()

    def spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ------------------------------------------------------------ shortcuts

    async def register(self) -> None:
        """Register every action without keys. The default is set to none
        (IsDefault), then the action is marked present with autoloading:
        kglobalaccel keeps keys the user assigned and only a new action gets
        the (empty) keys passed here."""
        for action, name in ACTIONS:
            ident = [COMPONENT, action, COMPONENT_NAME, friendly(name)]
            await self.bus.call(KGA_DEST, KGA_PATH, KGA_IFACE, "doRegister", "as", [ident])
            await self.bus.call(KGA_DEST, KGA_PATH, KGA_IFACE, "setShortcut", "asaiu", [ident, [], IS_DEFAULT])
            await self.bus.call(KGA_DEST, KGA_PATH, KGA_IFACE, "setShortcut", "asaiu", [ident, [], SET_PRESENT])
        (self.component_path,) = await self.bus.call(KGA_DEST, KGA_PATH, KGA_IFACE, "getComponent", "s",
                                                     [COMPONENT])
        await self.bus.add_match(f"type='signal',interface='{KGA_COMPONENT_IFACE}',"
                                 f"member='globalShortcutPressed',path='{self.component_path}'")
        log.info("global shortcuts registered (%d actions, keys in System Settings → Shortcuts)", len(ACTIONS))

    def _signal(self, msg) -> None:
        if (msg.member == "globalShortcutPressed" and msg.path == self.component_path
                and msg.body and msg.body[0] == COMPONENT):
            self.spawn(self.pressed(msg.body[1]))

    async def pressed(self, action: str) -> None:
        try:
            osd = self.perform(action)
        except Exception as exc:
            log.warning("shortcut %s: %s", action, exc)
            return
        if osd is not None:
            await self.show(*osd)

    def label(self) -> str:
        """Widget 1's selection, as the OSD names it."""
        sel = self.daemon.selection(1)
        return _("All bulbs") if sel == "all" else self.daemon.bulbs[sel].name

    def perform(self, action: str) -> tuple[str, str, float | None] | None:
        """Do one action on widget 1; returns the OSD (icon, text, percent or
        None), None when nothing happened."""
        d = self.daemon
        if action == "select-all" or action.startswith("select-"):
            if action == "select-all":
                target = "all"
            else:
                ids = [b.id for b in d.in_widget(1)]
                k = int(action.split("-", 1)[1])
                if not 1 <= k <= len(ids):
                    return None
                target = ids[k - 1]
            d.select(target, 1)
            return ICON, self.label(), None
        if action == "toggle":
            bulbs = d.driven(d.selected(1))
            on = not any(b.power for b in bulbs)
            d.set_power(bulbs, on, "toggle")
            return (ICON if on else "brightness-low",
                    f"{self.label()} · {_('on') if on else _('off')}", None)
        if action in ("remote-up", "remote-down"):
            step = STEP if action.endswith("up") else -STEP
            if d.settings["mode"] == "off":
                return ICON, self.label(), d.nudge_level(step, 1)
            d.nudge(step, 1)
            return ICON, self.label(), float(d.settings["slider"])
        if action in ("direct-up", "direct-down"):
            step = STEP if action.endswith("up") else -STEP
            return ICON, self.label(), d.nudge_level(step, 1)
        log.debug("unknown shortcut %s", action)
        return None

    # ------------------------------------------------------------ OSD

    async def show(self, icon: str, text: str, percent: float | None = None) -> None:
        """Plasma's OSD. osdService has no generic progress call; the media
        player volume OSD is one (icon, bar, name), except at 0, where it
        says "muted": then the level goes into the text."""
        try:
            if percent is None:
                await self.bus.call(OSD_DEST, OSD_PATH, OSD_IFACE, "showText", "ss", [icon, text])
            else:
                p = max(0, min(100, round(percent)))
                if p == 0:
                    await self.bus.call(OSD_DEST, OSD_PATH, OSD_IFACE, "showText", "ss", [icon, f"{text} · 0 %"])
                else:
                    await self.bus.call(OSD_DEST, OSD_PATH, OSD_IFACE, "mediaPlayerVolumeChanged", "iss",
                                        [p, text, icon])
        except Exception as exc:
            log.debug("no OSD (%s)", exc)

    # ------------------------------------------------------------ panel widgets

    def widgets_changed(self) -> None:
        """Bring the panel in line with the widget count (latest wins, one at a time)."""
        if not self.panel or self.bus is None:
            return
        if self.panel_task is not None and not self.panel_task.done():
            self.panel_again = True
            return
        self.panel_task = asyncio.get_running_loop().create_task(self._sync_panel())

    async def _sync_panel(self) -> None:
        while True:
            self.panel_again = False
            await self.sync_panel(self.daemon.widget_count)
            if not self.panel_again:
                return

    async def sync_panel(self, n: int) -> list | None:
        try:
            (out,) = await self.bus.call(SHELL_DEST, SHELL_PATH, SHELL_IFACE, "evaluateScript", "s",
                                         [panel_script(n)])
        except Exception as exc:
            log.info("panel widgets not synced (%s)", exc)
            return None
        try:
            done = json.loads(out.strip() or "[]")
        except ValueError:
            log.info("panel script: %s", out.strip())
            return None
        if done:
            log.info("panel widgets: %s", done)
        return done
