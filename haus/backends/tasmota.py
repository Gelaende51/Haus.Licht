"""Tasmota lights over local HTTP (`/cm?cmnd=`), plain aiohttp.

`Dimmer N` lights at N % (0-100), `Power OFF` switches off, `PowerOnState`
0/1/3 sets what the device does when it gets power. Polled every few seconds
with `Status 11`; a restarted `UptimeSec` is reported as a power return
(flick detection). See docs/backends-research.md#tasmota.
"""

from __future__ import annotations

import asyncio
import logging

from . import Backend, Offline
from ..common import UserError
from ._lan import Presence, Throttle, mdns_browse, mdns_host as _host, poll_loop

log = logging.getLogger("hauslicht")

POLL_S = 5.0
TIMEOUT_S = 4.0
POWERUP = {"off": 0, "on": 1, "restore": 3}


class TasmotaError(Exception):
    pass


class TasmotaBackend(Backend):
    kind = "tasmota"
    title = "Tasmota (local HTTP)"
    requires = ("aiohttp",)
    fields = (
        {"key": "host", "label": "Host or IP address", "label_de": "Hostname oder IP-Adresse",
         "type": "text", "required": True},
        {"key": "username", "label": "Web user (if a web password is set)",
         "label_de": "Web-Benutzer (falls ein Web-Passwort gesetzt ist)",
         "type": "text", "required": False, "default": "admin"},
        {"key": "password", "label": "Web password", "label_de": "Web-Passwort",
         "type": "password", "required": False, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.presence = Presence(ctx)
        self.throttle = Throttle(0.2)
        self.session = None
        self.last: dict[str, dict] = {}
        self.poller: asyncio.Task | None = None

    def _session(self):
        import aiohttp
        if self.session is None or self.session.closed:
            # one connection per device: Tasmota's web server handles one at a time
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                connector=aiohttp.TCPConnector(limit_per_host=1))
        return self.session

    async def _cmnd(self, config: dict, command: str, session=None) -> dict:
        params = {"cmnd": command}
        if config.get("password"):
            params["user"] = config.get("username") or "admin"
            params["password"] = config["password"]
        host = config["host"]
        async with (session or self._session()).get(f"http://{host}/cm", params=params) as r:
            data = await r.json(content_type=None)
            if r.status == 401 or (isinstance(data, dict) and "WARNING" in data and "password" in str(data["WARNING"])):
                raise UserError(f"Tasmota {host}: wrong or missing web password")
            if r.status >= 400:
                raise TasmotaError(f"{command}: HTTP {r.status}")
        if isinstance(data, dict) and data.get("Command") == "Unknown":
            raise TasmotaError(f"{command}: unknown command")
        return data

    async def _send(self, bulb, command: str) -> dict:
        import aiohttp
        try:
            return await self._cmnd(bulb.config, command)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError) as exc:
            self.presence.lost(bulb.id)
            raise Offline(f"tasmota {bulb.config['host']}: {exc}") from exc

    # ------------------------------------------------------------------ contract
    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim", "powerup", "flick"}

    def min_lit(self, bulb) -> float:
        return 1.0

    async def start(self) -> None:
        if self.poller is None:
            self.poller = asyncio.create_task(poll_loop(POLL_S, self.ctx.bulbs, self._probe))

    async def stop(self) -> None:
        if self.poller:
            self.poller.cancel()
            try:
                await self.poller
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.poller = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def suspend(self) -> None:
        if self.poller:
            self.poller.cancel()
            self.poller = None

    async def resume(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
        await self.start()

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self.last.pop(bulb.id, None)
        self.throttle.forget(bulb.id)

    async def apply(self, bulb, percent: float) -> None:
        on = percent > 0
        level = max(1, min(100, round(percent))) if on else 0
        await self.throttle.wait(bulb.id)
        try:
            # Dimmer > 0 also switches on (Tasmota's default, SetOption20 0)
            await self._send(bulb, f"Dimmer {level}" if on else "Power OFF")
        finally:
            self.throttle.sent(bulb.id)
        self.last[bulb.id] = {"on": on, "percent": float(level)}
        if self.presence.get(bulb.id) is not True:
            self.presence.contact(bulb.id)

    async def set_powerup(self, bulb, value: str) -> bool:
        data = await self._send(bulb, f"PowerOnState {POWERUP[value]}")
        return int(data.get("PowerOnState", -1)) == POWERUP[value]

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bool(online), "online": online}

    async def query(self, bulb) -> dict | None:
        await self._probe(bulb)
        return self.last.get(bulb.id)

    async def _probe(self, bulb) -> None:
        try:
            sts = (await self._send(bulb, "Status 11")).get("StatusSTS", {})
        except TasmotaError as exc:
            log.debug("tasmota %s: %s", bulb.id, exc)
            return
        power = sts.get("POWER", sts.get("POWER1"))
        if power is not None:
            on = str(power).upper() == "ON"
            self.last[bulb.id] = {"on": on, "percent": float(sts.get("Dimmer", 100)) if on else 0.0}
        uptime = sts.get("UptimeSec")
        self.presence.contact(bulb.id, float(uptime) if uptime is not None else None)

    async def discover(self, timeout: float) -> list[dict]:
        """mDNS `_http._tcp` names starting with "tasmota" (mDNS is off in
        many builds: then add by host)."""
        found = []
        for svc in await mdns_browse(["_http._tcp.local."], timeout):
            if not svc["name"].lower().startswith("tasmota"):
                continue
            addr = _host(svc)
            if addr:
                found.append({"kind": self.kind, "address": addr, "name": svc["name"],
                              "config": {"host": addr}})
        return found

    async def add(self, config: dict) -> dict:
        import aiohttp
        host = str(config.get("host") or "").strip()
        if not host:
            raise UserError("Tasmota: host is required")
        config["host"] = host
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
                sts = (await self._cmnd(config, "Status 11", s)).get("StatusSTS", {})
                net = (await self._cmnd(config, "Status 5", s)).get("StatusNET", {})
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise UserError(f"Tasmota {host} does not answer: {exc}") from exc
        except (TasmotaError, ValueError) as exc:
            raise UserError(f"Tasmota {host}: {exc}") from exc
        if "Dimmer" not in sts:
            raise UserError(f"Tasmota {host} has no dimmable light (no Dimmer in its status)")
        mac = str(net.get("Mac") or "").replace(":", "").lower()
        if mac:
            config["id"] = mac
        return config
