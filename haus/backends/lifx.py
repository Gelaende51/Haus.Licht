"""LIFX bulbs over the LAN protocol (UDP 56700), in plain asyncio: the
protocol is small and aiolifx drags CLI packages into the runtime.

Brightness is 16-bit (SetColor keeps the bulb's hue, saturation and kelvin
from its last LightState); power is SetLightPower. LIFX asks for at most 20
messages per second per device: we send at most 10, each acknowledged.

Wall-switch flicks: a bulb's StateInfo carries its uptime. A watcher asks
every POLL_S; an uptime that went backwards is a reboot we did not notice
as an outage (a flick), and a bulb back after an outage with an uptime
shorter than the outage lost power (ctx.power_returned). No local power-on
setting exists: a LIFX bulb always comes up on, in its last colour.

Colour: GetVersion's product id, looked up in LIFX's products.json (the
colour flag and temperature range, below; tests check it against the
registry lifx-emulator generates from it). A colour product has `hs` and
`ct`, a white one with a range `ct`, a fixed white none; an unknown (newer)
product counts as colour, 1500-9000 K. Learned at add (`product`, `color`,
`kelvin`); an older config asks once on the first contact. The colour goes
in SetColor with the brightness the bulb has, so it keeps its level; power
is SetLightPower only, which a colour never sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import struct

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, Throttle, clamp, color_caps, kelvin_range, learn_color, tr

log = logging.getLogger("hauslicht")

PORT = 56700
BROADCAST = "255.255.255.255"
SEND_INTERVAL_S = 0.1
REPLY_TIMEOUT_S = 0.4
ATTEMPTS = 3
POLL_S = 2.0
DURATION_MS = 150

GET_SERVICE, STATE_SERVICE = 2, 3
GET_VERSION, STATE_VERSION = 32, 33
GET_INFO, STATE_INFO = 34, 35
ACK = 45
LIGHT_GET, SET_COLOR, LIGHT_STATE = 101, 102, 107
SET_LIGHT_POWER = 117
HEADER = struct.Struct("<HHI8s6sBBQHH")  # 36 bytes
DEFAULT_KELVIN = 3500  # SetColor's kelvin while a bulb's own is not known

# products.json (LIFX/products, vendor 1): lights without colour and their
# white range, and colour lights whose range is not 1500-9000 K.
WHITE_PRODUCTS = {
    **dict.fromkeys((113, 114, 125, 126, 131, 132), (1500, 9000)),
    **dict.fromkeys((85, 101), (2000, 2000)),
    **dict.fromkeys((82, 86, 100, 102), (2200, 2200)),
    **dict.fromkeys((67, 81, 96), (2200, 6500)),
    **dict.fromkeys((39, 50, 60), (2500, 9000)),
    **dict.fromkeys((12, 13, 51, 54, 61, 66, 87, 88, 127, 128, 133, 134), (2700, 2700)),
    **dict.fromkeys((10, 11, 18, 19), (2700, 6500)),
}
COLOR_RANGES = {
    **dict.fromkeys((1, 2, 3, 14, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 36, 37, 38,
                     40, 43, 44, 45, 46, 55, 56), (2500, 9000)),
    **dict.fromkeys((15, 191, 192, 207, 208, 211, 300), (2700, 6500)),
}
COLOR_RANGE = (1500, 9000)


def product_color(product: int) -> dict:
    """{color, kelvin} of a LIFX product id."""
    if product in WHITE_PRODUCTS:
        lo, hi = WHITE_PRODUCTS[product]
        return {"color": ["ct"], "kelvin": [lo, hi]} if lo < hi else {"color": []}
    lo, hi = COLOR_RANGES.get(product, COLOR_RANGE)
    return {"color": ["ct", "hs"], "kelvin": [lo, hi]}


def packet(source: int, seq: int, msg_type: int, payload: bytes = b"", target: bytes = bytes(8),
           ack: bool = False, res: bool = False) -> bytes:
    tagged = target == bytes(8)
    flags = (2 if ack else 0) | (1 if res else 0)
    proto = 1024 | 0x1000 | (0x2000 if tagged else 0)
    return HEADER.pack(HEADER.size + len(payload), proto, source, target, bytes(6), flags, seq, 0,
                       msg_type, 0) + payload


def parse(data: bytes) -> tuple | None:
    """(source, target, seq, type, payload) or None."""
    if len(data) < HEADER.size:
        return None
    size, _proto, source, target, _r, _flags, seq, _r2, msg_type, _r3 = HEADER.unpack_from(data)
    return source, target, seq, msg_type, data[HEADER.size:size]


class Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "LifxBackend") -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        got = parse(data)
        if got is None or got[0] != self.owner.source:
            return
        fut = self.owner.pending.get(got[2])
        if fut is not None and not fut.done() and got[3] in fut.want:
            fut.set_result(got)


class LifxBackend(Backend):
    kind = "lifx"
    title = "LIFX (Wi-Fi, local)"
    experimental = True
    requires = ()
    fields = (
        {"key": "host", "label": "IP address", "label_de": "IP-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.source = random.randint(2, 0xFFFFFFFF)
        self.seq = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.transport = None
        self.throttle = Throttle(SEND_INTERVAL_S)
        self.presence = Presence(ctx, self._probe)
        self.hsbk: dict[str, tuple] = {}     # bulb id -> (hue, sat, bri, kelvin) last known
        self.power: dict[str, bool] = {}     # bulb id -> last power we know of
        self.uptime: dict[str, float] = {}   # bulb id -> last uptime (s)
        self.asked: set[str] = set()         # old configs: product asked once
        self.watcher: asyncio.Task | None = None

    # ------------------------------------------------------------ transport

    async def _socket(self):
        if self.transport is None or self.transport.is_closing():
            loop = asyncio.get_running_loop()
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: Protocol(self), local_addr=("0.0.0.0", 0), allow_broadcast=True)
        return self.transport

    def _next_seq(self) -> int:
        for _ in range(256):
            self.seq = (self.seq + 1) & 0xFF
            if self.seq not in self.pending:
                return self.seq
        raise RuntimeError("no free sequence number")

    async def request(self, host: str, port: int, msg_type: int, payload: bytes = b"",
                      want: tuple = (ACK,), target: bytes = bytes(8), attempts: int = ATTEMPTS) -> tuple:
        transport = await self._socket()
        seq = self._next_seq()
        fut = asyncio.get_running_loop().create_future()
        fut.want = want
        self.pending[seq] = fut
        data = packet(self.source, seq, msg_type, payload, target,
                      ack=ACK in want, res=ACK not in want)
        try:
            for _ in range(attempts):
                transport.sendto(data, (host, port))
                with contextlib.suppress(TimeoutError):
                    return await asyncio.wait_for(asyncio.shield(fut), REPLY_TIMEOUT_S)
            raise ConnectionError(f"{host}: no answer to message {msg_type}")
        finally:
            self.pending.pop(seq, None)

    @staticmethod
    def addr(bulb) -> tuple[str, int]:
        return bulb.config["host"], int(bulb.config.get("port", PORT))

    @staticmethod
    def target(bulb) -> bytes:
        mac = bulb.config.get("mac")
        return bytes.fromhex(mac) + bytes(2) if mac else bytes(8)

    async def _ask(self, bulb, msg_type, payload=b"", want=(ACK,)) -> tuple:
        host, port = self.addr(bulb)
        return await self.request(host, port, msg_type, payload, want, self.target(bulb))

    async def light_state(self, bulb) -> dict:
        got = await self._ask(bulb, LIGHT_GET, want=(LIGHT_STATE,))
        h, s, b, k, _r, power = struct.unpack_from("<HHHHhH", got[4])
        label = got[4][12:44].split(b"\0", 1)[0].decode(errors="replace")
        self.hsbk[bulb.id] = (h, s, b, k)
        self.power[bulb.id] = power > 0
        return {"on": power > 0, "percent": round(b / 65535 * 100, 1), "label": label, "mac": got[1][:6].hex()}

    async def product_of(self, host: str, port: int, target: bytes = bytes(8)) -> int:
        got = await self.request(host, port, GET_VERSION, want=(STATE_VERSION,), target=target)
        _vendor, product = struct.unpack_from("<II", got[4])
        return product

    async def uptime_of(self, bulb) -> float:
        got = await self._ask(bulb, GET_INFO, want=(STATE_INFO,))
        _time, uptime, _down = struct.unpack_from("<QQQ", got[4])
        return uptime / 1e9

    # ------------------------------------------------------------ contract

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim", "flick"} | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, COLOR_RANGE)

    def step(self, bulb) -> float:
        return 100 / 65535  # 16-bit brightness

    def min_lit(self, bulb) -> float:
        return 1.0

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bool(online), "online": online}

    async def start(self) -> None:
        self.watcher = asyncio.get_running_loop().create_task(self._watch())

    async def stop(self) -> None:
        if self.watcher:
            self.watcher.cancel()
            with contextlib.suppress(BaseException):
                await self.watcher
        await self.presence.stop()
        if self.transport:
            self.transport.close()
            self.transport = None

    async def apply(self, bulb, percent: float) -> None:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        await self.throttle.wait(bulb.id)
        try:
            if percent <= 0:
                await self._ask(bulb, SET_LIGHT_POWER, struct.pack("<HI", 0, DURATION_MS))
                self.power[bulb.id] = False
            else:
                if bulb.id not in self.hsbk:
                    await self.light_state(bulb)
                if "color" not in bulb.config and bulb.id not in self.asked:
                    await self._learn(bulb)
                h, s, _b, k = self.hsbk[bulb.id]
                b = int(clamp(round(percent / 100 * 65535), 1, 65535))
                await self._ask(bulb, SET_COLOR, struct.pack("<BHHHHI", 0, h, s, b, k, DURATION_MS))
                self.hsbk[bulb.id] = (h, s, b, k)
                if not self.power.get(bulb.id):
                    await self._ask(bulb, SET_LIGHT_POWER, struct.pack("<HI", 65535, DURATION_MS))
                    self.power[bulb.id] = True
        except ConnectionError as exc:
            self.presence.lost(bulb, str(exc))
            raise Offline(str(exc)) from exc
        self.presence.ok(bulb)

    async def _learn(self, bulb) -> None:
        """An older config without `color`: ask the product once."""
        self.asked.add(bulb.id)
        host, port = self.addr(bulb)
        product = await self.product_of(host, port, self.target(bulb))
        bulb.config["product"] = product
        got = product_color(product)
        learn_color(self.ctx, bulb, got["color"], got.get("kelvin"))

    async def apply_color(self, bulb, color: dict) -> bool:
        """SetColor with the new hue/saturation (kelvin kept) or kelvin
        (saturation 0, hue kept), and the brightness the bulb has."""
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        caps = self.caps(bulb)
        try:
            if not self.power.get(bulb.id) or bulb.id not in self.hsbk:
                return False  # dark (or not known lit): a colour never switches it on
            h, s, b, k = self.hsbk[bulb.id]
            if color.get("mode") == "hs" and "hs" in caps:
                h = round((float(color["hue"]) % 360) / 360 * 65535) & 0xFFFF
                s = int(clamp(round(float(color["sat"]) / 100 * 65535), 0, 65535))
                k = k or DEFAULT_KELVIN
            elif color.get("mode") == "ct" and "ct" in caps:
                lo, hi = self.ct_range(bulb)
                s, k = 0, int(clamp(round(float(color["kelvin"])), lo, hi))
            else:
                return False
            if (h, s, b, k) == self.hsbk[bulb.id]:
                return True  # the bulb shows it already
            await self.throttle.wait(bulb.id)
            await self._ask(bulb, SET_COLOR, struct.pack("<BHHHHI", 0, h, s, b, k, DURATION_MS))
            self.hsbk[bulb.id] = (h, s, b, k)
        except ConnectionError as exc:
            self.presence.lost(bulb, str(exc))
            raise Offline(str(exc)) from exc
        self.presence.ok(bulb)
        return True

    async def _probe(self, bulb) -> dict | None:
        try:
            uptime = await self.uptime_of(bulb)
        except ConnectionError:
            return None
        self.uptime[bulb.id] = uptime
        self.hsbk.pop(bulb.id, None)  # a rebooted bulb: read colour and power afresh
        self.power.pop(bulb.id, None)
        return {"uptime": uptime}

    async def _watch(self) -> None:
        """Uptime of every reachable bulb every POLL_S: one that went backwards
        rebooted between two looks, too quickly to be seen gone: a flick."""
        while True:
            await asyncio.sleep(POLL_S)
            for bulb in list(self.ctx.bulbs()):
                if self.presence.get(bulb.id) is False:
                    continue  # the presence probe watches it
                try:
                    uptime = await self.uptime_of(bulb)
                except ConnectionError as exc:
                    self.presence.lost(bulb, str(exc))
                    continue
                except Exception as exc:  # never let one bulb end the watch
                    log.debug("lifx watch %s: %s", bulb.id, exc)
                    continue
                before = self.uptime.get(bulb.id)
                self.uptime[bulb.id] = uptime
                if before is not None and uptime + 1.0 < before:
                    log.info("%s: uptime %.0f s after %.0f s: rebooted, a flick", bulb.id, uptime, before)
                    self.hsbk.pop(bulb.id, None)
                    self.power.pop(bulb.id, None)
                    self.ctx.flick(bulb.id)
                self.presence.ok(bulb)

    async def query(self, bulb) -> dict | None:
        state = await self.light_state(bulb)
        return {"on": state["on"], "percent": state["percent"] if state["on"] else 0}

    async def discover(self, timeout: float) -> list[dict]:
        """GetService broadcast; each bulb answers StateService from its address."""
        transport = await self._socket()
        found: dict[str, dict] = {}
        seq = self._next_seq()
        fut = asyncio.get_running_loop().create_future()
        fut.want = ()
        self.pending[seq] = fut
        original = transport.get_protocol().datagram_received

        def seen(data, addr) -> None:
            got = parse(data)
            if got and got[0] == self.source and got[2] == seq and got[3] == STATE_SERVICE:
                service, port = struct.unpack_from("<BI", got[4])
                if service == 1:
                    mac = got[1][:6].hex()
                    found[mac] = {"kind": self.kind, "address": addr[0], "name": f"LIFX {mac}",
                                  "config": {"host": addr[0], "port": port, "mac": mac}}
            else:
                original(data, addr)

        transport.get_protocol().datagram_received = seen
        try:
            transport.sendto(packet(self.source, seq, GET_SERVICE, res=True), (BROADCAST, PORT))
            await asyncio.sleep(timeout)
        finally:
            transport.get_protocol().datagram_received = original
            self.pending.pop(seq, None)
        return list(found.values())

    async def add(self, config: dict) -> dict:
        host = str(config.get("host", "")).strip()
        port = int(config.get("port", PORT))
        try:
            got = await self.request(host, port, LIGHT_GET, want=(LIGHT_STATE,))
        except (ConnectionError, OSError) as exc:
            raise UserError(tr(f"no LIFX bulb answers at {host}", f"Unter {host} antwortet keine LIFX-Lampe")) from exc
        mac = got[1][:6].hex()
        config.update({"host": host, "mac": mac, "id": mac})
        try:
            product = await self.product_of(host, port, got[1])
        except ConnectionError:
            log.info("LIFX %s: no product id: brightness only until it answers", host)
        else:
            config["product"] = product
            config.update(product_color(product))
        if port != PORT:
            config["port"] = port
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        for d in (self.hsbk, self.power, self.uptime):
            d.pop(bulb.id, None)
        self.asked.discard(bulb.id)

    async def resume(self) -> None:
        self.uptime.clear()  # the sleep's gap says nothing about the bulbs
