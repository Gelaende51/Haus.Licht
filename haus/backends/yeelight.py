"""Yeelight Wi-Fi bulbs over their LAN API (JSON lines, TCP 55443), in plain
asyncio. Experimental.

A bulb answers at most 60 commands per minute on its control connection
("client quota exceeded"), far too few for following the screen. Music mode
lifts it: `set_music [1, host, port]` makes the bulb connect back to a TCP
server of ours, and commands sent on that connection have no quota (and no
answers). When the bulb cannot reach us (a firewall on the laptop), we stay
on the control connection at most once per CONTROL_INTERVAL_S.

"LAN Control" must be switched on per bulb in the Yeelight app. Power-on
behaviour is not offered: `set_default` only stores the current state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, Throttle, clamp, tr

log = logging.getLogger("hauslicht")

PORT = 55443
SSDP = ("239.255.255.250", 1982)
TIMEOUT_S = 3.0
MUSIC_INTERVAL_S = 0.1
CONTROL_INTERVAL_S = 1.1  # under the 60/min quota
EFFECT = ["smooth", 200]


class YeeError(ConnectionError):
    pass


class YeeBulb:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.n = 0
        self.music: asyncio.StreamWriter | None = None
        self.server: asyncio.Server | None = None
        self.music_failed = False  # the bulb never called back: stay on the control connection

    async def command(self, method: str, params: list) -> list:
        """One command on a fresh control connection; the result list."""
        self.n += 1
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            raise YeeError(f"{self.host}: {exc or 'timeout'}") from exc
        try:
            writer.write((json.dumps({"id": self.n, "method": method, "params": params}) + "\r\n").encode())
            await writer.drain()
            async with asyncio.timeout(TIMEOUT_S):
                while True:
                    line = await reader.readline()
                    if not line:
                        raise YeeError(f"{self.host}: connection closed")
                    msg = json.loads(line)
                    if msg.get("id") != self.n:
                        continue  # a props notification
                    if "error" in msg:
                        raise YeeError(f"{self.host}: {msg['error'].get('message', msg['error'])}")
                    return msg.get("result", [])
        except TimeoutError as exc:
            raise YeeError(f"{self.host}: no answer to {method}") from exc
        except (OSError, ValueError) as exc:
            raise YeeError(f"{self.host}: {exc}") from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def start_music(self) -> bool:
        """Our server on the address the bulb reaches us at; True once the bulb connected."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            raise YeeError(f"{self.host}: {exc or 'timeout'}") from exc
        local_ip = writer.get_extra_info("sockname")[0]
        writer.close()
        connected: asyncio.Future = asyncio.get_running_loop().create_future()

        def accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            if not connected.done():
                connected.set_result(w)
            else:
                w.close()

        await self.close_music()
        self.server = await asyncio.start_server(accept, local_ip, 0)
        port = self.server.sockets[0].getsockname()[1]
        await self.command("set_music", [1, local_ip, port])
        try:
            self.music = await asyncio.wait_for(connected, TIMEOUT_S)
        except TimeoutError:
            log.warning("%s: music mode: the bulb did not connect to %s:%d (firewall?): "
                        "at most one change per %.1f s", self.host, local_ip, port, CONTROL_INTERVAL_S)
            self.music_failed = True
            await self.close_music()
            return False
        finally:
            if self.server:
                self.server.close()  # one connection is all we take
        return True

    async def send_music(self, method: str, params: list) -> None:
        self.n += 1
        try:
            self.music.write((json.dumps({"id": self.n, "method": method, "params": params}) + "\r\n").encode())
            await asyncio.wait_for(self.music.drain(), TIMEOUT_S)
        except (OSError, TimeoutError, AttributeError) as exc:
            await self.close_music()
            raise YeeError(f"{self.host}: music connection lost ({exc})") from exc

    @property
    def in_music(self) -> bool:
        return self.music is not None and not self.music.is_closing()

    async def close_music(self) -> None:
        if self.music is not None:
            self.music.close()
            self.music = None
        if self.server is not None:
            self.server.close()
            self.server = None


