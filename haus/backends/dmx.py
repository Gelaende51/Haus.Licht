"""DMX fixtures: one bulb per fixture, on Art-Net, sACN (E1.31), OLA, an
Enttec DMX USB Pro or an Enttec Open DMX (FTDI) dongle.

A fixture is a universe, a start address (1-512) and a channel layout, the
roles of its channels in order: `dimmer`, `r`, `g`, `b`, `w`, or `x=<0-255>`
for a constant channel (a mode or strobe channel that needs 0). Caps follow
the layout: dim always, hs with r, g and b. A white channel carries the
brightness when there is no RGB, else the white part of the colour.

Every fixture on the same output, target and universe writes into ONE
512-channel frame, sent by ONE sender: two fixtures never send frames that
overwrite each other's channels. Senders:
- artnet: ArtDmx (opcode 0x5000, protocol 14, sequence 1-255, Net/SubUni
  from the 15-bit port-address, length 512) over UDP 6454 to the host, or
  broadcast when none is given. Written here (18-byte header).
- sacn: E1.31 data packets from the `sacn` library's DataPacket (its own
  sender thread and socket are not used), multicast 239.255.hi.lo or
  unicast to the host, port 5568, a CID kept in hauslicht.json.
- ola: OLA daemon's HTTP API, POST /set_dmx u=<universe>&d=<v,v,...>.
- enttec-pro: 0x7E, label 6, length (513, LSB first), start code 0, 512
  channels, 0xE7. The widget repeats the frame on its own.
- opendmx: raw 250000 baud 8N2; the FTDI chip has no DMX buffer, so a thread
  streams break, mark-after-break, start code and 512 channels ~30 times a
  second while the fixture is driven.
Art-Net and sACN frames are sent again every KEEPALIVE_S while the kind is
driven, also at 0 % (sACN receivers drop a source after 2.5 s of silence,
Art-Net nodes may time out); idle(True) stops that and the Open DMX stream.
Daemon stop sends nothing: no blackout frame, no sACN stream-terminated.

Serial outputs: discover never opens a port (serial.py's DTR rule) and lists
FTDI ports only; Art-Net nodes are found by ArtPoll (read-only), OLA by
asking localhost:9090 for its universes. sACN has no discovery worth doing.
"""

from __future__ import annotations

import asyncio
import colorsys
import contextlib
import logging
import os
import re
import socket
import threading
import time
import uuid

from . import Backend, NeedsAccess, Offline
from ..common import UserError
from ._bulbkit import Presence, clamp, no_access, tr
from .serial import by_id_of, is_url

log = logging.getLogger("hauslicht")

OUTPUTS = ("artnet", "sacn", "ola", "enttec-pro", "opendmx")
SERIAL_OUTPUTS = ("enttec-pro", "opendmx")
ROLES = ("dimmer", "r", "g", "b", "w")
CHANNELS = 512

ARTNET_PORT = 6454
POLL_BIND_PORT = 6454   # ArtPollReply goes to the poller's port 6454 (tests: 0)
SACN_PORT = 5568
BROADCAST = "255.255.255.255"
OLA_HOST = "localhost:9090"
SOURCE_NAME = "Haus.Licht"
FTDI_VID = 0x0403

KEEPALIVE_S = 1.0
MIN_GAP_S = 1 / 40      # at most ~40 frames a second per universe (Art-Net nodes take ~44)
ENTTEC_BAUD = 57600     # the Pro's FTDI ignores it
OPENDMX_BAUD = 250000
OPENDMX_HZ = 30
BREAK_S = 0.0001        # >= 88 us
MAB_S = 0.000012        # >= 8 us; sleep() gives more, which is allowed
HTTP_TIMEOUT_S = 3.0


# ------------------------------------------------------------------ layouts

