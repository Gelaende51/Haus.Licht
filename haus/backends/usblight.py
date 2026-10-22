"""USB status lights (BlinkStick, Luxafor, blink(1), Kuando, Embrava, MuteMe,
EPOS, Plantronics, CompuLab fit-statUSB...) through busylight-core
(JnyJny, Apache-2.0; the successor of busylight-for-humans).

busylight-core API used (2.5): `Hardware.enumerate(ConnectionType.HID)`
lists devices without opening them; `Light.subclasses()` / `cls.claims(hw)`
pick the class (BlinkStick variants share 20a0:41e5 and tell themselves
apart by serial and release number); `cls(hw, reset=False, exclusive=True)`
opens it without changing what it shows; `light.on((r, g, b))`, `off()`,
`cancel_tasks()`, `release()`. A failed write raises LightUnavailableError.
Kuando lights go dark without a keepalive every ~15 s: the library's
`_on` starts it as an asyncio task of the running loop (on() is called on
the daemon's loop, so it runs there), and `off()` stops it.

Only HID lights: busylight-core also claims serial ports (MuteSync on any
CP210x 10c4:ea60, BusyTag on Espressif 303a:81df), and opening a serial port
resets an Arduino; those ports belong to the `serial` kind, opened only by
the user's choice.

The PyPI hidapi wheel's `hid` module is the libusb backend on Linux: paths
are "bus:device:interface" (hex), and opening needs the USB device node
/dev/bus/usb/BBB/DDD, not /dev/hidrawN. The root setup's udev rule grants
both to the logged-in user (uaccess).

RGB: brightness scales the current colour (white until a colour is set).
"""

from __future__ import annotations

import colorsys
import contextlib
import logging
import os
import re

from . import Backend, NeedsAccess, Offline
from ..common import UserError
from ._bulbkit import Presence, no_access, tr

log = logging.getLogger("hauslicht")

LIBUSB_PATH = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4}):")
USB_DEV = "/dev/bus/usb"  # tests point it at a tmp tree


def _hardware() -> list[tuple[type, object]]:
    """(Light subclass, Hardware) of every HID status light, nothing opened."""
    from busylight_core import Light
    from busylight_core.hardware import ConnectionType, Hardware
    out = []
    for hw in Hardware.enumerate(ConnectionType.HID):
        for cls in Light.subclasses():
            if cls.claims(hw):
                out.append((cls, hw))
                break
    return out


def _open(cls, hw):
    """Open it, leaving what it shows as it is."""
    return cls(hw, reset=False, exclusive=True)


def _path(hw) -> str:
    p = hw.path
    return p.decode(errors="replace") if isinstance(p, bytes) else str(p)


def node_of(path: str) -> str | None:
    """The device node opening `path` needs: /dev/hidrawN as is, a libusb
    "bus:device:interface" as /dev/bus/usb/BBB/DDD."""
    if path.startswith("/dev/"):
        return path
    m = LIBUSB_PATH.match(path)
    return f"{USB_DEV}/{int(m.group(1), 16):03d}/{int(m.group(2), 16):03d}" if m else None


def can_open(path: str) -> bool:
    node = node_of(path)
    return node is None or not os.path.exists(node) or os.access(node, os.R_OK | os.W_OK)


def device_key(hw) -> str:
    """Stable when the light has a serial: vid:pid:serial; else vid:pid:path
    (changes when it moves to another port)."""
    serial = str(hw.serial_number or "").strip()
    head = f"{hw.vendor_id:04x}:{hw.product_id:04x}"
    return f"{head}:{serial}" if serial else f"{head}:@{_path(hw)}"


def describe(cls, hw) -> str:
    try:
        name = cls.supported_device_ids.get((hw.vendor_id, hw.product_id)) or cls.__name__
        vendor = cls.vendor()
    except Exception:
        return getattr(cls, "__name__", "USB light")
    return name if name.lower().startswith(vendor.lower()) else f"{vendor} {name}"