class YeelightBackend(Backend):
    kind = "yeelight"
    title = "Yeelight (Wi-Fi, LAN control)"
    experimental = True
    requires = ()
    fields = (
        {"key": "host", "label": "IP address", "label_de": "IP-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.bulbs: dict[str, YeeBulb] = {}
        self.on: dict[str, bool] = {}
        self.throttle = Throttle(MUSIC_INTERVAL_S)
        self.presence = Presence(ctx, self._probe)

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"}

    def min_lit(self, bulb) -> float:
        return 1.0

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        yb = self.bulbs.get(bulb.id)
        return {"connected": bool(online), "online": online,
                "music": bool(yb and yb.in_music)}

    def _bulb(self, bulb) -> YeeBulb:
        yb = self.bulbs.get(bulb.id)
        if yb is None:
            yb = self.bulbs[bulb.id] = YeeBulb(bulb.config["host"], int(bulb.config.get("port", PORT)))
        return yb

    async def _probe(self, bulb) -> dict | None:
        try:
            await self._bulb(bulb).command("get_prop", ["power"])
            return {}
        except YeeError:
            return None

    async def apply(self, bulb, percent: float) -> None:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        yb = self._bulb(bulb)
        try:
            if not yb.in_music and not yb.music_failed:
                await yb.start_music()
                self.on.pop(bulb.id, None)  # music mode sends no state: assume nothing
            music = yb.in_music
            await self.throttle.wait(bulb.id, MUSIC_INTERVAL_S if music else CONTROL_INTERVAL_S)
            send = yb.send_music if music else yb.command
            if percent <= 0:
                await send("set_power", ["off", *EFFECT])
                self.on[bulb.id] = False
            else:
                if not self.on.get(bulb.id):
                    await send("set_power", ["on", *EFFECT])
                    self.on[bulb.id] = True
                await send("set_bright", [int(clamp(round(percent), 1, 100)), *EFFECT])
        except YeeError as exc:
            self.on.pop(bulb.id, None)
            if "quota" in str(exc):
                raise  # reachable, only too busy: the core retries later
            self.presence.lost(bulb, str(exc))
            yb.music_failed = False  # try music mode again once it is back
            raise Offline(str(exc)) from exc
        self.presence.ok(bulb)

    async def query(self, bulb) -> dict | None:
        power, bright = await self._bulb(bulb).command("get_prop", ["power", "bright"])
        on = power == "on"
        return {"on": on, "percent": float(bright) if on else 0}

    async def discover(self, timeout: float) -> list[dict]:
        """SSDP-like M-SEARCH to 239.255.255.250:1982; bulbs answer by unicast."""
        found: dict[str, dict] = {}
        loop = asyncio.get_running_loop()

        class Listen(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:
                head = {}
                for line in data.decode(errors="replace").splitlines():
                    key, sep, value = line.partition(":")
                    if sep:
                        head[key.strip().lower()] = value.strip()
                loc = head.get("location", "")
                if not loc.startswith("yeelight://"):
                    return
                host, _sep, port = loc[len("yeelight://"):].partition(":")
                dev_id = head.get("id", host)
                config = {"host": host, "device_id": dev_id}
                if port and int(port) != PORT:
                    config["port"] = int(port)
                found[dev_id] = {"kind": "yeelight", "address": host,
                                 "name": head.get("name") or f"Yeelight {head.get('model', '')}".strip(),
                                 "config": config}

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        transport, _ = await loop.create_datagram_endpoint(Listen, sock=sock)
        try:
            transport.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\n'
                             b'MAN: "ssdp:discover"\r\nST: wifi_bulb\r\n', SSDP)
            await asyncio.sleep(timeout)
        finally:
            transport.close()
        return list(found.values())

    async def add(self, config: dict) -> dict:
        host = str(config.get("host", "")).strip()
        try:
            await YeeBulb(host, int(config.get("port", PORT))).command("get_prop", ["power"])
        except YeeError as exc:
            raise UserError(tr(f"no Yeelight answers at {host} ({exc}); is LAN Control on in the Yeelight app?",
                               f"Unter {host} antwortet keine Yeelight ({exc}); ist „LAN-Steuerung“ "
                               "in der Yeelight-App an?")) from exc
        config["host"] = host
        dev_id = str(config.get("device_id", "")).lower()
        if dev_id:
            config["id"] = dev_id.removeprefix("0x").lstrip("0") or dev_id
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self.on.pop(bulb.id, None)
        yb = self.bulbs.pop(bulb.id, None)
        if yb:
            await yb.close_music()

    async def resume(self) -> None:
        for yb in self.bulbs.values():
            await yb.close_music()
            yb.music_failed = False
        self.on.clear()

    async def stop(self) -> None:
        await self.presence.stop()
        for yb in self.bulbs.values():
            await yb.close_music()
