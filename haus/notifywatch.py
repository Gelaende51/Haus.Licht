"""Desktop notifications as they are sent: org.freedesktop.Notifications.Notify
calls on the session bus, seen through a monitor connection of our own.

The watcher opens a second session-bus connection and turns it into a monitor
(org.freedesktop.DBus.Monitoring.BecomeMonitor, match on the Notify method
call only). A monitor may never send again, so the handler marks every
message handled (dbus-fast would otherwise answer method calls with an
error). Where the bus refuses that, it tries the old eavesdrop match
(dbus-daemon only; dbus-broker refuses it too) and else reports itself
unavailable: it never raises.

Do Not Disturb: org.freedesktop.Notifications' `Inhibited` property (Plasma
sets it), read on an ordinary connection when a notification arrives; while
it is true nothing is passed on. Titles and bodies go to the callback only:
nothing here logs or keeps them (the app name at most).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

log = logging.getLogger("hauslicht")

NOTIFY_IFACE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
MATCH = f"type='method_call',interface='{NOTIFY_IFACE}',member='Notify'"
INHIBITED_TIMEOUT_S = 1.0


def is_call(msg) -> bool:
    try:
        from dbus_fast import MessageType
        return msg.message_type == MessageType.METHOD_CALL
    except ImportError:
        return False


def notify_args(msg) -> tuple[str, str, str] | None:
    """(app_name, summary, body) of a Notify method call, else None.
    Signature susssasa{sv}i: app_name, replaces_id, app_icon, summary, body, ..."""
    if (not is_call(msg) or msg.interface != NOTIFY_IFACE or msg.member != "Notify"
            or not isinstance(msg.body, (list, tuple)) or len(msg.body) < 5):
        return None
    app, _rid, _icon, title, body = msg.body[:5]
    return str(app or ""), str(title or ""), str(body or "")


class NotifyWatcher:
    """on_notify(app, title, body) for each notification while not inhibited.

    state: off | starting | monitor | eavesdrop | unavailable; error: why unavailable."""

    def __init__(self, on_notify) -> None:
        self.on_notify = on_notify
        self.state = "off"
        self.error = ""
        self.inhibited: bool | None = None  # the last reading
        self.monitor = None  # the monitor connection
        self.bus = None      # an ordinary connection: Inhibited
        self.task: asyncio.Task | None = None
        self.pending: set[asyncio.Task] = set()

    def describe(self) -> dict:
        return {"state": self.state, "available": self.state in ("monitor", "eavesdrop", "off", "starting"),
                "error": self.error, "inhibited": self.inhibited}

    # ------------------------------------------------------------ control

    def apply(self, on: bool) -> None:
        """Watch while on (the global switch), not at all otherwise."""
        if on and self.state in ("off", "unavailable") and self.task is None:
            self.state, self.error = "starting", ""
            self.task = asyncio.get_running_loop().create_task(self._start())
        elif not on and self.state != "off":
            self.stop()

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None
        for task in list(self.pending):
            task.cancel()
        for bus in (self.monitor, self.bus):
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.disconnect()
        self.monitor = self.bus = None
        self.state = "off"

    async def _start(self) -> None:
        try:
            from dbus_fast import Message, MessageType
            from dbus_fast.aio import MessageBus
        except ImportError as exc:
            self._unavailable(f"dbus-fast missing ({exc})")
            return
        try:
            self.bus = await MessageBus().connect()
            self.monitor = await MessageBus().connect()
            self.monitor.add_message_handler(self.handle)
            reply = await self.monitor.call(Message(
                destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus.Monitoring", member="BecomeMonitor",
                signature="asu", body=[[MATCH], 0]))
            if reply is not None and reply.message_type != MessageType.ERROR:
                self.state = "monitor"
            else:
                why = reply.error_name if reply is not None else "no reply"
                reply = await self.monitor.call(Message(
                    destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                    body=["eavesdrop='true'," + MATCH]))
                if reply is None or reply.message_type == MessageType.ERROR:
                    self._unavailable(f"BecomeMonitor refused ({why}), eavesdrop too")
                    return
                self.state = "eavesdrop"
            log.info("notifications: watching (%s)", self.state)
            await self.read_inhibited()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # no session bus, refused: never fatal
            self._unavailable(str(exc) or type(exc).__name__)
        finally:
            self.task = None

    def _unavailable(self, why: str) -> None:
        log.warning("notifications: cannot watch (%s)", why)
        for bus in (self.monitor, self.bus):
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.disconnect()
        self.monitor = self.bus = None
        self.state, self.error = "unavailable", why

    # ------------------------------------------------------------ messages

    def handle(self, msg) -> bool | None:
        """Every message on the monitor connection. A method call (ours to see,
        not to answer) is marked handled, so that dbus-fast never replies; a
        reply or signal is not (True would swallow our own calls' replies)."""
        try:
            got = notify_args(msg)
            if got is not None:
                task = asyncio.get_running_loop().create_task(self.deliver(*got))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
        except Exception as exc:  # never an error reply from a monitor
            log.warning("notifications: %s", type(exc).__name__)
        return True if is_call(msg) else None

    async def deliver(self, app: str, title: str, body: str) -> None:
        if await self.read_inhibited():
            log.debug("notification from %s: Do Not Disturb", app)
            return
        try:
            self.on_notify(app, title, body)
        except Exception as exc:
            log.warning("notification from %s: %s", app, type(exc).__name__)

    async def read_inhibited(self) -> bool:
        """Do Not Disturb now (False when it cannot be read)."""
        if self.bus is None:
            return bool(self.inhibited)
        try:
            from dbus_fast import Message, MessageType
            reply = await asyncio.wait_for(self.bus.call(Message(
                destination=NOTIFY_IFACE, path=NOTIFY_PATH, interface="org.freedesktop.DBus.Properties",
                member="Get", signature="ss", body=[NOTIFY_IFACE, "Inhibited"])), INHIBITED_TIMEOUT_S)
            if reply is None or reply.message_type == MessageType.ERROR:
                self.inhibited = None
                return False
            value = reply.body[0]
            value = getattr(value, "value", value)
            self.inhibited = bool(value)
        except Exception:
            self.inhibited = None
            return False
        return self.inhibited
