"""Philips Hue lights through a Hue Bridge, API v2 (CLIP v2), plain aiohttp.

Pairing: the user presses the bridge's link button, then `add hue host=…`
asks the bridge for an application key (`POST /api`, generatekey). Keys are
kept per bridge under the top-level `hue` key of hauslicht.json, so a failed
add (no light picked yet) does not need another button press. A bulb is one
`light` resource (its id, or its name at add time). The bridge has a
self-signed certificate (not checked, as aiohue does). Power-on behaviour is
the light's `powerup` object, offered when the light has one.

`host` may be a full `http://host:port` URL instead of an address (tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time

from . import Backend, Offline
from ..common import UserError, _language

log = logging.getLogger("hauslicht")

MIN_INTERVAL_S = 0.2  # per bridge: Signify allows ~10 light commands/s in total
REQUEST_TIMEOUT_S = 5.0
TRANSITION_MS = 200
ERROR_LINK_BUTTON = 101

# Field names from the openhue OpenAPI spec of CLIP v2 (not verified on a bridge).
POWERUP = {
    "on": {"preset": "custom", "on": {"mode": "on", "on": {"on": True}}, "dimming": {"mode": "previous"}},
    "off": {"preset": "custom", "on": {"mode": "on", "on": {"on": False}}},
    "restore": {"preset": "powerfail"},
}

GERMAN = {
    "bridge address is required": "Die Adresse der Bridge fehlt",
    "press the link button on the Hue Bridge, then add again within 30 s":
        "Den Verbindungsknopf der Hue Bridge drücken, dann innerhalb von 30 s erneut hinzufügen",
    "Hue Bridge not reachable at {host}: {error}": "Hue Bridge unter {host} nicht erreichbar: {error}",
    "the Hue Bridge refused the application key": "Die Hue Bridge hat den Anwendungsschlüssel abgelehnt",
    "pick a light: {known}": "Ein Licht wählen: {known}",
    "no light {light} on the bridge; known: {known}": "Kein Licht {light} an der Bridge; bekannt: {known}",
}


def _t(text: str) -> str:
    return GERMAN.get(text, text) if _language() == "de" else text


def base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("http://", "https://")) else f"https://{host}"


def powerup_matches(got: dict | None, value: str) -> bool:
    if not got:
        return False
    want = POWERUP[value]
    if got.get("preset") != want["preset"]:
        return False
    if want["preset"] == "custom":
        return (got.get("on") or {}).get("on", {}).get("on") == want["on"]["on"]["on"]
    return True


class HueError(Exception):
    pass


class HueBackend(Backend):
    kind = "hue"
    title = "Philips Hue Bridge (API v2)"
    experimental = True  # tested against a fake bridge only; powerup field names unverified
    requires = ("aiohttp",)
    fields = (
        {"key": "host", "label": "Bridge address", "label_de": "Adresse der Bridge",
         "type": "text", "required": True},
        {"key": "app_key", "label": "Application key (empty: press the bridge's link button first)",
         "label_de": "Anwendungsschlüssel (leer: zuerst den Verbindungsknopf der Bridge drücken)",
         "type": "password", "required": False, "default": ""},
        {"key": "light", "label": "Light (name or id)", "label_de": "Licht (Name oder ID)",
         "type": "text", "required": False, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.session = None
        self.last_sent: dict[str, float] = {}       # bridge -> last request
        self.reachable: dict[str, bool] = {}        # bridge -> last request got through
        self.powerup_cache: dict[str, dict] = {}    # light id -> powerup as last read

    # ------------------------------------------------------------ http

    def _session(self):
        import aiohttp
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S))
        return self.session

    async def request(self, host: str, method: str, path: str, key: str | None = None, body=None):
        import aiohttp
        headers = {"hue-application-key": key} if key else {}
        url = base_url(host) + path
        try:
            async with self._session().request(method, url, json=body, headers=headers, ssl=False) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            if self.reachable.get(host) is not False:
                self.reachable[host] = False
                self.ctx.changed()
            raise ConnectionError(f"hue bridge {host}: {exc}") from exc
        if self.reachable.get(host) is not True:
            self.reachable[host] = True
            self.ctx.changed()
        if status == 403 or status == 401:
            raise UserError(_t("the Hue Bridge refused the application key"))
        errors = data.get("errors") if isinstance(data, dict) else None
        if status >= 400 or errors:
            raise HueError(f"hue bridge {host}: HTTP {status} {errors or data}")
        return data

    async def _pace(self, host: str) -> None:
        wait = self.last_sent.get(host, 0.0) + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_sent[host] = time.monotonic()

    async def lights(self, host: str, key: str) -> list[dict]:
        return (await self.request(host, "GET", "/clip/v2/resource/light", key)).get("data", [])

    async def pair(self, host: str) -> str:
        body = {"devicetype": f"hauslicht#{socket.gethostname()[:19]}", "generatekey": True}
        data = await self.request(host, "POST", "/api", body=body)
        for item in data if isinstance(data, list) else [data]:
            if "success" in item:
                return item["success"]["username"]
            if (item.get("error") or {}).get("type") == ERROR_LINK_BUTTON:
                raise UserError(_t("press the link button on the Hue Bridge, then add again within 30 s"))
        raise HueError(f"hue bridge {host}: unexpected pairing answer {data}")

    def keys(self) -> dict:
        return self.ctx.state.setdefault("hue", {}).setdefault("keys", {})

    # ------------------------------------------------------------ contract

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | ({"powerup"} if bulb.config.get("powerup") else set())

    def step(self, bulb) -> float:
        return 100 / 254  # the lights' 8-bit level (1-254) behind API v2's percent

    def min_lit(self, bulb) -> float:
        return float(bulb.config.get("min_dim") or 1.0)  # the light's dimming.min_dim_level

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def status(self, bulb) -> dict:
        reachable = self.reachable.get(bulb.config.get("host"))
        return {"connected": bool(reachable), "online": reachable}

    async def apply(self, bulb, percent: float) -> None:
        c = bulb.config
        await self._pace(c["host"])
        if percent <= 0:
            body = {"on": {"on": False}, "dynamics": {"duration": TRANSITION_MS}}
        else:
            body = {"on": {"on": True}, "dimming": {"brightness": round(min(percent, 100.0), 2)},
                    "dynamics": {"duration": TRANSITION_MS}}
        try:
            await self.request(c["host"], "PUT", f"/clip/v2/resource/light/{c['light_id']}", c["app_key"], body)
        except HueError as exc:
            if "404" in str(exc):
                raise Offline(str(exc)) from exc
            raise

    async def set_powerup(self, bulb, value: str) -> bool:
        c = bulb.config
        if not c.get("powerup"):
            return False
        path = f"/clip/v2/resource/light/{c['light_id']}"
        if powerup_matches(self.powerup_cache.get(c["light_id"]), value):
            return True
        await self._pace(c["host"])
        await self.request(c["host"], "PUT", path, c["app_key"], {"powerup": POWERUP[value]})
        await self._pace(c["host"])
        data = (await self.request(c["host"], "GET", path, c["app_key"])).get("data") or [{}]
        self.powerup_cache[c["light_id"]] = data[0].get("powerup") or {}
        ok = powerup_matches(self.powerup_cache[c["light_id"]], value)
        if not ok:
            log.warning("%s: powerup after the change reads %s", bulb.id, self.powerup_cache[c["light_id"]])
        return ok

    async def query(self, bulb) -> dict | None:
        c = bulb.config
        data = (await self.request(c["host"], "GET", f"/clip/v2/resource/light/{c['light_id']}",
                                   c["app_key"])).get("data") or [{}]
        light = data[0]
        return {"on": (light.get("on") or {}).get("on"),
                "percent": (light.get("dimming") or {}).get("brightness")}

    async def add(self, config: dict) -> dict:
        host = str(config.get("host") or "").strip()
        if not host:
            raise UserError(_t("bridge address is required"))
        key = str(config.get("app_key") or "").strip() or self.keys().get(host, "")
        try:
            if not key:
                key = await self.pair(host)
                self.keys()[host] = key
                self.ctx.save()
            lights = await self.lights(host, key)
        except (UserError, HueError):
            raise
        except ConnectionError as exc:
            raise UserError(_t("Hue Bridge not reachable at {host}: {error}").format(host=host, error=exc)) from exc
        if self.keys().get(host) != key:
            self.keys()[host] = key
            self.ctx.save()
        names = ", ".join(sorted((li.get("metadata") or {}).get("name", li["id"]) for li in lights)) or "-"
        want = str(config.get("light") or "").strip()
        if not want:
            raise UserError(_t("pick a light: {known}").format(known=names))
        light = next((li for li in lights if li["id"] == want
                      or (li.get("metadata") or {}).get("name", "").lower() == want.lower()), None)
        if light is None:
            raise UserError(_t("no light {light} on the bridge; known: {known}").format(light=want, known=names))
        if light.get("powerup"):
            self.powerup_cache[light["id"]] = light["powerup"]
        return {"host": host, "app_key": key, "light_id": light["id"],
                "light": (light.get("metadata") or {}).get("name", ""),
                "min_dim": float((light.get("dimming") or {}).get("min_dim_level") or 1.0),
                "powerup": "powerup" in light, "id": light["id"][:8]}

    async def discover(self, timeout: float) -> list[dict]:
        """Lights of bridges we have a key for, plus bridges seen over mDNS."""
        found = []
        added = {b.config.get("light_id") for b in self.ctx.bulbs()}
        known_hosts = dict(self.keys())
        for b in self.ctx.bulbs():
            known_hosts.setdefault(b.config.get("host"), b.config.get("app_key"))
        for host, key in sorted(known_hosts.items()):
            if not host or not key:
                continue
            try:
                lights = await asyncio.wait_for(self.lights(host, key), timeout)
            except Exception as exc:
                log.info("discover hue %s: %s", host, exc)
                continue
            for li in lights:
                if li["id"] in added:
                    continue
                name = (li.get("metadata") or {}).get("name", li["id"])
                found.append({"kind": self.kind, "address": f"{host} {li['id']}", "name": name,
                              "config": {"host": host, "light": li["id"]}})
        for host, name in await self.browse(timeout):
            if host not in known_hosts:
                found.append({"kind": self.kind, "address": host, "name": name, "config": {"host": host}})
        return found

    async def browse(self, timeout: float) -> list[tuple[str, str]]:
        """Bridges announcing `_hue._tcp.local.` (needs the optional zeroconf)."""
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            return []
        stype = "_hue._tcp.local."
        names: list[str] = []

        def changed(zeroconf, service_type, name, state_change) -> None:
            if state_change is ServiceStateChange.Added:
                names.append(name)

        azc = AsyncZeroconf()
        out = []
        try:
            browser = AsyncServiceBrowser(azc.zeroconf, [stype], handlers=[changed])
            await asyncio.sleep(min(timeout, 3.0))
            await browser.async_cancel()
            for name in names:
                info = AsyncServiceInfo(stype, name)
                if await info.async_request(azc.zeroconf, 1000) and info.parsed_addresses():
                    out.append((info.parsed_addresses()[0], name.removesuffix("." + stype)))
        finally:
            with contextlib.suppress(Exception):
                await azc.async_close()
        return out