def parse_layout(text: str) -> list[tuple[str, int | None]]:
    """`dimmer,r,g,b,w` / `r,g,b,x=0` -> [(role, constant)]; UserError on anything else."""
    out: list[tuple[str, int | None]] = []
    for raw in str(text or "").split(","):
        item = raw.strip().lower().replace(" ", "")
        if not item:
            continue
        if item.startswith("x="):
            try:
                value = int(item[2:])
            except ValueError:
                value = -1
            if not 0 <= value <= 255:
                raise UserError(tr(f"channel layout: {raw.strip()}: a constant is x=0 to x=255",
                                   f"Kanalbelegung: {raw.strip()}: eine Konstante ist x=0 bis x=255"))
            out.append(("x", value))
        elif item in ROLES:
            if any(r == item for r, _ in out):
                raise UserError(tr(f"channel layout: {item} twice", f"Kanalbelegung: {item} doppelt"))
            out.append((item, None))
        else:
            raise UserError(tr(f"channel layout: unknown role {raw.strip()!r} (known: dimmer, r, g, b, w, x=0-255)",
                               f"Kanalbelegung: unbekannte Rolle {raw.strip()!r} (bekannt: dimmer, r, g, b, w, x=0–255)"))
    if not out:
        raise UserError(tr("channel layout is empty (e.g. dimmer,r,g,b)", "Kanalbelegung ist leer (z. B. dimmer,r,g,b)"))
    roles = {r for r, _ in out}
    if roles & {"r", "g", "b"} and not {"r", "g", "b"} <= roles:
        raise UserError(tr("channel layout: r, g and b go together", "Kanalbelegung: r, g und b nur zusammen"))
    if not roles & {"dimmer", "r", "w"}:
        raise UserError(tr("channel layout: no channel for the brightness (dimmer, r,g,b or w)",
                           "Kanalbelegung: kein Kanal für die Helligkeit (dimmer, r,g,b oder w)"))
    return out


def fixture_values(layout: list[tuple[str, int | None]], percent: float,
                   rgb: tuple[float, float, float] | None) -> list[int]:
    """The fixture's channel values for a level and a 0-1 colour (None: white)."""
    roles = {r for r, _ in layout}
    level = clamp(percent, 0.0, 100.0) / 100.0
    if {"r", "g", "b"} <= roles:
        r, g, b = rgb or (1.0, 1.0, 1.0)
        w = min(r, g, b) if "w" in roles else 0.0
        r, g, b = r - w, g - w, b - w
    else:
        r = g = b = 0.0
        w = 1.0
    scale = 1.0 if "dimmer" in roles else level
    colour = {"r": r, "g": g, "b": b, "w": w}
    out = []
    for role, const in layout:
        if role == "x":
            value = const
        elif role == "dimmer":
            value = level * 255
        else:
            value = colour[role] * scale * 255
        out.append(int(clamp(round(value), 0, 255)))
    return out


# ------------------------------------------------------------------ wire formats

def artdmx(universe: int, sequence: int, frame: bytes) -> bytes:
    data = bytes(frame)
    if len(data) % 2:
        data += b"\x00"
    return (b"Art-Net\x00" + (0x5000).to_bytes(2, "little") + (14).to_bytes(2, "big")
            + bytes((sequence & 0xFF, 0, universe & 0xFF, (universe >> 8) & 0x7F))
            + len(data).to_bytes(2, "big") + data)


ARTPOLL = b"Art-Net\x00" + (0x2000).to_bytes(2, "little") + (14).to_bytes(2, "big") + b"\x00\x00"


def parse_artpollreply(data: bytes) -> dict | None:
    """{ip, name, universe} of an ArtPollReply, None for anything else."""
    if len(data) < 108 or data[:8] != b"Art-Net\x00" or int.from_bytes(data[8:10], "little") != 0x2100:
        return None
    ip = ".".join(str(b) for b in data[10:14])
    short = data[26:44].split(b"\x00", 1)[0].decode("latin-1").strip()
    long = data[44:108].split(b"\x00", 1)[0].decode("latin-1").strip()
    universe = 0
    if len(data) >= 194:
        universe = ((data[18] & 0x7F) << 8) | ((data[19] & 0x0F) << 4) | (data[190] & 0x0F)
    return {"ip": ip, "name": long or short or ip, "universe": universe}


def sacn_packet(cid: bytes, universe: int, sequence: int, priority: int, frame: bytes) -> bytes:
    from sacn.messages.data_packet import DataPacket
    packet = DataPacket(cid=tuple(cid), sourceName=SOURCE_NAME, universe=universe, dmxData=tuple(frame),
                        priority=priority, sequence=sequence & 0xFF)
    return bytes(packet.getBytes())


def sacn_multicast(universe: int) -> str:
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def enttec_pro_frame(frame: bytes) -> bytes:
    n = len(frame) + 1
    return bytes((0x7E, 6, n & 0xFF, (n >> 8) & 0xFF, 0)) + bytes(frame) + b"\xE7"


