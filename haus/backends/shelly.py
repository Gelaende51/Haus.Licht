"""Shelly lights and dimmers over local HTTP, plain aiohttp (no aioshelly: it
pulls in Home Assistant's Bluetooth stack).

Gen1 (Duo, Vintage, Dimmer 1/2, Bulb): `GET /light/<ch>?turn=on&brightness=N`,
power-on via `/settings/light/<ch>?default_state=`, basic auth.
Gen2+ (Plus Wall Dimmer, Dimmer Gen3/4, Pro Dimmer): JSON-RPC `POST /rpc`
`Light.Set`, power-on via `Light.SetConfig` `initial_state`, digest auth.
The device is polled every few seconds; a restarted uptime is reported as a
power return (flick detection). See docs/backends-research.md#shelly.
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
GEN1_POWERUP = {"off": "off", "on": "on", "restore": "last"}
GEN2_POWERUP = {"off": "off", "on": "on", "restore": "restore_last"}


class ShellyError(Exception):
    pass


class ShellyBackend(Backend):
    kind = "shelly"
    title = "Shelly (Duo, Vintage, Dimmer; Gen1 and Gen2+, local HTTP)"
    experimental = True
    requires = ("aiohttp",)
    fields = (
        {"key": "host", "label": "Host or IP address", "label_de": "Hostname oder IP-Adresse",
         "type": "text", "required": True},
        {"key": "password", "label": "Password (if device login is on)",
         "label_de": "Passwort (falls die Geräteanmeldung an ist)",
         "type": "password", "required": False, "default": ""},
        {"key": "username", "label": "User name (Gen1 only)", "label_de": "Benutzername (nur Gen1)",
         "type": "text", "required": False, "default": "admin"},
        {"key": "channel", "label": "Light channel", "label_de": "Lichtkanal",
         "type": "int", "required": False, "default": 0},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.presence = Presence(ctx)
        self.throttle: dict[int, Throttle] = {1: Throttle(0.3), 2: Throttle(0.15)}
        self.sessions: dict[str, object] = {}  # bulb id -> aiohttp.ClientSession
        self.last: dict[str, dict] = {}         # bulb id -> {on, percent}
        self.poller: asyncio.Task | None = None

    # ------------------------------------------------------------------ http
    @staticmethod
    def _gen(config: dict) -> int:
        return 1 if int(config.get("gen", 2) or 2) == 1 else 2

    def _session(self, key: str, config: dict):
        import aiohttp
        s = self.sessions.get(key)
        if s is None or s.closed:
            password = config.get("password") or ""
            kwargs: dict = {"timeout": aiohttp.ClientTimeout(total=TIMEOUT_S)}
            if password and self._gen(config) == 1:
                user = config.get("username") or "admin"
                encode = getattr(aiohttp, "encode_basic_auth", None)
                if encode is not None:  # aiohttp >= 3.14
                    kwargs["headers"] = {"Authorization": encode(user, password)}
                else:
                    kwargs["auth"] = aiohttp.BasicAuth(user, password)
            elif password:
                kwargs["middlewares"] = (aiohttp.DigestAuthMiddleware("admin", password),)
            s = aiohttp.ClientSession(**kwargs)
            self.sessions[key] = s
        return s

    async def _get(self, session, host: str, path: str, params: dict | None = None) -> dict:
        async with session.get(f"http://{host}{path}", params=params) as r:
            if r.status == 401:
                raise UserError(f"Shelly {host}: wrong or missing password")
            if r.status >= 400:
                raise ShellyError(f"{path}: HTTP {r.status}")
            return await r.json(content_type=None)

    async def _rpc(self, session, host: str, method: str, params: dict | None = None) -> dict:
        body = {"id": 1, "method": method, "params": params or {}}
        async with session.post(f"http://{host}/rpc", json=body) as r:
            if r.status == 401:
                raise UserError(f"Shelly {host}: wrong or missing password")
            data = await r.json(content_type=None)
        if "error" in data:
            raise ShellyError(f"{method}: {data['error'].get('message', data['error'])}")
        return data.get("result", {})

    async def _call(self, bulb, gen1, gen2):
        """Run one exchange, mapping network failures to Offline."""
        import aiohttp
        session = self._session(bulb.id, bulb.config)
        host = bulb.config["host"]
        try:
            if self._gen(bulb.config) == 1:
                return await gen1(session, host)
            return await gen2(session, host)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError) as exc:
            self.presence.lost(bulb.id)
            raise Offline(f"shelly {host}: {exc}") from exc

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
        for s in self.sessions.values():
            await s.close()
        self.sessions.clear()

    async def remove(self, bulb) -> None:
        s = self.sessions.pop(bulb.id, None)
        if s is not None:
            await s.close()
        self.presence.forget(bulb.id)
        self.last.pop(bulb.id, None)

    async def suspend(self) -> None:
        if self.poller:
            self.poller.cancel()
            self.poller = None

    async def resume(self) -> None:
        for s in self.sessions.values():
            await s.close()
        self.sessions.clear()
        await self.start()

    async def apply(self, bulb, percent: float) -> None:
        ch = int(bulb.config.get("channel", 0) or 0)
        on = percent > 0
        level = max(1, min(100, round(percent))) if on else None
        throttle = self.throttle[self._gen(bulb.config)]
        await throttle.wait(bulb.id)

        async def gen1(s, host):
            params = {"turn": "on" if on else "off"}
            if on:
                params["brightness"] = str(level)
            return await self._get(s, host, f"/light/{ch}", params)

        async def gen2(s, host):
            params = {"id": ch, "on": on}
            if on:
                params["brightness"] = level
            return await self._rpc(s, host, "Light.Set", params)

        try:
            await self._call(bulb, gen1, gen2)
        finally:
            throttle.sent(bulb.id)
        self.last[bulb.id] = {"on": on, "percent": float(level) if on else 0.0}
        if self.presence.get(bulb.id) is not True:
            self.presence.contact(bulb.id)

    async def set_powerup(self, bulb, value: str) -> bool:
        ch = int(bulb.config.get("channel", 0) or 0)

        async def gen1(s, host):
            return await self._get(s, host, f"/settings/light/{ch}", {"default_state": GEN1_POWERUP[value]})

        async def gen2(s, host):
            return await self._rpc(s, host, "Light.SetConfig",
                                   {"id": ch, "config": {"initial_state": GEN2_POWERUP[value]}})

        await self._call(bulb, gen1, gen2)
        return True

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bool(online), "online": online}

    async def query(self, bulb) -> dict | None:
        await self._probe(bulb)
        return self.last.get(bulb.id)

    async def _probe(self, bulb) -> None:
        ch = int(bulb.config.get("channel", 0) or 0)

        async def gen1(s, host):
            st = await self._get(s, host, "/status")
            light = (st.get("lights") or [{}])[ch] if len(st.get("lights") or []) > ch else {}
            return st.get("uptime"), light.get("ison"), light.get("brightness")

        async def gen2(s, host):
            st = await self._rpc(s, host, "Shelly.GetStatus")
            light = st.get(f"light:{ch}", {})
            return (st.get("sys") or {}).get("uptime"), light.get("output"), light.get("brightness")

        try:
            uptime, on, level = await self._call(bulb, gen1, gen2)
        except ShellyError as exc:
            log.debug("shelly %s: %s", bulb.id, exc)
            return
        if on is not None:
            self.last[bulb.id] = {"on": bool(on), "percent": float(level or 0) if on else 0.0}
        self.presence.contact(bulb.id, float(uptime) if uptime is not None else None)

    async def discover(self, timeout: float) -> list[dict]:
        services = await mdns_browse(["_shelly._tcp.local.", "_http._tcp.local."], timeout)
        hosts: dict[str, str] = {}
        for svc in services:
            name = svc["name"].lower()
            if svc["type"].startswith("_http") and not name.startswith("shelly"):
                continue
            addr = _host(svc)
            if addr:
                hosts.setdefault(addr, svc["name"])
        return [d for d in await asyncio.gather(*(self._describe(h, n) for h, n in hosts.items())) if d]

    async def _describe(self, host: str, name: str) -> dict | None:
        """One discovered device: kept only when it has a light channel."""
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
                info = await self._get(s, host, "/shelly")
                gen = 1 if not info.get("gen") else int(info["gen"])
                if gen == 1:
                    lights = (await self._get(s, host, "/status")).get("lights") if not info.get("auth") else [None]
                    if not lights:
                        return None
                elif not info.get("auth_en"):
                    st = await self._rpc(s, host, "Shelly.GetStatus")
                    if "light:0" not in st:
                        return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, UserError, ShellyError, ValueError):
            return None
        return {"kind": self.kind, "address": host,
                "name": info.get("name") or name or info.get("model") or info.get("type") or host,
                "config": {"host": host}}

    async def add(self, config: dict) -> dict:
        import aiohttp
        host = str(config.get("host") or "").strip()
        if not host:
            raise UserError("Shelly: host is required")
        config["host"] = host
        ch = int(config.get("channel", 0) or 0)
        probe = dict(config)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
                info = await self._get(s, host, "/shelly")
            gen = 1 if not info.get("gen") else int(info["gen"])
            probe["gen"] = gen
            needs_auth = info.get("auth") if gen == 1 else info.get("auth_en")
            if needs_auth and not config.get("password"):
                raise UserError(f"Shelly {host} has login enabled: enter its password")
            session = self._session("add", probe)
            try:
                if gen == 1:
                    st = await self._get(session, host, "/status")
                    has_light = len(st.get("lights") or []) > ch
                else:
                    st = await self._rpc(session, host, "Shelly.GetStatus")
                    has_light = f"light:{ch}" in st
            finally:
                await self.sessions.pop("add").close()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise UserError(f"Shelly {host} does not answer: {exc}") from exc
        except (ShellyError, ValueError) as exc:
            raise UserError(f"Shelly {host}: {exc}") from exc
        if not has_light:
            raise UserError(f"Shelly {host} has no light channel {ch} (a relay or a roller is not dimmable)")
        config["gen"] = gen
        config["model"] = info.get("model") or info.get("type") or ""
        mac = str(info.get("mac") or "").replace(":", "").lower()
        if mac:
            config["id"] = mac if ch == 0 else f"{mac}-{ch}"
        return config
