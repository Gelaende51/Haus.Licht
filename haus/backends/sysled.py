"""LEDs of the Linux LED class (/sys/class/leds/<name>): keyboard, chassis,
Wi-Fi, ThinkPad lid logo, LED strips with a kernel driver.

`brightness` 0..`max_brightness`; an LED with max_brightness 1 is on/off
only (step 100 %). Writing `brightness` needs root on most systems, so a
write the user may not make goes through systemd-logind's
Session.SetBrightness("leds", name, value) on the user's own graphical
session (the one with a seat): no root, no udev rule. Multicolor LEDs
(`multi_index` "red green blue", `multi_intensity`) get the `hs` cap only
when `multi_intensity` itself is writable: logind sets `brightness` alone.

Writing 0 makes the kernel drop the LED's trigger (phy0rx, kbd-capslock...):
switched off once, it no longer follows its event until the trigger is set
again. Our own uleds device ("hauslicht::kbd_backlight", which the sync
daemon creates for Plasma's applet) is never listed: driving it would loop.
"""

from __future__ import annotations

import colorsys
import logging
import os
import re
from pathlib import Path

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, tr

log = logging.getLogger("hauslicht")

SYSFS = Path("/sys/class/leds")  # tests point it at a tmp tree
OWN_PREFIX = "hauslicht::"       # the daemon's uleds LEDs (haus/desktop.py): never ours to drive
LOGIND = "org.freedesktop.login1"
SESSION_AUTO = "/org/freedesktop/login1/session/auto"
USER_SELF = "/org/freedesktop/login1/user/self"


def trigger_of(text: str) -> str:
    """The active trigger: the [bracketed] word of `trigger`, "none" when unset."""
    m = re.search(r"\[([^\]]+)\]", text)
    return m.group(1) if m else "none"


