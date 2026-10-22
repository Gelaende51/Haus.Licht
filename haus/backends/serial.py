"""LED controllers on a serial port (Arduino / ESP sketches, WLED), with pyserial.

Protocols:
- adalight: "Ada", hi, lo, hi^lo^0x55 (hi/lo: LED count - 1), then R G B per
  LED. Spoken by the Adalight sketch and its Prismatik/Hyperion relatives,
  and by WLED (its realtime serial input). Many sketches and WLED blank, or
  WLED falls back to its own state, ~2.5 s after the last frame, so the
  current frame is sent again every KEEPALIVE_S while the bulb is driven
  (a dark frame too: WLED's own state may be lit).
- wled: WLED's JSON API over serial, one line per change:
  {"on": .., "bri": 0-255, "seg": [{"col": [[r, g, b]]}]} (the colour only
  once one was set: WLED keeps its own until then).

Opening a port toggles DTR, which resets an Arduino (and would abort a 3D
printer's print): discover only lists the USB serial ports pyserial knows,
never opens one; only `add` (the user's explicit choice) and the running
bulb open the selected port, and the bulb keeps it open. After opening, it
waits for the Adalight sketch's "Ada" greeting, at most GREETING_S (the
Arduino boots). The port is stored by its /dev/serial/by-id/ name when
there is one (stable when ttyUSB numbers change).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path

from . import Backend, NeedsAccess, Offline
from ..common import UserError
from ._bulbkit import Presence, no_access, tr

log = logging.getLogger("hauslicht")

BY_ID = Path("/dev/serial/by-id")  # tests point it at a tmp tree
GREETING_S = 2.5
KEEPALIVE_S = 1.0
BAUD = 115200
PROTOCOLS = ("adalight", "wled")
MAX_LEDS = 65536  # 16-bit count field


def adalight_frame(rgb: tuple[int, int, int], count: int) -> bytes:
    n = max(1, min(MAX_LEDS, int(count)))
    hi, lo = ((n - 1) >> 8) & 0xFF, (n - 1) & 0xFF
    return b"Ada" + bytes((hi, lo, hi ^ lo ^ 0x55)) + bytes(rgb) * n


def wled_line(on: bool, bri: int, rgb: tuple[int, int, int] | None) -> bytes:
    msg: dict = {"on": on}
    if on:
        msg["bri"] = bri
        if rgb is not None:
            msg["seg"] = [{"col": [list(rgb)]}]
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def is_url(port: str) -> bool:
    return "://" in port  # pyserial URL handlers (loop://, socket://, rfc2217://)


def by_id_of(device: str) -> str | None:
    """The /dev/serial/by-id/ link of a device node, if udev made one."""
    try:
        real = os.path.realpath(device)
        for link in sorted(BY_ID.iterdir()):
            if os.path.realpath(link) == real:
                return str(link)
    except OSError:
        pass
    return None


def _ports() -> list:
    """USB serial ports as pyserial lists them (sysfs only: nothing is opened)."""
    from serial.tools import list_ports
    return [p for p in list_ports.comports() if p.vid is not None]


def _open_port(port: str, baud: int):
    import serial
    return serial.serial_for_url(port, baudrate=baud, timeout=0, write_timeout=1)


class SerialBackend(Backend):
    kind = "serial"
    title = "Serial LED controller (Adalight, WLED over USB)"
    product = "LED controller"
    experimental = True
    requires = ("pyserial",)
    modules = ("serial",)
    autoconnect = False  # opening a port resets the board on it: only by the user's choice
    fields = (
        {"key": "port", "label": "Serial port", "label_de": "Serielle Schnittstelle",
         "type": "text", "required": True, "default": ""},
        {"key": "protocol", "label": "Protocol", "label_de": "Protokoll",
         "type": "choice", "choices": list(PROTOCOLS), "default": "adalight"},
        {"key": "baud", "label": "Baud rate", "label_de": "Baudrate", "type": "int", "default": BAUD},
        {"key": "leds", "label": "LED count (Adalight)", "label_de": "Anzahl LEDs (Adalight)",
         "type": "int", "default": 1},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.links: dict[str, object] = {}     # bulb id -> open port
        self.pending: dict[str, object] = {}   # port path -> opened by add, taken by the bulb
        self.level: dict[str, float] = {}
        self.rgb: dict[str, tuple[float, float, float]] = {}
        self.last: dict[str, bytes] = {}       # the frame the keepalive repeats
        self.keep: dict[str, asyncio.Task] = {}
        self.presence = Presence(ctx, self._probe)

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim", "hs"}

    def step(self, bulb) -> float:
        return 100.0 / 255

    def details(self, bulb) -> dict:
        cfg = bulb.config
        return {"model": f"{self.product} ({cfg.get('protocol', 'adalight')})", "address": cfg.get("port"),
                "baud": cfg.get("baud", BAUD), "leds": cfg.get("leds", 1) if cfg.get("protocol") != "wled" else None}

    def status(self, bulb) -> dict:
        return {"connected": bulb.id in self.links, "online": self.presence.get(bulb.id)}

    # ------------------------------------------------------------ the port
    async def _open(self, port: str, baud: int, protocol: str):
        """Open, then wait for the sketch's greeting (or GREETING_S while it boots)."""
        try:
            link = _open_port(port, baud)
        except OSError as exc:  # serial.SerialException is an IOError
            if not is_url(port) and not os.path.exists(port):
                raise Offline(f"{port} is gone") from exc
            raise
        deadline, seen = time.monotonic() + GREETING_S, b""
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                if n := link.in_waiting:
                    seen = (seen + link.read(n))[-64:]
                    if protocol == "adalight" and b"Ada" in seen:
                        break
            await asyncio.sleep(0.05)
        with contextlib.suppress(OSError):
            link.reset_input_buffer()
        log.info("%s: open (%s)", port, "greeted" if b"Ada" in seen else "no greeting")
        return link

    async def _link(self, bulb):
        link = self.links.get(bulb.id)
        if link is not None:
            return link
        cfg = bulb.config
        port = str(cfg.get("port", ""))
        if not is_url(port) and not os.path.exists(port):
            self.presence.lost(bulb, f"{port} is gone")
            raise Offline(f"{bulb.id}: {port} is gone")
        if not is_url(port) and not os.access(port, os.R_OK | os.W_OK):
            raise PermissionError(no_access(port))
        link = self.pending.pop(port, None)
        if link is None:
            link = await self._open(port, int(cfg.get("baud", BAUD)), cfg.get("protocol", "adalight"))
        self.links[bulb.id] = link
        return link

    def _close(self, bid: str) -> None:
        task = self.keep.pop(bid, None)
        if task is not None:
            task.cancel()
        link = self.links.pop(bid, None)
        if link is not None:
            with contextlib.suppress(Exception):
                link.close()

    def _write(self, bulb, data: bytes) -> None:
        link = self.links.get(bulb.id)
        if link is None:
            raise Offline(f"{bulb.id}: not open")
        try:
            link.write(data)
        except OSError as exc:  # unplugged: SerialException / SerialTimeoutException
            self._close(bulb.id)
            self.presence.lost(bulb, str(exc))
            raise Offline(f"{bulb.id}: {exc}") from exc

    async def _probe(self, bulb) -> dict | None:
        try:
            await self._link(bulb)
        except Exception:
            return None
        return {}

    def _scaled(self, bid: str) -> tuple[int, int, int]:
        percent = max(0.0, min(100.0, self.level.get(bid, 0.0)))
        r, g, b = self.rgb.get(bid, (1.0, 1.0, 1.0))
        return tuple(round(c * 255 * percent / 100.0) for c in (r, g, b))

    def _frame(self, bulb) -> bytes:
        cfg, bid = bulb.config, bulb.id
        if cfg.get("protocol", "adalight") == "wled":
            percent = self.level.get(bid, 0.0)
            rgb = self.rgb.get(bid)
            full = None if rgb is None else tuple(round(c * 255) for c in rgb)
            return wled_line(percent > 0, max(1, min(255, round(percent * 255 / 100.0))), full)
        return adalight_frame(self._scaled(bid), int(cfg.get("leds", 1)))

    async def _send(self, bulb) -> None:
        if self.presence.get(bulb.id) is False and bulb.id not in self.links:
            raise Offline(f"{bulb.id} unreachable")
        await self._link(bulb)
        data = self._frame(bulb)
        self._write(bulb, data)
        self.presence.ok(bulb)
        if bulb.config.get("protocol", "adalight") == "adalight":
            self.last[bulb.id] = data
            if bulb.id not in self.keep:
                self.keep[bulb.id] = asyncio.get_running_loop().create_task(self._keepalive(bulb))

    async def _keepalive(self, bulb) -> None:
        try:
            while bulb.id in self.links:
                await asyncio.sleep(KEEPALIVE_S)
                if (data := self.last.get(bulb.id)) is not None and bulb.id in self.links:
                    self._write(bulb, data)
        except Offline:
            pass
        finally:
            if self.keep.get(bulb.id) is asyncio.current_task():
                self.keep.pop(bulb.id, None)

    # ------------------------------------------------------------ the API
    async def apply(self, bulb, percent: float) -> None:
        self.level[bulb.id] = percent
        await self._send(bulb)

    async def apply_color(self, bulb, color: dict) -> bool:
        if color.get("mode") != "hs":
            return False
        import colorsys
        self.rgb[bulb.id] = colorsys.hsv_to_rgb((float(color["hue"]) % 360) / 360.0,
                                                float(color["sat"]) / 100.0, 1.0)
        if self.level.get(bulb.id, 0.0) > 0:
            await self._send(bulb)
        return True

    async def discover(self, timeout: float) -> list[dict]:
        found, blocked = [], None
        for p in _ports():
            address = by_id_of(p.device) or p.device
            desc = p.description if p.description and p.description != "n/a" else p.device
            entry = {"kind": self.kind, "address": address,
                     "name": f"{desc} ({p.vid:04x}:{p.pid:04x})",
                     "config": {"port": address, "protocol": "adalight", "baud": BAUD, "leds": 1}}
            if not os.access(p.device, os.R_OK | os.W_OK):
                entry["access"] = False
                blocked = blocked or p.device
            found.append(entry)
        if blocked:
            raise NeedsAccess(no_access(blocked), found)
        return found

    async def add(self, config: dict) -> dict:
        port = str(config.get("port", "")).strip()
        protocol = config.get("protocol") or "adalight"
        leds = int(config.get("leds") or 1)
        if not 1 <= leds <= MAX_LEDS:
            raise UserError(tr(f"LED count: 1 to {MAX_LEDS}", f"Anzahl LEDs: 1 bis {MAX_LEDS}"))
        if not is_url(port):
            if not port or not os.path.exists(port):
                raise UserError(tr(f"no serial port {port}", f"Keine serielle Schnittstelle {port}"))
            port = by_id_of(port) or port
            if not os.access(port, os.R_OK | os.W_OK):
                raise NeedsAccess(no_access(os.path.realpath(port)))
        if port not in self.pending:
            try:
                self.pending[port] = await self._open(port, int(config.get("baud") or BAUD), protocol)
            except (OSError, Offline) as exc:
                raise UserError(tr(f"{port} does not open: {exc}", f"{port} lässt sich nicht öffnen: {exc}")) from exc
        config.update({"port": port, "protocol": protocol, "leds": leds, "baud": int(config.get("baud") or BAUD)})
        config["id"] = re.sub(r"[^A-Za-z0-9_.-]", "_", port.rsplit("/", 1)[-1])
        return config

    async def idle(self, idle: bool) -> None:
        if idle:  # nothing driven: no keepalive (the port stays open: no reset on the next use)
            for task in list(self.keep.values()):
                task.cancel()
            self.keep.clear()
            self.last.clear()

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self._close(bulb.id)
        for d in (self.level, self.rgb, self.last):
            d.pop(bulb.id, None)

    async def stop(self) -> None:  # what the LEDs show stays (no dark frame)
        await self.presence.stop()
        for bid in list(self.links):
            self._close(bid)
        for link in self.pending.values():
            with contextlib.suppress(Exception):
                link.close()
        self.pending.clear()