class UsbLightBackend(Backend):
    kind = "usblight"
    title = "USB status light (BlinkStick, Luxafor, blink(1), Kuando, Embrava, MuteMe, …)"
    product = "USB light"
    experimental = True
    requires = ("busylight-core",)
    autoconnect = False  # a busylight may serve a call app: taken over only by the user's choice
    fields = (
        {"key": "device", "label": "Device", "label_de": "Gerät",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.lights: dict[str, object] = {}
        self.rgb: dict[str, tuple[float, float, float]] = {}  # full-scale colour 0..1
        self.level: dict[str, float] = {}
        self.presence = Presence(ctx, self._probe)

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim", "hs"}

    def step(self, bulb) -> float:
        return 100.0 / 255

    def details(self, bulb) -> dict:
        cfg = bulb.config
        return {"model": cfg.get("model") or self.product, "address": cfg.get("device"),
                "serial": cfg.get("serial") or None}

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bulb.id in self.lights, "online": online}

    # ------------------------------------------------------------ device
    def _find(self, config: dict):
        key = str(config.get("device", ""))
        for cls, hw in _hardware():
            if device_key(hw) == key:
                return cls, hw
        return None

    def _light(self, bulb):
        light = self.lights.get(bulb.id)
        if light is not None:
            return light
        got = self._find(bulb.config)
        if got is None:
            self.presence.lost(bulb, "unplugged")
            raise Offline(f"{bulb.id}: not plugged in")
        cls, hw = got
        if not can_open(_path(hw)):
            raise PermissionError(no_access(node_of(_path(hw))))
        light = _open(cls, hw)
        self.lights[bulb.id] = light
        return light

    def _close(self, bid: str) -> None:
        light = self.lights.pop(bid, None)
        if light is None:
            return
        with contextlib.suppress(Exception):
            light.cancel_tasks()
        with contextlib.suppress(Exception):
            light.release()

    def _rgb(self, bid: str) -> tuple[int, int, int]:
        percent = max(0.0, min(100.0, self.level.get(bid, 0.0)))
        r, g, b = self.rgb.get(bid, (1.0, 1.0, 1.0))
        return tuple(round(c * 255 * percent / 100.0) for c in (r, g, b))

    def _show(self, bulb) -> None:
        light = self._light(bulb)
        rgb = self._rgb(bulb.id)
        try:
            if any(rgb):
                light.on(rgb)
            else:
                light.off()
        except Exception as exc:  # LightUnavailableError, OSError: unplugged
            self._close(bulb.id)
            self.presence.lost(bulb, str(exc) or type(exc).__name__)
            raise Offline(f"{bulb.id}: {exc or type(exc).__name__}") from exc
        self.presence.ok(bulb)

    async def _probe(self, bulb) -> dict | None:
        try:
            self._light(bulb)
        except Exception:
            return None
        return {}

    # ------------------------------------------------------------ the API
    async def apply(self, bulb, percent: float) -> None:
        self.level[bulb.id] = percent
        self._show(bulb)

    async def apply_color(self, bulb, color: dict) -> bool:
        if color.get("mode") != "hs":
            return False
        self.rgb[bulb.id] = colorsys.hsv_to_rgb((float(color["hue"]) % 360) / 360.0,
                                                float(color["sat"]) / 100.0, 1.0)
        if self.level.get(bulb.id, 0.0) > 0:
            self._show(bulb)
        return True

    async def discover(self, timeout: float) -> list[dict]:
        found, seen, blocked = [], set(), None
        for cls, hw in _hardware():
            key = device_key(hw)
            if key in seen:  # one entry per light, not per HID interface
                continue
            seen.add(key)
            entry = {"kind": self.kind, "address": key, "name": describe(cls, hw),
                     "config": {"device": key}}
            if not can_open(_path(hw)):
                entry["access"] = False
                blocked = blocked or node_of(_path(hw))
            found.append(entry)
        if blocked:
            raise NeedsAccess(no_access(blocked), found)
        return found

    async def add(self, config: dict) -> dict:
        key = str(config.get("device", "")).strip()
        got = self._find({"device": key})
        if got is None:
            raise UserError(tr(f"no USB light {key} is plugged in", f"Kein USB-Licht {key} angesteckt"))
        cls, hw = got
        if not can_open(_path(hw)):
            raise NeedsAccess(no_access(node_of(_path(hw))))
        try:
            _open(cls, hw).release()  # it opens: the light stays as it is
        except Exception as exc:
            raise UserError(tr(f"{describe(cls, hw)} does not open: {exc}",
                               f"{describe(cls, hw)} lässt sich nicht öffnen: {exc}")) from exc
        serial = str(hw.serial_number or "").strip()
        config.update({"device": key, "model": describe(cls, hw), "vid": hw.vendor_id, "pid": hw.product_id})
        if serial:
            config["serial"] = serial
        suffix = serial or f"{hw.vendor_id:04x}{hw.product_id:04x}-{_path(hw)}"
        config["id"] = re.sub(r"[^A-Za-z0-9_.-]", "_", suffix)
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self._close(bulb.id)
        self.rgb.pop(bulb.id, None)
        self.level.pop(bulb.id, None)

    async def resume(self) -> None:
        for bid in list(self.lights):  # USB re-enumerates over sleep: open afresh
            self._close(bid)

    async def stop(self) -> None:  # what the light shows stays (a Kuando dims without its keepalive)
        await self.presence.stop()
        for bid in list(self.lights):
            self._close(bid)