class Logind:
    """Session.SetBrightness on the user's graphical session. The daemon is a
    user service outside any session: "session/auto" would not resolve, so
    the session is the user's Display one (user/self), auto as a fallback."""

    def __init__(self) -> None:
        self.bus = None
        self.session: str | None = None

    async def _call(self, path: str, interface: str, member: str, signature: str, body: list):
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus
        if self.bus is None or not self.bus.connected:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self.session = None
        reply = await self.bus.call(Message(destination=LOGIND, path=path, interface=interface,
                                            member=member, signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise PermissionError(f"logind {member}: {reply.error_name} {' '.join(map(str, reply.body))}".strip())
        return reply.body

    async def set_brightness(self, subsystem: str, name: str, value: int) -> None:
        if self.session is None:
            try:
                body = await self._call(USER_SELF, "org.freedesktop.DBus.Properties", "Get", "ss",
                                        [f"{LOGIND}.User", "Display"])
                sid, path = body[0].value
                self.session = path if sid else SESSION_AUTO
            except PermissionError:
                self.session = SESSION_AUTO
        await self._call(self.session, f"{LOGIND}.Session", "SetBrightness", "ssu", [subsystem, name, int(value)])

    async def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None


class SysLedBackend(Backend):
    kind = "sysled"
    title = "LED (Linux LED class, /sys/class/leds)"
    product = "LED"
    experimental = True
    autoconnect = False  # capslock, Wi-Fi, disk LEDs: never taken over by themselves
    fields = (
        {"key": "led", "label": "LED name", "label_de": "LED-Name",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.logind = Logind()
        self.presence = Presence(ctx, self._probe)
        self.color: dict[str, tuple[float, float, float]] = {}  # bulb id -> rgb 0..1 (multicolor)

    # ------------------------------------------------------------ sysfs
    @staticmethod
    def _dir(name: str) -> Path:
        if not name or "/" in name or name in (".", "..") or name.startswith(OWN_PREFIX):
            raise UserError(tr(f"not an LED name: {name!r}", f"Kein LED-Name: {name!r}"))
        return SYSFS / name

    def _read(self, name: str, attr: str, default: str = "") -> str:
        try:
            return (self._dir(name) / attr).read_text().strip()
        except OSError:
            return default

    @staticmethod
    def own(name: str) -> bool:
        """One of the daemon's own uleds LEDs (by name, or by its device under misc/uleds)."""
        if name.startswith(OWN_PREFIX):
            return True
        try:
            return "/misc/uleds/" in str((SYSFS / name).resolve())
        except OSError:
            return False

    def max_brightness(self, name: str) -> int:
        try:
            return max(1, int(self._read(name, "max_brightness", "1")))
        except ValueError:
            return 1

    def channels(self, name: str) -> list[str]:
        return self._read(name, "multi_index").split()

    def color_capable(self, name: str) -> bool:
        ch = self.channels(name)
        path = self._dir(name) / "multi_intensity"
        return {"red", "green", "blue"} <= set(ch) and os.access(path, os.W_OK)

    # ------------------------------------------------------------ the API
    def caps(self, bulb) -> set[str]:
        name = bulb.config.get("led", "")
        caps = {"onoff"} if self.max_brightness(name) <= 1 else {"onoff", "dim"}
        if self.color_capable(name):
            caps.add("hs")
        return caps

    def step(self, bulb) -> float:
        return 100.0 / self.max_brightness(bulb.config.get("led", ""))

    def min_lit(self, bulb) -> float:
        return self.step(bulb)

    def details(self, bulb) -> dict:
        name = bulb.config.get("led", "")
        return {"model": self.product, "address": name, "trigger": trigger_of(self._read(name, "trigger")),
                "max_brightness": self.max_brightness(name)}

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        if online is None:
            online = self._dir(bulb.config.get("led", "")).is_dir()
        return {"connected": bool(online), "online": online}

    async def _probe(self, bulb) -> dict | None:
        return {} if self._dir(bulb.config.get("led", "")).is_dir() else None

    async def _write_brightness(self, name: str, value: int) -> None:
        path = self._dir(name) / "brightness"
        if os.access(path, os.W_OK):
            try:
                path.write_text(f"{value}\n")
                return
            except PermissionError:
                pass
        await self.logind.set_brightness("leds", name, value)

    async def apply(self, bulb, percent: float) -> None:
        name = bulb.config.get("led", "")
        if not self._dir(name).is_dir():
            self.presence.lost(bulb, "gone from /sys/class/leds")
            raise Offline(f"{bulb.id}: LED {name} is gone")
        top = self.max_brightness(name)
        value = 0 if percent <= 0 else max(1, min(top, round(percent * top / 100.0)))
        try:
            await self._write_brightness(name, value)
        except (FileNotFoundError, NotADirectoryError) as exc:
            self.presence.lost(bulb, str(exc))
            raise Offline(f"{bulb.id}: {exc}") from exc
        self.presence.ok(bulb)

    async def apply_color(self, bulb, color: dict) -> bool:
        name = bulb.config.get("led", "")
        if color.get("mode") != "hs" or not self.color_capable(name):
            return False
        rgb = colorsys.hsv_to_rgb((float(color["hue"]) % 360) / 360.0, float(color["sat"]) / 100.0, 1.0)
        top = self.max_brightness(name)
        level = dict(zip(("red", "green", "blue"), rgb))
        values = [round(level.get(ch, 0.0) * top) for ch in self.channels(name)]
        try:
            (self._dir(name) / "multi_intensity").write_text(" ".join(map(str, values)) + "\n")
        except FileNotFoundError as exc:
            self.presence.lost(bulb, str(exc))
            raise Offline(f"{bulb.id}: {exc}") from exc
        self.color[bulb.id] = rgb
        return True

    async def discover(self, timeout: float) -> list[dict]:
        found = []
        try:
            names = sorted(p.name for p in SYSFS.iterdir())
        except OSError:
            return []
        for name in names:
            if self.own(name):
                continue
            trig = trigger_of(self._read(name, "trigger"))
            found.append({"kind": self.kind, "address": name,
                          "name": name if trig == "none" else f"{name} ({trig})",
                          "trigger": trig, "config": {"led": name}})
        return found

    async def add(self, config: dict) -> dict:
        name = str(config.get("led", "")).strip()
        if self.own(name) or not self._dir(name).is_dir():
            raise UserError(tr(f"no LED {name} in {SYSFS}", f"Keine LED {name} in {SYSFS}"))
        config["led"] = name
        config["id"] = re.sub(r"[^A-Za-z0-9_.:-]", "_", name)
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self.color.pop(bulb.id, None)

    async def stop(self) -> None:  # what the LED shows stays
        await self.presence.stop()
        await self.logind.close()