def _split_host(host: str, port: int) -> tuple[str, int]:
    h, sep, p = host.rpartition(":")
    if sep and p.isdigit() and "]" not in p:
        return h.strip("[]"), int(p)
    return host.strip("[]"), port


def _ports() -> list:
    """FTDI USB serial ports as pyserial lists them (sysfs only: nothing is opened)."""
    from serial.tools import list_ports
    return [p for p in list_ports.comports() if p.vid == FTDI_VID]


def _open_port(port: str, output: str):
    import serial
    if output == "opendmx":
        return serial.serial_for_url(port, baudrate=OPENDMX_BAUD, bytesize=8, parity="N", stopbits=2,
                                     timeout=0, write_timeout=1)
    return serial.serial_for_url(port, baudrate=ENTTEC_BAUD, timeout=0, write_timeout=1)


# ------------------------------------------------------------------ senders

class UdpSender:
    keepalive = True

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setblocking(False)
        self.seq = 0

    def close(self) -> None:
        self.sock.close()


class ArtNetSender(UdpSender):
    def __init__(self, host: str) -> None:
        super().__init__()
        self.dest = _split_host(host, ARTNET_PORT) if host else (BROADCAST, ARTNET_PORT)

    async def send(self, universe: int, frame: bytes, priority: int) -> None:
        self.seq = self.seq % 255 + 1   # 0 would mean "no sequencing"
        self.sock.sendto(artdmx(universe, self.seq, frame), self.dest)


class SacnSender(UdpSender):
    def __init__(self, host: str, cid: bytes) -> None:
        super().__init__()
        self.host, self.cid = host, cid
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    async def send(self, universe: int, frame: bytes, priority: int) -> None:
        dest = _split_host(self.host, SACN_PORT) if self.host else (sacn_multicast(universe), SACN_PORT)
        self.sock.sendto(sacn_packet(self.cid, universe, self.seq, priority, frame), dest)
        self.seq = (self.seq + 1) & 0xFF


