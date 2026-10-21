"""Follow video: the bulbs a user switched to it follow what is on screen,
each one a region of it (ambilight: one lamp the left side, another the right).

    xdg-desktop-portal ScreenCast (dbus-fast): CreateSession, SelectSources
       (a monitor, cursor hidden, persist_mode 2 with the saved restore
       token), Start (the permission dialog, unless the token restores it),
       OpenPipeWireRemote ─▶ a PipeWire fd + the stream's node id
       │
    gst-launch-1.0 (VideoCapture): pipewiresrc on that fd ─▶ at most FPS
       frames a second ─▶ scaled to W×H (bilinear2: every pixel counts) ─▶
       RGB ─▶ a pipe, read FRAME bytes at a time (nothing on disk)
       │
    Analyser, per frame: the mean colour of each region in linear light
       (whole, the halves, the quarters; on odd sizes the middle row or
       column belongs to both halves), smoothed exponentially over
       `video_smooth` seconds of wall time
       │
    daemon (haus/daemon.py): luminance ─▶ 0 … `video_max` percent, never
       below the lowest lit step; hue and saturation on colour (`hs`) bulbs,
       brightness only on the others; sent through the rate cap and deadband
       shared with follow-audio (haus/followrate.py).

The restore token is a setting (`video_token`): the dialog appears once. The
portal keys it by the caller's app id: Register() on
org.freedesktop.host.portal.Registry gives ours (APP_ID) when its .desktop
file is installed (install/install.sh), else the daemon is an app without
an id (""), which persists all the same. A restart without a token the
permission store still knows never asks: follow-video stays off with a hint.
"""

from __future__ import annotations

import asyncio
import colorsys
import contextlib
import functools
import itertools
import logging
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable

log = logging.getLogger("hauslicht")

W, H = 48, 27  # the frame the bulbs are taken from (16:9; other screens are stretched)
FPS = 5
FRAME = W * H * 3  # RGB, rows packed (W * 3 is a multiple of 4: no padding)
APP_ID = "io.github.gelaende51.HausLicht"

