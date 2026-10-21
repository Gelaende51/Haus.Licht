"""The laptop side: backlight, panel, lid, Night Light, colour scheme, the
keyboard-slider LED in Plasma's brightness applet and the suspend guard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

from .common import _

log = logging.getLogger("hauslicht")

BACKLIGHT_DIR = Path("/sys/class/backlight")
NIGHT_POLL_S = 60.0  # besides the PropertiesChanged signal, in case it is missed
NL_DEST, NL_PATH, NL_IFACE = "org.kde.KWin", "/org/kde/KWin/NightLight", "org.kde.KWin.NightLight"
PORTAL_DEST, PORTAL_PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Settings"


def find_backlight() -> Path | None:
    devices = sorted(BACKLIGHT_DIR.iterdir()) if BACKLIGHT_DIR.exists() else []
    return devices[0] if devices else None


def read_backlight(dev: Path) -> float:
    return int((dev / "brightness").read_text()) / int((dev / "max_brightness").read_text()) * 100


def lid_closed() -> bool:
    try:
        return any("closed" in f.read_text() for f in Path("/proc/acpi/button/lid").glob("*/state"))
    except OSError:
        return False


LID_DIR = Path("/proc/acpi/button/lid")
DMI_CHASSIS = Path("/sys/class/dmi/id/chassis_type")
DRM_DIR = Path("/sys/class/drm")
LAPTOP_CHASSIS = {8, 9, 10, 14, 31, 32}  # portable, laptop, notebook, sub-notebook, convertible, detachable
DDC_POLL_S = 10.0  # ddcutil takes a second or more per display: rarely
BUILTIN_PANELS = ("eDP", "LVDS", "DSI")


def is_laptop(lid_dir: Path = LID_DIR, chassis: Path = DMI_CHASSIS) -> bool:
    """A lid (ACPI lid button) or a portable chassis type."""
    with contextlib.suppress(OSError):
        if any(lid_dir.iterdir()):
            return True
    try:
        return int(chassis.read_text().strip()) in LAPTOP_CHASSIS
    except (OSError, ValueError):
        return False


def run_ddcutil(args: list[str]) -> str:
    """ddcutil's stdout ("" on any failure)."""
    try:
        return subprocess.run(["ddcutil", *args], capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ddc_possible() -> bool:
    """ddcutil installed and at least one i2c device the user may open."""
    return shutil.which("ddcutil") is not None and any(
        os.access(dev, os.R_OK | os.W_OK) for dev in Path("/dev").glob("i2c-*"))


class MonitorPower:
    """Desktops only: the monitors switched off by the user, never standby.

    Per DRM connector (/sys/class/drm/card*-*): a monitor that was in use
    (connected, enabled, dpms On) and then reads disconnected on two polls in a
    row was switched off (DisplayPort drops hot-plug detect). A connector whose
    dpms is Off (standby, blanking) never counts, nor does a disconnect while
    dpms was already Off (standby can drop DP links too). With ddcutil, VCP D6
    05 (off by the power button) counts as off, 04 (standby) is ignored, 01 is
    on. poll() returns ("off", connector) when the last monitor that was on is
    switched off, ("on", connector) when one comes back, else None."""

    def __init__(self, drm_dir: Path = DRM_DIR, ddc=None) -> None:
        self.drm_dir = drm_dir
        self.ddc = ddc  # runner(args) -> stdout, or None: no DDC/CI
        self.seen: dict[str, dict] = {}  # connector -> last status/dpms/enabled read
        self.pending: set[str] = set()  # disconnected once while in use: confirm on the next poll
        self.known: set[str] = set()  # connectors that were in use
        self.off_hotplug: set[str] = set()
        self.off_ddc: set[str] = set()
        self.ddc_displays: dict[str, str] = {}  # connector -> ddcutil display number
        self.ddc_answer: dict[str, int] = {}  # connector -> last D6 value
        self.ddc_connected: frozenset[str] | None = None  # the set detect ran for
        self.next_ddc = 0.0
        self.all_off = False

    def read(self) -> dict[str, dict]:
        found = {}
        for path in sorted(self.drm_dir.glob("card*-*")):
            name = path.name
            if any(f"-{kind}-" in name for kind in BUILTIN_PANELS) or "Writeback" in name:
                continue
            try:
                status = (path / "status").read_text().strip()
            except OSError:
                continue
            def opt(f: str) -> str:
                try:
                    return (path / f).read_text().strip()
                except OSError:
                    return ""
            found[name] = {"status": status, "dpms": opt("dpms"), "enabled": opt("enabled")}
        return found

    @staticmethod
    def in_use(c: dict) -> bool:
        return c["status"] == "connected" and c["enabled"] != "disabled" and c["dpms"] == "On"

    def poll(self, baseline: bool = False) -> tuple[str, str] | None:
        """Read sysfs once; baseline (after a resume): take the state as it is,
        with no event."""
        now = self.read()
        changed = None
        for name, cur in now.items():
            prev = self.seen.get(name)
            if self.in_use(cur):
                self.known.add(name)
            if cur["status"] == "connected":
                self.pending.discard(name)
                if name in self.off_hotplug or name in self.off_ddc:
                    self.off_hotplug.discard(name)
                    self.off_ddc.discard(name)
                    changed = changed or name
                    if not baseline:
                        log.debug("monitor power: %s switched on", name)
                self.seen[name] = cur
                continue
            if baseline:
                self.pending.discard(name)
                self.seen[name] = cur
                continue
            if name in self.pending:  # second disconnected read: switched off
                self.pending.discard(name)
                self.off_hotplug.add(name)
                changed = name
                log.debug("monitor power: %s switched off (disconnected while on)", name)
            elif prev is not None and self.in_use(prev):
                self.pending.add(name)  # keep prev: its dpms was On
                continue
            self.seen[name] = cur
        for name in set(self.seen) - set(now):  # connector gone (dock unplugged): as disconnected
            self.seen.pop(name)
            self.pending.discard(name)
        return self.settle(changed, baseline)

    def settle(self, changed: str | None, baseline: bool = False) -> tuple[str, str] | None:
        off = self.off_hotplug | self.off_ddc
        all_off = bool(self.known) and self.known <= off
        if all_off == self.all_off:
            return None
        self.all_off = all_off
        if baseline or changed is None:
            return None
        return ("off" if all_off else "on", changed)

    # ---- DDC/CI (optional, slow: the core runs it in a thread)

    def ddc_due(self, clock: float | None = None) -> bool:
        now = time.monotonic() if clock is None else clock
        if self.ddc is None or now < self.next_ddc:
            return False
        self.next_ddc = now + DDC_POLL_S
        return True

    def ddc_detect(self) -> None:
        displays, number = {}, None
        for line in self.ddc(["detect", "--terse"]).splitlines():
            m = re.match(r"\s*Display\s+(\d+)", line)
            if m:
                number = m.group(1)
                continue
            m = re.match(r"\s*DRM[_ ]connector:\s*(\S+)", line)
            if m and number is not None:
                displays[m.group(1)] = number
        self.ddc_displays = displays

    def ddc_read(self) -> dict[str, int]:
        """VCP D6 of every detected display -> {connector: value}; blocking."""
        connected = frozenset(n for n, c in self.seen.items() if c["status"] == "connected")
        if connected != self.ddc_connected:
            self.ddc_detect()
            self.ddc_connected = connected
        answers = {}
        for name, number in self.ddc_displays.items():
            out = self.ddc(["getvcp", "D6", "--display", number, "--brief"])
            m = re.search(r"\bD6\b.*?x([0-9a-fA-F]{2})", out)
            if m:
                answers[name] = int(m.group(1), 16)
        return answers

    def ddc_apply(self, answers: dict[str, int]) -> tuple[str, str] | None:
        """05 = off by the power button: off (only while dpms said On); 04 =
        standby: ignored; 01 = on."""
        changed = None
        for name, value in answers.items():
            self.ddc_answer[name] = value
            cur = self.seen.get(name)
            if value == 0x01:
                self.known.add(name)
                if name in self.off_ddc:
                    self.off_ddc.discard(name)
                    changed = changed or name
                    log.debug("monitor power: %s switched on (DDC power mode 01)", name)
            elif value == 0x05 and name not in self.off_ddc and name in self.known:
                if cur is not None and cur["dpms"] not in ("On", ""):
                    continue  # in standby: never
                self.off_ddc.add(name)
                changed = name
                log.debug("monitor power: %s switched off (DDC power mode 05)", name)
        return self.settle(changed)

    def status(self, laptop: bool) -> dict:
        monitors = []
        for name, c in self.seen.items():
            watch = ("ddc" if name in self.ddc_answer
                     else "hotplug" if "-DP-" in name else "none")
            monitors.append({"connector": name, "status": c["status"], "dpms": c["dpms"], "watch": watch,
                             "off": name in self.off_hotplug or name in self.off_ddc})
        available, why = self.available(laptop)
        return {"available": available, "why": why, "monitors": monitors}

    def available(self, laptop: bool) -> tuple[bool, str]:
        if laptop:
            return False, _("a laptop: the lid switches the lights instead")
        if any(m in self.ddc_answer for m in self.seen) or any("-DP-" in n for n in self.seen):
            return True, ""
        if not self.seen:
            return False, _("no monitor connector found")
        if self.ddc is None:
            return False, _("no DisplayPort monitor, and ddcutil is missing or may not use i2c: "
                            "the monitor's power switch cannot be seen")
        return False, _("no DisplayPort monitor and no DDC/CI answer: the monitor's power switch cannot be seen")


def find_panel() -> Path | None:
    """The DRM connector of the built-in screen (eDP, LVDS or DSI)."""
    for pattern in ("card*-eDP-*", "card*-LVDS-*", "card*-DSI-*"):
        found = sorted(Path("/sys/class/drm").glob(pattern))
        if found:
            return found[0]
    return None


def panel_on(panel: Path | None) -> bool:
    """False while the panel is blanked (DPMS off) or switched off (lid closed)."""
    if panel is None:
        return True
    try:
        return ((panel / "dpms").read_text().strip() == "On"
                and (panel / "enabled").read_text().strip() == "enabled")
    except OSError:
        return True


class Environment:
    """Read-only desktop signals for the profiles' `track`:
    night / day from KWin Night Light's `daylight` (`running` is true day and
    night whenever Night Light is on, so it says nothing about the hour),
    dark / light from the XDG portal's color-scheme (1 dark, 2 light);
    screen_kelvin from Night Light's `currentTemperature` (6500 = no shift)
    for the colour-following profiles. on_change (set by the core) is called
    when screen_kelvin changes."""

    def __init__(self) -> None:
        self.daylight: bool | None = None  # None: Night Light unreadable
        self.scheme: int | None = None
        self.screen_kelvin: int | None = None  # None: Night Light unreadable
        self.on_change = None
        self.bus = None

    @property
    def night(self) -> bool:
        return self.daylight is False

    @property
    def day(self) -> bool:
        return not self.night  # unreadable: "not night"

    @property
    def scheme_name(self) -> str | None:
        return {0: "default", 1: "dark", 2: "light"}.get(self.scheme)

    async def start(self) -> None:
        try:
            from dbus_fast import Message
            from dbus_fast.aio import MessageBus
            self.bus = await MessageBus().connect()
            for rule in (f"type='signal',interface='{PORTAL_IFACE}',member='SettingChanged'",
                         f"type='signal',interface='org.freedesktop.DBus.Properties',"
                         f"member='PropertiesChanged',path='{NL_PATH}'"):
                await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                            interface="org.freedesktop.DBus", member="AddMatch",
                                            signature="s", body=[rule]))
            self.bus.add_message_handler(self._signal)
        except Exception as exc:
            log.warning("no Night Light / colour scheme signals (%s)", exc)
            self.bus = None
            return
        await self._read_scheme()
        asyncio.get_running_loop().create_task(self._poll_night())

    async def _call(self, dest: str, path: str, iface: str, member: str, signature: str, body: list):
        from dbus_fast import Message, MessageType
        reply = await self.bus.call(Message(destination=dest, path=path, interface=iface, member=member,
                                            signature=signature, body=body))
        if reply is None or reply.message_type == MessageType.ERROR:
            return None
        value = reply.body[0]
        while hasattr(value, "signature") and hasattr(value, "value"):  # unwrap (nested) variants
            value = value.value
        return value

    async def _read_scheme(self) -> None:
        value = await self._call(PORTAL_DEST, PORTAL_PATH, PORTAL_IFACE, "ReadOne", "ss",
                                 ["org.freedesktop.appearance", "color-scheme"])
        self._set_scheme(value)

    def _set_scheme(self, value) -> None:
        if isinstance(value, int) and value != self.scheme:
            self.scheme = value
            log.info("colour scheme %s", self.scheme_name)

    def _set_daylight(self, value) -> None:
        if value is None or isinstance(value, bool):
            if value != self.daylight:
                self.daylight = value
                log.info("Night Light: %s", "day" if value else "night" if value is False else "unknown")

    def _set_kelvin(self, value) -> None:
        value = int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        if value != self.screen_kelvin:
            self.screen_kelvin = value
            log.debug("Night Light: %s K", value)
            if self.on_change is not None:
                self.on_change()

    async def _poll_night(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                self._set_daylight(await self._call(NL_DEST, NL_PATH, "org.freedesktop.DBus.Properties",
                                                    "Get", "ss", [NL_IFACE, "daylight"]))
            with contextlib.suppress(Exception):
                self._set_kelvin(await self._call(NL_DEST, NL_PATH, "org.freedesktop.DBus.Properties",
                                                  "Get", "ss", [NL_IFACE, "currentTemperature"]))
            await asyncio.sleep(NIGHT_POLL_S)

    def _signal(self, msg):
        try:
            if msg.member == "SettingChanged" and msg.body[:2] == ["org.freedesktop.appearance", "color-scheme"]:
                value = msg.body[2]
                self._set_scheme(getattr(value, "value", value))
            elif msg.member == "PropertiesChanged" and msg.path == NL_PATH and msg.body[0] == NL_IFACE:
                if "daylight" in msg.body[1]:
                    self._set_daylight(msg.body[1]["daylight"].value)
                if "currentTemperature" in msg.body[1]:
                    self._set_kelvin(msg.body[1]["currentTemperature"].value)
        except Exception as exc:  # a malformed signal must not stop the bus
            log.debug("desktop signal ignored: %s", exc)
        return None


class KbdSlider:
    """Shows up as the keyboard backlight in Plasma's brightness applet.

    The kernel's uleds driver creates an LED class device from user space;
    UPower takes any LED named *kbd_backlight as the keyboard backlight and
    PowerDevil draws its slider. Moving the slider writes the LED, which
    arrives here as a 4-byte int on /dev/uleds.
    """

    NAME = b"hauslicht::kbd_backlight"
    MAX = 100

    def __init__(self, on_change) -> None:
        self.on_change = on_change
        self.fd: int | None = None
        self.ready = False  # the new LED reads 0 until the saved value is restored

    def open(self) -> bool:
        try:
            self.fd = os.open("/dev/uleds", os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            log.warning("no keyboard slider (%s): run install/setup-root.sh once", exc)
            return False
        os.write(self.fd, self.NAME.ljust(64, b"\0") + struct.pack("i", self.MAX))
        asyncio.get_running_loop().add_reader(self.fd, self._readable)
        return True

    def _readable(self) -> None:
        with contextlib.suppress(BlockingIOError):
            data = os.read(self.fd, 4)
            if len(data) == 4 and self.ready:
                self.on_change(struct.unpack("i", data)[0])

    async def restore(self, value: int, timeout: float = 30.0) -> None:
        """Wait until UPower has picked the LED up, then set the saved value."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                "busctl", "--system", "call", "org.freedesktop.UPower",
                "/org/freedesktop/UPower/KbdBacklight", "org.freedesktop.UPower.KbdBacklight",
                "GetMaxBrightness", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _err = await proc.communicate()
            if out.split()[-1:] == [str(self.MAX).encode()]:
                break
            await asyncio.sleep(1.0)
        else:
            log.warning("UPower did not report the slider within %.0f s", timeout)
        await self.push(value)
        await asyncio.sleep(0.5)  # let the echo of our own write pass
        self.ready = True
        await self.wake_powerdevil()

    @staticmethod
    async def wake_powerdevil() -> None:
        """PowerDevil looks for a keyboard backlight only when it starts; at login
        it usually wins the race against this daemon. Restart it once if so."""
        call = ("busctl", "--user", "call", "org.kde.Solid.PowerManagement",
                "/org/kde/Solid/PowerManagement/Actions/KeyboardBrightnessControl",
                "org.kde.Solid.PowerManagement.Actions.KeyboardBrightnessControl",
                "keyboardBrightnessMax")
        proc = await asyncio.create_subprocess_exec(
            *call, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _err = await proc.communicate()
        if proc.returncode != 0:
            log.info("PowerDevil not reachable; no Plasma slider")
            return
        if out.split()[-1:] != [b"0"]:
            return  # the slider is already offered
        log.info("restarting PowerDevil so the brightness applet shows the slider")
        restart = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "plasma-powerdevil.service")
        await restart.wait()

    @staticmethod
    async def push(value: int) -> bool:
        """Set the value through UPower so Plasma's slider moves along."""
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call", "org.freedesktop.UPower",
            "/org/freedesktop/UPower/KbdBacklight", "org.freedesktop.UPower.KbdBacklight",
            "SetBrightness", "i", str(int(value)),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode:
            log.warning("UPower SetBrightness failed: %s", err.decode().strip())
        return proc.returncode == 0

    def close(self) -> None:
        if self.fd is not None:
            asyncio.get_running_loop().remove_reader(self.fd)
            os.close(self.fd)  # the LED device disappears with the fd
            self.fd = None


class SleepGuard:
    """Holds a logind delay inhibitor so the bulbs go off before suspend."""

    def __init__(self, before_sleep, after_wake) -> None:
        self.before_sleep, self.after_wake = before_sleep, after_wake
        self.manager = None
        self.fd: int | None = None

    async def start(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
            path = "/org/freedesktop/login1"
            introspection = await bus.introspect("org.freedesktop.login1", path)
            self.manager = bus.get_proxy_object("org.freedesktop.login1", path, introspection) \
                .get_interface("org.freedesktop.login1.Manager")
        except Exception as exc:
            log.warning("no suspend handling (%s)", exc)
            return
        self.manager.on_prepare_for_sleep(self._prepare)
        await self._inhibit()

    async def _inhibit(self) -> None:
        if self.fd is None:
            self.fd = await self.manager.call_inhibit(
                "sleep", "Haus.Licht", "switch the bulbs off first", "delay")

    def _release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _prepare(self, sleeping: bool) -> None:
        asyncio.get_running_loop().create_task(self._on_prepare(sleeping))

    async def _on_prepare(self, sleeping: bool) -> None:
        if sleeping:
            try:
                await asyncio.wait_for(self.before_sleep(), timeout=4.0)
            except Exception as exc:
                log.warning("bulbs not switched off before sleep: %s", exc)
            finally:
                self._release()
        else:
            await self.after_wake()
            await self._inhibit()


class Desktop:
    """Everything the core reads from or writes to the laptop. With writes off
    (a test instance beside the live one) there is no slider LED, no
    PowerDevil restart, no UPower push and no suspend inhibitor."""

    def __init__(self, on_slider, before_sleep, after_wake, writes: bool = True) -> None:
        self.writes = writes
        self.backlight = find_backlight()
        if self.backlight is None:
            log.warning(_("no backlight device in /sys/class/backlight"))
        self.panel = find_panel()
        self.laptop = is_laptop()
        # desktops only: the monitors' power switches (laptops follow the lid)
        self.monitor_power = None if self.laptop else MonitorPower(ddc=run_ddcutil if ddc_possible() else None)
        self.screen_on = True
        self.last_screen: float | None = None
        self.env = Environment()
        self.slider = KbdSlider(on_slider)
        self.sleep_guard = SleepGuard(before_sleep, after_wake) if writes else None

    def screen_level(self) -> float:
        """Screen brightness %; while the panel is off (DPMS, lid) the last value
        read while it was on: a dark panel may read 0 and must not move the bulbs."""
        self.screen_on = panel_on(self.panel)
        if self.backlight is None:
            return 100.0
        if self.screen_on or self.last_screen is None:
            self.last_screen = read_backlight(self.backlight)
        return self.last_screen

    def lid_closed(self) -> bool:
        return lid_closed()

    @property
    def slider_ready(self) -> bool:
        return self.writes and self.slider.ready

    async def push_slider(self, value: int) -> bool:
        return self.writes and await KbdSlider.push(value)

    async def start(self, slider_value: int) -> None:
        await self.env.start()
        if self.writes and self.slider.open():
            asyncio.get_running_loop().create_task(self.slider.restore(slider_value))
        if self.sleep_guard is not None:
            await self.sleep_guard.start()

    def close(self) -> None:
        self.slider.close()