class OlaSender:
    keepalive = False  # olad keeps sending its last frame

    def __init__(self, host: str) -> None:
        self.host = host or OLA_HOST
        self.session = None

    async def send(self, universe: int, frame: bytes, priority: int) -> None:
        import aiohttp
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S))
        try:
            async with self.session.post(f"http://{self.host}/set_dmx",
                                         data={"u": str(universe), "d": ",".join(map(str, frame))}) as r:
                if r.status >= 400:
                    raise OSError(f"OLA {self.host}: HTTP {r.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OSError(f"OLA {self.host}: {exc}") from exc

    def close(self) -> None:
        if self.session is not None and not self.session.closed:
            asyncio.get_running_loop().create_task(self.session.close())
        self.session = None


class EnttecProSender:
    keepalive = False  # the widget repeats the frame by itself

    def __init__(self, port: str) -> None:
        self.link = _open_port(port, "enttec-pro")

    async def send(self, universe: int, frame: bytes, priority: int) -> None:
        self.link.write(enttec_pro_frame(frame))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.link.close()


class OpenDmxSender:
    """Streams the frame from a thread: break, mark after break, start code 0,
    512 channels, ~OPENDMX_HZ times a second. send() only hands it a frame."""

    keepalive = False

    def __init__(self, port: str, on_error=None) -> None:
        self.link = _open_port(port, "opendmx")
        self.frame = bytes(CHANNELS)
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None
        self.on_error = on_error

    async def send(self, universe: int, frame: bytes, priority: int) -> None:
        if self.error is not None:
            raise OSError(str(self.error))
        with self.lock:
            self.frame = bytes(frame)
        if self.thread is None or not self.thread.is_alive():
            self.halt.clear()
            self.thread = threading.Thread(target=self._run, name="hauslicht-opendmx", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        period = 1.0 / OPENDMX_HZ
        while not self.halt.is_set():
            started = time.monotonic()
            with self.lock:
                data = b"\x00" + self.frame
            try:
                self.link.break_condition = True
                time.sleep(BREAK_S)
                self.link.break_condition = False
                time.sleep(MAB_S)
                self.link.write(data)
                self.link.flush()  # drained: the next break must not cut this frame
            except Exception as exc:  # noqa: BLE001 - unplugged: SerialException is an OSError
                self.error = exc
                if self.on_error is not None:
                    self.on_error(exc)
                return
            self.halt.wait(max(0.0, period - (time.monotonic() - started)))

    def pause(self) -> None:
        """Stop streaming (idle); the port stays open."""
        self.halt.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        self.thread = None

    def close(self) -> None:
        self.pause()
        with contextlib.suppress(Exception):
            self.link.close()


class Universe:
    """One frame of 512 channels and its sender, shared by every fixture on it."""

    def __init__(self, key: tuple, output: str, target: str, number: int) -> None:
        self.key, self.output, self.target, self.number = key, output, target, number
        self.frame = bytearray(CHANNELS)
        self.fixtures: dict[str, object] = {}  # bulb id -> bulb
        self.sender = None
        self.last = 0.0
        self.keep: asyncio.Task | None = None


# ------------------------------------------------------------------ backend

class DmxBackend(Backend):
    kind = "dmx"
    title = "DMX fixtures (Art-Net, sACN, OLA, Enttec USB)"
    product = "DMX fixture"
    experimental = True
    requires = ("sacn", "pyserial", "aiohttp")
    modules = ("sacn", "serial", "aiohttp")
    autoconnect = False  # a found node or dongle says nothing about the fixtures' addresses
    fields = (
        {"key": "output", "label": "Output", "label_de": "Ausgang",
         "type": "choice", "choices": list(OUTPUTS), "default": "artnet"},
        {"key": "host", "label": "Host (Art-Net node, else broadcast; sACN unicast, else multicast; OLA)",
         "label_de": "Host (Art-Net-Knoten, sonst Broadcast; sACN-Unicast, sonst Multicast; OLA)",
         "type": "text", "required": False, "default": ""},
        {"key": "port", "label": "Serial port (Enttec)", "label_de": "Serielle Schnittstelle (Enttec)",
         "type": "text", "required": False, "default": ""},
        {"key": "universe", "label": "Universe (Art-Net 0-32767, sACN 1-63999)",
         "label_de": "Universum (Art-Net 0–32767, sACN 1–63999)", "type": "int", "required": False},
        {"key": "address", "label": "Start address (1-512)", "label_de": "Startadresse (1–512)",
         "type": "int", "default": 1},
        {"key": "layout", "label": "Channels in order (dimmer, r, g, b, w, x=0-255)",
         "label_de": "Kanäle der Reihe nach (dimmer, r, g, b, w, x=0–255)",
         "type": "text", "required": False, "default": "dimmer"},
        {"key": "priority", "label": "sACN priority (0-200)", "label_de": "sACN-Priorität (0–200)",
         "type": "int", "default": 100},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.universes: dict[tuple, Universe] = {}
        self.level: dict[str, float] = {}
        self.rgb: dict[str, tuple[float, float, float]] = {}
        self.presence = Presence(ctx, self._probe)
        self.idle_now = False
        self._cid: bytes | None = None

    # ------------------------------------------------------------ config
    @staticmethod
    def _key(cfg: dict) -> tuple[str, str, int]:
        output = cfg.get("output", "artnet")
        if output in SERIAL_OUTPUTS:
            return (output, str(cfg.get("port", "")), 0)
        target = str(cfg.get("host") or "")
        if output == "ola":
            target = target or OLA_HOST
        return (output, target, int(cfg.get("universe", 0)))

    def _layout(self, bulb) -> list[tuple[str, int | None]]:
        return parse_layout(bulb.config.get("layout", "dimmer"))

    def _span(self, cfg: dict) -> range:
        start = int(cfg.get("address", 1))
        return range(start, start + len(parse_layout(cfg.get("layout", "dimmer"))))

    def _universe(self, bulb) -> Universe:
        key = self._key(bulb.config)
        u = self.universes.get(key)
        if u is None:
            u = self.universes[key] = Universe(key, *key)
        u.fixtures[bulb.id] = bulb
        return u

    def cid(self) -> bytes:
        """sACN component id: one per installation, kept in hauslicht.json."""
        if self._cid is None:
            state = getattr(self.ctx, "state", None)
            store = state.setdefault("dmx", {}) if isinstance(state, dict) else {}
            try:
                self._cid = uuid.UUID(store["cid"]).bytes
            except (KeyError, ValueError, TypeError, AttributeError):
                new = uuid.uuid4()
                store["cid"] = str(new)
                self._cid = new.bytes
                with contextlib.suppress(Exception):
                    self.ctx.save()
        return self._cid

    # ------------------------------------------------------------ contract
    def caps(self, bulb) -> set[str]:
        try:
            roles = {r for r, _ in self._layout(bulb)}
        except UserError:
            roles = set()
        return {"onoff", "dim", "hs"} if {"r", "g", "b"} <= roles else {"onoff", "dim"}

    def step(self, bulb) -> float:
        return 100.0 / 255

    def details(self, bulb) -> dict:
        cfg = bulb.config
        output, target, number = self._key(cfg)
        span = self._span(cfg)
        return {"model": f"{self.product} ({output})", "address": target or BROADCAST,
                "universe": number if output not in SERIAL_OUTPUTS else None,
                "channels": f"{span.start}-{span.stop - 1}", "layout": cfg.get("layout")}

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        u = self.universes.get(self._key(bulb.config))
        return {"connected": bool(u is not None and u.sender is not None), "online": online}

    # ------------------------------------------------------------ sending
    def _sender(self, u: Universe):
        if u.sender is None:
            if u.output == "artnet":
                u.sender = ArtNetSender(u.target)
            elif u.output == "sacn":
                u.sender = SacnSender(u.target, self.cid())
            elif u.output == "ola":
                u.sender = OlaSender(u.target)
            else:
                port = u.target
                if not is_url(port) and not os.path.exists(port):
                    raise Offline(f"{port} is gone")
                if not is_url(port) and not os.access(port, os.R_OK | os.W_OK):
                    raise PermissionError(no_access(port))
                if u.output == "enttec-pro":
                    u.sender = EnttecProSender(port)
                else:
                    loop = asyncio.get_running_loop()
                    u.sender = OpenDmxSender(port, lambda exc, u=u: loop.call_soon_threadsafe(self._failed, u, exc))
        return u.sender

    def _priority(self, u: Universe) -> int:
        return max((int(b.config.get("priority", 100)) for b in u.fixtures.values()), default=100)

    def _failed(self, u: Universe, exc: Exception) -> None:
        if u.sender is not None:
            with contextlib.suppress(Exception):
                u.sender.close()
            u.sender = None
        for b in list(u.fixtures.values()):
            self.presence.lost(b, str(exc))

    async def _transmit(self, u: Universe) -> None:
        try:
            sender = self._sender(u)
            await sender.send(u.number, bytes(u.frame), self._priority(u))
        except Offline:
            self._failed(u, Exception("gone"))
            raise
        except OSError as exc:  # unreachable network, unplugged dongle, OLA down
            self._failed(u, exc)
            raise Offline(f"dmx {u.output} {u.target}: {exc}") from exc
        u.last = time.monotonic()
        for b in list(u.fixtures.values()):
            self.presence.ok(b)

    async def _keepalive(self, u: Universe) -> None:
        try:
            while not self.idle_now and u.fixtures:
                await asyncio.sleep(KEEPALIVE_S)
                if self.idle_now or u.sender is None:
                    break
                if time.monotonic() - u.last >= KEEPALIVE_S * 0.9:
                    await self._transmit(u)
        except Offline:
            pass
        finally:
            if u.keep is asyncio.current_task():
                u.keep = None

    async def _send(self, bulb) -> None:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        u = self._universe(bulb)
        values = fixture_values(self._layout(bulb), self.level.get(bulb.id, 0.0), self.rgb.get(bulb.id))
        start = int(bulb.config.get("address", 1)) - 1
        u.frame[start:start + len(values)] = bytes(values)
        gap = u.last + MIN_GAP_S - time.monotonic()
        if gap > 0:
            await asyncio.sleep(gap)
        await self._transmit(u)
        if u.sender is not None and u.sender.keepalive and u.keep is None and not self.idle_now:
            u.keep = asyncio.get_running_loop().create_task(self._keepalive(u))

    async def _probe(self, bulb) -> dict | None:
        """Is the output there? Sends no DMX: a route check, an OLA read, the port node."""
        output, target, _ = self._key(bulb.config)
        try:
            if output in SERIAL_OUTPUTS:
                return {} if is_url(target) or os.access(target, os.R_OK | os.W_OK) else None
            if output == "ola":
                return {} if await self._ola_universes(target) is not None else None
            if output == "artnet":
                dest = _split_host(target, ARTNET_PORT) if target else (BROADCAST, ARTNET_PORT)
            else:
                dest = _split_host(target, SACN_PORT) if target else (sacn_multicast(int(bulb.config.get("universe", 1))), SACN_PORT)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.connect(dest)  # UDP connect: a route lookup, no packet
            return {}
        except OSError:
            return None

    # ------------------------------------------------------------ the API
    async def apply(self, bulb, percent: float) -> None:
        self.level[bulb.id] = percent
        await self._send(bulb)

    async def apply_color(self, bulb, color: dict) -> bool:
        if color.get("mode") != "hs" or "hs" not in self.caps(bulb):
            return False
        self.rgb[bulb.id] = colorsys.hsv_to_rgb((float(color["hue"]) % 360) / 360.0,
                                                clamp(float(color["sat"]) / 100.0, 0.0, 1.0), 1.0)
        if self.level.get(bulb.id, 0.0) > 0:
            await self._send(bulb)
        return True

    async def idle(self, idle: bool) -> None:
        self.idle_now = idle
        if idle:  # nothing driven: no resend, no Open DMX stream (what the fixtures show stays)
            for u in self.universes.values():
                if u.keep is not None:
                    u.keep.cancel()
                    u.keep = None
                if isinstance(u.sender, OpenDmxSender):
                    await asyncio.get_running_loop().run_in_executor(None, u.sender.pause)

    async def suspend(self) -> None:
        await self.idle(True)

    async def resume(self) -> None:
        for u in self.universes.values():  # fresh sockets and ports after the sleep
            if u.sender is not None:
                u.sender.close()
                u.sender = None
        self.idle_now = False

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self.level.pop(bulb.id, None)
        self.rgb.pop(bulb.id, None)
        key = self._key(bulb.config)
        u = self.universes.get(key)
        if u is None:
            return
        u.fixtures.pop(bulb.id, None)  # its channels keep their last value: nothing switches
        if not u.fixtures:
            if u.keep is not None:
                u.keep.cancel()
            if u.sender is not None:
                u.sender.close()
            del self.universes[key]

    async def stop(self) -> None:  # no blackout, no stream-terminated: the fixtures keep their look
        await self.presence.stop()
        for u in self.universes.values():
            if u.keep is not None:
                u.keep.cancel()
                u.keep = None
            if u.sender is not None:
                if isinstance(u.sender, OpenDmxSender):
                    await asyncio.get_running_loop().run_in_executor(None, u.sender.close)
                else:
                    u.sender.close()
                u.sender = None

    # ------------------------------------------------------------ discover / add
    async def _ola_universes(self, host: str | None = None) -> list[dict] | None:
        import aiohttp
        host = host or OLA_HOST
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as s:
                async with s.get(f"http://{host}/json/universe_plugin_list") as r:
                    if r.status >= 400:
                        return None
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
        return list((data or {}).get("universes") or []) if isinstance(data, dict) else []

    async def _artpoll(self, timeout: float) -> list[dict]:
        """ArtPoll broadcast (read-only): the nodes that answer within timeout."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setblocking(False)
            sock.bind(("", POLL_BIND_PORT))
            await loop.sock_sendto(sock, ARTPOLL, (BROADCAST, ARTNET_PORT))
        except OSError as exc:  # port taken by a lighting program, no network
            log.info("ArtPoll: %s", exc)
            sock.close()
            return []
        nodes: dict[tuple[str, int], dict] = {}
        deadline = loop.time() + timeout
        try:
            while (left := deadline - loop.time()) > 0:
                try:
                    data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), left)
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if (node := parse_artpollreply(data)) is not None:
                    nodes.setdefault((node["ip"], node["universe"]), node)
        finally:
            sock.close()
        return [{"kind": self.kind, "address": f"artnet/{n['ip']}/{n['universe']}",
                 "name": f"{n['name']} (Art-Net {n['ip']}, universe {n['universe']})",
                 "config": {"output": "artnet", "host": n["ip"], "universe": n["universe"],
                            "address": 1, "layout": "dimmer"}} for n in nodes.values()]

    async def _ola_found(self) -> list[dict]:
        universes = await self._ola_universes()
        if universes is None:
            return []
        if not universes:
            universes = [{"id": 1, "name": ""}]
        return [{"kind": self.kind, "address": f"ola/{u.get('id')}",
                 "name": f"OLA {u.get('name') or ''} (universe {u.get('id')})".replace("OLA  ", "OLA "),
                 "config": {"output": "ola", "host": OLA_HOST, "universe": int(u.get("id", 1)),
                            "address": 1, "layout": "dimmer"}} for u in universes]

    async def discover(self, timeout: float) -> list[dict]:
        found: list[dict] = []
        for part in await asyncio.gather(self._artpoll(min(timeout, 3.0)), self._ola_found(),
                                         return_exceptions=True):
            if isinstance(part, list):
                found += part
            else:
                log.info("dmx discover: %s", part)
        blocked = None
        for p in _ports():  # listed, never opened
            address = by_id_of(p.device) or p.device
            desc = p.description if p.description and p.description != "n/a" else p.device
            output = "enttec-pro" if "pro" in f"{p.product or ''} {desc}".lower() else "opendmx"
            entry = {"kind": self.kind, "address": address, "name": f"{desc} ({output})",
                     "config": {"output": output, "port": address, "address": 1, "layout": "dimmer"}}
            if not os.access(p.device, os.R_OK | os.W_OK):
                entry["access"] = False
                blocked = blocked or p.device
            found.append(entry)
        if blocked:
            raise NeedsAccess(no_access(blocked), found)
        return found

    async def add(self, config: dict) -> dict:
        output = config.get("output") or "artnet"
        if output not in OUTPUTS:
            raise UserError(tr(f"output: one of {', '.join(OUTPUTS)}", f"Ausgang: einer von {', '.join(OUTPUTS)}"))
        layout = parse_layout(config.get("layout") or "dimmer")
        start = int(config.get("address") if config.get("address") not in (None, "") else 1)
        if not 1 <= start <= CHANNELS:
            raise UserError(tr("start address: 1 to 512", "Startadresse: 1 bis 512"))
        if start + len(layout) - 1 > CHANNELS:
            raise UserError(tr(f"{len(layout)} channels from {start} go past 512",
                               f"{len(layout)} Kanäle ab {start} gehen über 512 hinaus"))
        out = {"output": output, "address": start, "layout": ",".join(r if c is None else f"x={c}" for r, c in layout)}
        host = str(config.get("host") or "").strip()
        if output in SERIAL_OUTPUTS:
            port = str(config.get("port") or "").strip()
            if not is_url(port):
                if not port or not os.path.exists(port):
                    raise UserError(tr(f"no serial port {port}", f"Keine serielle Schnittstelle {port}"))
                port = by_id_of(port) or port
                if not os.access(port, os.R_OK | os.W_OK):
                    raise NeedsAccess(no_access(os.path.realpath(port)))
            out.update({"port": port, "universe": 0})
        else:
            universe = config.get("universe")
            universe = (0 if output == "artnet" else 1) if universe in (None, "") else int(universe)
            limits = {"artnet": (0, 32767), "sacn": (1, 63999), "ola": (0, 2**31 - 1)}[output]
            if not limits[0] <= universe <= limits[1]:
                raise UserError(tr(f"universe for {output}: {limits[0]} to {limits[1]}",
                                   f"Universum für {output}: {limits[0]} bis {limits[1]}"))
            out.update({"host": host, "universe": universe})
            if output == "sacn":
                priority = int(config.get("priority") if config.get("priority") not in (None, "") else 100)
                if not 0 <= priority <= 200:
                    raise UserError(tr("sACN priority: 0 to 200", "sACN-Priorität: 0 bis 200"))
                out["priority"] = priority
            if output == "ola":
                out["host"] = host or OLA_HOST
                if await self._ola_universes(out["host"]) is None:
                    raise UserError(tr(f"OLA does not answer at {out['host']} (is olad running?)",
                                       f"OLA antwortet nicht unter {out['host']} (läuft olad?)"))
        key, span = self._key(out), self._span(out)
        for b in self.ctx.bulbs():
            if self._key(b.config) == key:
                other = self._span(b.config)
                if span.start < other.stop and other.start < span.stop:
                    raise UserError(tr(f"channels {span.start}-{span.stop - 1} overlap {b.name}",
                                       f"Kanäle {span.start}–{span.stop - 1} überschneiden sich mit {b.name}"))
        # the node or dongle too: two of them may use the same universe and addresses
        target = "" if output == "ola" and key[1] == OLA_HOST else key[1].rsplit("/", 1)[-1]
        out["id"] = re.sub(r"[^A-Za-z0-9_.-]", "_", "-".join(
            p for p in (output, target, str(out["universe"]), str(start)) if p))
        return out
