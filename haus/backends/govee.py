"""Govee lights over the Govee LAN API, in plain asyncio UDP. Experimental.

Commands go to the device's port 4003; it answers `scan` and `devStatus` on
port 4002 of the sender's host (fixed by the protocol, so only one program
on this computer can listen for Govee answers). Fire and forget: after a
change we ask devStatus, re-send once if the device shows something else,
and take silence as unreachable. "LAN Control" must be switched on per
device in the Govee Home app (supported models only). No power-on setting
over LAN.

Colour: devStatus shows `color` ({r, g, b}: `hs`) and `colorTemInKelvin`
(`ct`, the LAN API's 2000-9000 K) for what the device has; learned from the
first devStatus after a change (`color`, `kelvin`), which apply asks anyway.
`colorwc` sets both: hue/saturation as RGB at full value with kelvin 0, a
white as its kelvin (and the blackbody RGB of it). It carries no brightness
and no power; it goes out only while the device is lit, is checked with
devStatus like a level, and an unchanged colour is not sent again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, Throttle, clamp, color_caps, hs_rgb, kelvin_range, learn_color, mac_suffix, tr

log = logging.getLogger("hauslicht")

MULTICAST = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002
COMMAND_PORT = 4003
SEND_INTERVAL_S = 0.2
STATUS_TIMEOUT_S = 1.0
ATTEMPTS = 2
CT_RANGE = (2000, 9000)  # colorTemInKelvin (Govee LAN API)


def status_color(got: dict) -> dict:
    """{color, kelvin} from what devStatus shows."""
    caps = (["ct"] if "colorTemInKelvin" in got else []) + (["hs"] if isinstance(got.get("color"), dict) else [])
    return {"color": caps, **({"kelvin": list(CT_RANGE)} if "ct" in caps else {})}


def message(cmd: str, data: dict | None = None) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data or {}}}).encode()


class Listener(asyncio.DatagramProtocol):
    def __init__(self, owner: "GoveeBackend") -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data)["msg"]
        except (ValueError, KeyError, TypeError):
            return
        self.owner.received(addr[0], msg.get("cmd"), msg.get("data") or {})


class GoveeBackend(Backend):
    kind = "govee"
    title = "Govee (Wi-Fi, LAN API)"
    experimental = True
    requires = ()
    fields = (
        {"key": "host", "label": "IP address", "label_de": "IP-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.transport = None
        self.waiting: dict[tuple[str, str], list[asyncio.Future]] = {}  # (ip, cmd) -> futures
        self.scan_found: dict[str, dict] | None = None
        self.throttle = Throttle(SEND_INTERVAL_S)
        self.presence = Presence(ctx, self._probe)
        self.on: dict[str, bool] = {}
        self.colors: dict[str, dict] = {}   # bulb id -> colorwc data last confirmed

    # ------------------------------------------------------------ transport

    async def _socket(self):
        if self.transport is None or self.transport.is_closing():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            try:
                sock.bind(("", LISTEN_PORT))
            except OSError as exc:
                sock.close()
                raise ConnectionError(f"port {LISTEN_PORT} busy (another Govee program?): {exc}") from exc
            self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Listener(self), sock=sock)
        return self.transport

    def received(self, ip: str, cmd: str, data: dict) -> None:
        if cmd == "scan" and self.scan_found is not None and data.get("ip"):
            dev = str(data.get("device", ""))
            self.scan_found[data["ip"]] = {
                "kind": self.kind, "address": data["ip"],
                "name": f"Govee {data.get('sku', '')} {dev[-5:]}".strip(),
                "config": {"host": data["ip"], "device": dev, "sku": data.get("sku", "")}}
        for fut in self.waiting.pop((ip, cmd), []):
            if not fut.done():
                fut.set_result(data)

    async def ask(self, host: str, cmd: str, port: int, data: dict | None = None,
                  timeout: float = STATUS_TIMEOUT_S) -> dict:
        transport = await self._socket()
        for _ in range(ATTEMPTS):
            fut = asyncio.get_running_loop().create_future()
            self.waiting.setdefault((host, cmd), []).append(fut)
            transport.sendto(message(cmd, data), (host, port))
            try:
                return await asyncio.wait_for(fut, timeout / ATTEMPTS)
            except TimeoutError:
                with contextlib.suppress(ValueError, KeyError):
                    self.waiting[(host, cmd)].remove(fut)
        raise ConnectionError(f"{host}: no answer to {cmd}")

    async def send(self, bulb, cmd: str, data: dict) -> None:
        transport = await self._socket()
        transport.sendto(message(cmd, data), (bulb.config["host"], self.cmd_port(bulb)))

    @staticmethod
    def cmd_port(bulb) -> int:
        return int(bulb.config.get("port", COMMAND_PORT))

    async def dev_status(self, bulb) -> dict:
        return await self.ask(bulb.config["host"], "devStatus", self.cmd_port(bulb))

    # ------------------------------------------------------------ contract

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, CT_RANGE)

    def min_lit(self, bulb) -> float:
        return 1.0

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bool(online), "online": online}

    async def _probe(self, bulb) -> dict | None:
        try:
            await self.dev_status(bulb)
            return {}
        except ConnectionError:
            return None

    async def _push(self, bulb, percent: float) -> tuple[int, int]:
        if percent <= 0:
            await self.send(bulb, "turn", {"value": 0})
            return 0, 0
        level = int(clamp(round(percent), 1, 100))
        if not self.on.get(bulb.id):
            await self.send(bulb, "turn", {"value": 1})
        await self.send(bulb, "brightness", {"value": level})
        return 1, level

    async def apply(self, bulb, percent: float) -> None:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        await self.throttle.wait(bulb.id)
        try:
            want_on, level = await self._push(bulb, percent)
            got = await self.dev_status(bulb)
            if got.get("onOff") != want_on or (want_on and got.get("brightness") != level):
                log.debug("%s: shows %s, sent %s/%s: once more", bulb.id, got, want_on, level)
                self.on.pop(bulb.id, None)
                await self._push(bulb, percent)
            self.on[bulb.id] = bool(want_on)
            if not want_on:
                self.colors.pop(bulb.id, None)
            if "color" not in bulb.config:  # learned from what the device shows
                learned = status_color(got)
                learn_color(self.ctx, bulb, learned["color"], learned.get("kelvin"))
        except ConnectionError as exc:
            self._lost(bulb, exc)
        self.presence.ok(bulb)

    def _lost(self, bulb, exc: Exception):
        self.on.pop(bulb.id, None)
        self.colors.pop(bulb.id, None)
        self.presence.lost(bulb, str(exc))
        raise Offline(str(exc)) from exc

    async def apply_color(self, bulb, color: dict) -> bool:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        if not self.on.get(bulb.id):
            return False  # dark or not known lit: leave it alone
        caps = self.caps(bulb)
        if color.get("mode") == "hs" and "hs" in caps:
            r, g, b = hs_rgb(color)
            data = {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}
        elif color.get("mode") == "ct" and "ct" in caps:
            from ..profiles import kelvin_rgb
            lo, hi = self.ct_range(bulb)
            kelvin = int(clamp(round(float(color["kelvin"])), lo, hi))
            r, g, b = (int(round(c)) for c in kelvin_rgb(kelvin))
            data = {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": kelvin}
        else:
            return False
        if self.colors.get(bulb.id) == data:
            return True
        await self.throttle.wait(bulb.id)
        try:
            await self.send(bulb, "colorwc", data)
            got = await self.dev_status(bulb)
            if not self._shows(got, data):
                log.debug("%s: shows %s, sent colour %s: once more", bulb.id, got, data)
                await self.send(bulb, "colorwc", data)
        except ConnectionError as exc:
            self._lost(bulb, exc)
        self.colors[bulb.id] = data
        self.presence.ok(bulb)
        return True

    @staticmethod
    def _shows(got: dict, data: dict) -> bool:
        if data["colorTemInKelvin"]:
            return got.get("colorTemInKelvin") == data["colorTemInKelvin"]
        return got.get("color") == data["color"]

    async def query(self, bulb) -> dict | None:
        got = await self.dev_status(bulb)
        on = bool(got.get("onOff"))
        return {"on": on, "percent": float(got.get("brightness", 0)) if on else 0}

    async def discover(self, timeout: float) -> list[dict]:
        transport = await self._socket()
        self.scan_found = {}
        try:
            transport.sendto(message("scan", {"account_topic": "reserve"}), (MULTICAST, SCAN_PORT))
            await asyncio.sleep(timeout)
            return list(self.scan_found.values())
        finally:
            self.scan_found = None

    async def add(self, config: dict) -> dict:
        host = str(config.get("host", "")).strip()
        try:
            got = await self.ask(host, "scan", int(config.get("scan_port", SCAN_PORT)),
                                 {"account_topic": "reserve"}, timeout=3.0)
        except (ConnectionError, OSError) as exc:
            raise UserError(tr(f"no Govee device answers at {host}; is LAN Control on in the Govee Home app?",
                               f"Unter {host} antwortet kein Govee-Gerät; ist „LAN Control“ in der "
                               "Govee-Home-App an?")) from exc
        config["host"] = host
        if got.get("device"):
            config["device"] = got["device"]
            config["id"] = mac_suffix(got["device"])[-12:]
        if got.get("sku"):
            config["sku"] = got["sku"]
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self.on.pop(bulb.id, None)
        self.colors.pop(bulb.id, None)

    async def resume(self) -> None:
        self.on.clear()
        self.colors.clear()

    async def stop(self) -> None:
        await self.presence.stop()
        if self.transport:
            self.transport.close()
            self.transport = None