REGIONS = ("whole", "left", "right", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right")
ALIASES = {"up": "top", "down": "bottom", "all": "whole", "full": "whole"}
DEFAULT_REGION = "whole"
SMOOTHS = (0.0, 0.2, 0.5, 1.0, 2.0, 5.0)  # seconds; 0: off
DEFAULT_SMOOTH = 0.5
DEFAULT_MAX = 100
NEEDS_PERMISSION = "Follow video needs permission again"

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
REGISTRY = "org.freedesktop.host.portal.Registry"
STORE = "org.freedesktop.impl.portal.PermissionStore"
STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
STORE_TABLE = "screencast"
MONITOR, CURSOR_HIDDEN, PERSIST_UNTIL_REVOKED = 1, 1, 2


class VideoError(Exception):
    """The screen cast could not start or ended: follow-video goes off."""


# --------------------------------------------------------------------- settings

def check_region(value) -> str:
    v = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    v = ALIASES.get(v, v)
    if v not in REGIONS:
        raise ValueError(f"video region must be one of {', '.join(REGIONS)}")
    return v


def check_max(value) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("video_max must be 0-100") from None
    if not math.isfinite(v) or not 0 <= v <= 100:
        raise ValueError("video_max must be 0-100")
    return int(round(v))


def check_smooth(value) -> float:
    try:
        v = float(str(value).strip().removesuffix("s"))
    except (TypeError, ValueError):
        v = math.nan
    for s in SMOOTHS:
        if abs(v - s) < 1e-6:
            return s
    raise ValueError(f"video_smooth must be one of {', '.join(f'{s:g}' for s in SMOOTHS)} (seconds)")


@functools.lru_cache(maxsize=1)
def _pipewiresrc() -> bool:
    inspect = shutil.which("gst-inspect-1.0")
    if inspect is None:
        return False
    try:
        return subprocess.run([inspect, "--exists", "pipewiresrc"], timeout=5,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def missing() -> list[str]:
    """What the mode lacks: GStreamer's gst-launch-1.0 and its pipewiresrc."""
    if shutil.which("gst-launch-1.0") is None:
        return ["gst-launch-1.0"]
    return [] if _pipewiresrc() else ["pipewiresrc"]


def install_hint() -> str:
    return "install GStreamer (gst-launch-1.0, gst-plugins-base) and its PipeWire plugin (gst-plugin-pipewire)"


# --------------------------------------------------------------------- analysis

def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


LINEAR = tuple(_to_linear(i / 255) for i in range(256))


def region_bounds(region: str, w: int = W, h: int = H) -> tuple[slice, slice]:
    """(rows, columns) of a region; halves of an odd size share the middle."""
    top, bottom = slice(0, (h + 1) // 2), slice(h // 2, h)
    left, right = slice(0, (w + 1) // 2), slice(w // 2, w)
    every_row, every_col = slice(0, h), slice(0, w)
    rows = {"top": top, "bottom": bottom}
    cols = {"left": left, "right": right}
    if region == "whole":
        return every_row, every_col
    if region in rows:
        return rows[region], every_col
    if region in cols:
        return every_row, cols[region]
    v, _, hz = region.partition("-")
    return rows[v], cols[hz]


def frame_means(frame, w: int = W, h: int = H) -> dict[str, tuple[float, float, float]]:
    """Every region's mean colour in linear light (0…1 per channel) of one
    packed RGB frame (bytes, or anything with the buffer protocol)."""
    data = bytes(frame)
    if len(data) != w * h * 3:
        raise ValueError(f"a frame is {w * h * 3} bytes, not {len(data)}")
    get = LINEAR.__getitem__
    stride = w * 3
    halves = {"left": slice(0, (w + 1) // 2), "right": slice(w // 2, w), "": slice(0, w)}
    # per row and column part: the summed linear R, G, B
    rows: dict[str, list[tuple[float, float, float]]] = {k: [] for k in halves}
    for y in range(h):
        row = data[y * stride:(y + 1) * stride]
        for part, cols in halves.items():
            seg = row[cols.start * 3:cols.stop * 3]
            rows[part].append((sum(map(get, seg[0::3])), sum(map(get, seg[1::3])), sum(map(get, seg[2::3]))))
    out = {}
    for region in REGIONS:
        rs, cs = region_bounds(region, w, h)
        part = "" if cs.stop - cs.start == w else ("left" if cs.start == 0 else "right")
        picked = rows[part][rs]
        n = (rs.stop - rs.start) * (cs.stop - cs.start)
        out[region] = tuple(sum(p[c] for p in picked) / n for c in range(3))
    return out


def luminance(rgb: tuple[float, float, float]) -> float:
    """Relative luminance (Rec. 709) of a linear colour, 0…1."""
    r, g, b = rgb
    return max(0.0, min(1.0, 0.2126 * r + 0.7152 * g + 0.0722 * b))


def brightness(lum: float, max_percent: float) -> float:
    """Luminance 0…1 onto 0…max_percent (the lowest lit step: the daemon)."""
    return round(max(0.0, min(1.0, lum)) * max(0.0, min(100.0, float(max_percent))), 1)


def hue_sat(rgb: tuple[float, float, float]) -> tuple[float, float]:
    """Hue (0-360) and saturation (0-100) of a linear colour, as it looks
    (taken from its sRGB encoding)."""
    h, s, _v = colorsys.rgb_to_hsv(*(_to_srgb(c) for c in rgb))
    return round(h * 360.0, 1) % 360.0, round(s * 100.0, 1)


class Analyser:
    """Fed frames with their time; keeps every region's smoothed mean colour.
    smooth: the time constant in seconds (0: none). Time is the caller's:
    tests feed frames as fast as they like."""

    def __init__(self, smooth: float = DEFAULT_SMOOTH, w: int = W, h: int = H) -> None:
        self.smooth, self.w, self.h = smooth, w, h
        self.means: dict[str, tuple[float, float, float]] | None = None
        self.raw: dict[str, tuple[float, float, float]] | None = None
        self.at: float | None = None
        self.frames = 0

    def feed(self, frame, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        raw = frame_means(frame, self.w, self.h)
        self.frames += 1
        self.raw = raw
        if self.means is None or self.smooth <= 0 or self.at is None:
            self.means = raw
        else:
            a = 1.0 - math.exp(-max(0.0, now - self.at) / self.smooth)
            self.means = {r: tuple(m + a * (x - m) for m, x in zip(self.means[r], raw[r])) for r in REGIONS}
        self.at = now

    def color(self, region: str) -> tuple[float, float, float] | None:
        return None if self.means is None else self.means[region]

    def luminance(self, region: str) -> float | None:
        c = self.color(region)
        return None if c is None else luminance(c)


# --------------------------------------------------------------------- portal

def _unwrap(value):
    while hasattr(value, "signature") and hasattr(value, "value"):
        value = value.value
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


class ScreenCast:
    """One portal ScreenCast session on its own session-bus connection (the
    portal closes the session when the connection goes). open() returns
    (pipewire fd, node id, restore token or ""); `closed` is set when the
    portal or the compositor ends the session."""

    def __init__(self) -> None:
        self.bus = None
        self.session: str | None = None
        self.app_id = ""
        self.closed = asyncio.Event()
        self.pending: dict[str, asyncio.Future] = {}
        self.tokens = itertools.count(1)
        self.asking = False  # Start is waiting: the dialog may be up

    async def _call(self, dest: str, path: str, iface: str, member: str, signature: str, body: list):
        from dbus_fast import Message, MessageType
        reply = await self.bus.call(Message(destination=dest, path=path, interface=iface, member=member,
                                            signature=signature, body=body))
        if reply is None:
            raise VideoError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise VideoError(f"{member}: {reply.error_name} {detail}".strip())
        return reply

    async def connect(self) -> None:
        from dbus_fast.aio import MessageBus
        try:
            self.bus = await asyncio.wait_for(MessageBus(negotiate_unix_fd=True).connect(), 5)
        except Exception as exc:
            raise VideoError(f"no session bus: {exc}") from None
        self.bus.add_message_handler(self._signal)
        for member, iface in (("Response", REQUEST), ("Closed", SESSION)):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                             [f"type='signal',interface='{iface}',member='{member}'"])
        # Our app id for the permission (needs the .desktop file); before any
        # other portal call on this connection. Without: an app without an id.
        try:
            await self._call(PORTAL, PORTAL_PATH, REGISTRY, "Register", "sa{sv}", [APP_ID, {}])
            self.app_id = APP_ID
        except VideoError as exc:
            log.debug("follow video: no app id (%s)", exc)

    def _signal(self, msg) -> None:
        from dbus_fast import MessageType
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.interface == REQUEST and msg.member == "Response":
            fut = self.pending.pop(msg.path, None)
            if fut is not None and not fut.done():
                fut.set_result((int(msg.body[0]), _unwrap(msg.body[1])))
        elif msg.interface == SESSION and msg.member == "Closed" and msg.path == self.session:
            self.closed.set()

    async def _request(self, member: str, signature: str, args: list, options: dict) -> dict:
        from dbus_fast import Variant
        token = f"hauslicht{os.getpid()}_{next(self.tokens)}"
        sender = self.bus.unique_name.lstrip(":").replace(".", "_")
        path = f"{PORTAL_PATH}/request/{sender}/{token}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[path] = fut  # before the call: the answer may come at once
        reply = await self._call(PORTAL, PORTAL_PATH, SCREENCAST, member, signature,
                                 [*args, {"handle_token": Variant("s", token), **options}])
        handle = reply.body[0]
        if handle != path:  # an older portal: its own path
            self.pending[handle] = self.pending.pop(path, fut)
        code, results = await fut
        if code == 1:
            raise VideoError("screen sharing was refused")
        if code != 0:
            raise VideoError(f"screen sharing failed ({member})")
        return results

    async def open(self, restore_token: str = "") -> tuple[int, int, str]:
        from dbus_fast import Variant
        await self.connect()
        results = await self._request("CreateSession", "a{sv}", [],
                                      {"session_handle_token": Variant("s", f"hauslicht{os.getpid()}")})
        self.session = str(results.get("session_handle") or "")
        if not self.session:
            raise VideoError("the portal gave no session")
        options = {"types": Variant("u", MONITOR), "multiple": Variant("b", False),
                   "cursor_mode": Variant("u", CURSOR_HIDDEN), "persist_mode": Variant("u", PERSIST_UNTIL_REVOKED)}
        if restore_token:
            options["restore_token"] = Variant("s", restore_token)
        await self._request("SelectSources", "oa{sv}", [self.session], options)
        self.asking = True
        try:
            results = await self._request("Start", "osa{sv}", [self.session, ""], {})
        finally:
            self.asking = False
        streams = results.get("streams") or []
        if not streams:
            raise VideoError("the portal gave no stream")
        node = int(streams[0][0])
        reply = await self._call(PORTAL, PORTAL_PATH, SCREENCAST, "OpenPipeWireRemote", "oa{sv}", [self.session, {}])
        fds = list(reply.unix_fds or [])
        idx = reply.body[0] if reply.body else 0
        if not fds or not isinstance(idx, int) or idx >= len(fds):
            raise VideoError("the portal gave no PipeWire connection")
        fd = fds.pop(idx)
        for other in fds:
            with contextlib.suppress(OSError):
                os.close(other)
        return fd, node, str(results.get("restore_token") or "")

    def close(self) -> None:
        """Drop the connection: the portal closes the session with it."""
        bus, self.bus = self.bus, None
        for fut in self.pending.values():
            if not fut.done():
                fut.cancel()
        self.pending.clear()
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()


async def _store_call(member: str, args: list):
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    bus = await asyncio.wait_for(MessageBus().connect(), 3)
    try:
        reply = await asyncio.wait_for(bus.call(Message(
            destination=STORE, path=STORE_PATH, interface=STORE, member=member,
            signature="ss", body=args)), 3)
        return reply if reply is not None and reply.message_type != MessageType.ERROR else None
    finally:
        bus.disconnect()


async def token_usable(token: str) -> bool:
    """The permission store still holds the grant behind this restore token
    (asked without the portal: no dialog). Unreachable: not usable."""
    if not token:
        return False
    try:
        reply = await _store_call("Lookup", [STORE_TABLE, token])
    except Exception as exc:
        log.info("follow video: permission store: %s", exc)
        return False
    return reply is not None and bool(_unwrap(reply.body[0]))


async def forget_token(token: str) -> None:
    """Remove the grant from the permission store (best effort)."""
    if token:
        with contextlib.suppress(Exception):
            await _store_call("Delete", [STORE_TABLE, token])


# --------------------------------------------------------------------- capture

class VideoCapture:
    """gst-launch-1.0 reading the PipeWire stream (fd, node), writing FRAME
    bytes per frame to a pipe; on_frame(bytes, monotonic time) for each.
    Returns when stopped; raises VideoError when gst ends by itself."""

    def __init__(self, fd: int, node: int, on_frame: Callable) -> None:
        self.fd, self.node, self.on_frame = fd, node, on_frame
        self.proc: asyncio.subprocess.Process | None = None

    def command(self) -> list[str]:
        gst = shutil.which("gst-launch-1.0") or "gst-launch-1.0"
        return [gst, "-q",
                "pipewiresrc", f"fd={self.fd}", f"path={self.node}", "keepalive-time=500", "!",
                "videorate", "drop-only=true", f"max-rate={FPS}", "!",
                "videoscale", "method=bilinear2", "add-borders=false", "!",
                f"video/x-raw,width={W},height={H},pixel-aspect-ratio=1/1", "!",
                "videoconvert", "!", "video/x-raw,format=RGB", "!",
                "fdsink", "fd=1", "sync=false"]

    async def run(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, pass_fds=(self.fd,))
        proc = self.proc
        try:
            while True:
                try:
                    frame = await proc.stdout.readexactly(FRAME)
                except asyncio.IncompleteReadError:
                    break
                self.on_frame(frame, time.monotonic())
            err = b""
            with contextlib.suppress(Exception):
                err = await asyncio.wait_for(proc.stderr.read(), 2)
            code = await proc.wait()
            tail = err.decode(errors="replace").strip().splitlines()[-1:] or [""]
            raise VideoError(f"the screen capture ended (exit {code}){': ' + tail[0] if tail[0] else ''}")
        finally:
            self.kill()

    def kill(self) -> None:
        proc = self.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


# --------------------------------------------------------------------- service

class VideoFollowService:
    """The portal session, the capture and the analyser, running only while
    some bulb follows video. on_frame() after every frame; on_token(token)
    when the portal hands out a restore token; on_failure(message) once when
    the capture cannot start or ends by itself: the daemon turns the mode off
    (no retry)."""

    def __init__(self, on_frame: Callable[[], None], on_failure: Callable[[str], None],
                 on_token: Callable[[str], None]) -> None:
        self.on_frame, self.on_failure, self.on_token = on_frame, on_failure, on_token
        self.on = False
        self.task: asyncio.Task | None = None
        self.cast: ScreenCast | None = None
        self.capture: VideoCapture | None = None
        self.state = "off"  # off | starting | asking | capturing | error
        self.error = ""
        self.smooth = DEFAULT_SMOOTH
        self.analyser: Analyser | None = None
        self.started_at: float | None = None

    def apply(self, on: bool, token: str = "") -> None:
        """Start (with the restore token) or stop; nothing when nothing changed."""
        self.on = on
        if not on:
            self.stop()
            if self.state != "error":  # the failure stays readable until the next start
                self.state = "off"
            return
        if self.task is None or self.task.done():
            self.stop()
            self.state, self.error = "starting", ""
            self.task = asyncio.get_running_loop().create_task(self._run(token))

    def set_smooth(self, seconds: float) -> None:
        self.smooth = seconds
        if self.analyser is not None:
            self.analyser.smooth = seconds

    def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
        self._release()
        self.analyser = None

    def _release(self) -> None:
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.kill()
        cast, self.cast = self.cast, None
        if cast is not None:
            cast.close()

    def _fail(self, message: str) -> None:
        self.state, self.error = "error", message
        log.warning("follow video: %s; turned off", message)
        try:
            self.on_failure(message)
        except Exception as exc:  # never let it take the loop down
            log.warning("follow video: turning off failed: %s", exc)

    async def _run(self, token: str) -> None:
        lacking = missing()
        if lacking:
            self._fail(f"missing {', '.join(lacking)}: {install_hint()}")
            return
        fd = None
        opening: asyncio.Future | None = None
        try:
            self.cast = cast = ScreenCast()
            opening = asyncio.ensure_future(cast.open(token))
            while not opening.done():  # "asking" while the dialog may be up
                await asyncio.wait({opening}, timeout=0.2)
                if cast.asking and self.state == "starting":
                    self.state = "asking"
            fd, node, new_token = opening.result()
            if new_token and new_token != token:
                self.on_token(new_token)
            self.analyser = Analyser(self.smooth)
            self.state, self.started_at = "capturing", time.monotonic()
            log.info("follow video: capturing the screen (node %d)", node)
            self.capture = VideoCapture(fd, node, self._frame)
            run = asyncio.ensure_future(self.capture.run())
            closed = asyncio.ensure_future(cast.closed.wait())
            try:
                done, _pending = await asyncio.wait({run, closed}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (run, closed):
                    if not t.done():
                        t.cancel()
            if run in done:
                run.result()  # raises the capture's VideoError
            raise VideoError("screen sharing ended")
        except VideoError as exc:
            self._release()
            self._fail(str(exc))
        except OSError as exc:  # gst could not be started
            self._release()
            self._fail(f"{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            if opening is not None and not opening.done():
                opening.cancel()
            raise
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _frame(self, frame: bytes, now: float) -> None:
        if self.analyser is None:
            return
        self.analyser.feed(frame, now)
        try:
            self.on_frame()
        except Exception as exc:
            log.warning("follow video: %s", exc)

    def luminance(self, region: str) -> float | None:
        a = self.analyser
        return a.luminance(region) if a is not None and self.state == "capturing" else None

    def color(self, region: str) -> tuple[float, float, float] | None:
        a = self.analyser
        return a.color(region) if a is not None and self.state == "capturing" else None

    def describe(self) -> dict:
        a = self.analyser
        lacking = missing()
        fps = None
        if a is not None and self.started_at is not None and self.state == "capturing":
            fps = round(a.frames / max(1e-6, time.monotonic() - self.started_at), 1)
        return {"state": self.state, "error": self.error, "available": not lacking, "missing": lacking,
                "install": install_hint() if lacking else "", "frames": a.frames if a else 0, "fps": fps,
                "luminance": {r: round(a.luminance(r), 3) for r in REGIONS} if a and a.means else {}}
