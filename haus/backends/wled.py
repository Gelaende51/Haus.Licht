"""WLED controllers over the network: WLED's JSON API, plain aiohttp.

python-wled (frenck, MIT) was checked and not used (dev/takeaways.md): its
`update()` reads /json plus presets.json, /json/cfg and the effect/palette
lists, which is far more than a level change needs on an ESP8266, it builds
URLs on port 80 only, and it brings mashumaro, orjson, awesomeversion and
backoff along. Two endpoints are all this needs:

- GET /json/info: ver, mac (12 hex digits), ip, name, uptime, leds {count,
  lc, rgbw, cct}. `lc` (WLED 0.14+) is a bit field: 1 RGB, 2 white channel,
  4 white temperature (CCT); older versions only say rgbw / cct as booleans.
- POST /json/state: {"on", "bri" 0-255, "tt" one-off transition in 100 ms
  units (at most 655), "seg": {"col": [[r, g, b(, w)]], "cct": 0-255}}. A
  `seg` object without an id applies to every selected segment.

Caps: onoff, dim (bri, step 100/255), hs when the LEDs have RGB, ct only
when they have a white temperature (CCT: `cct` 0-255 across ct_range, white
channel full) or RGB with a white channel (RGBW: the blackbody colour of
the Kelvin, its white part moved to W). No powerup and no flick: WLED's
boot behaviour is its own setting, and a reachable-again controller is only
`changed` (no uptime is passed on: a laptop's Wi-Fi drop must not toggle).
Found by mDNS (`_wled._tcp`, its TXT carries the MAC). The id is the MAC.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, Throttle, clamp, mac_suffix, tr
from ._lan import mdns_browse, mdns_host

log = logging.getLogger("hauslicht")

TIMEOUT_S = 4.0
SEND_INTERVAL_S = 0.1   # an ESP8266 copes with ~10 JSON posts a second
MAX_TT = 655            # "tt" in 100 ms units, WLED keeps the transition as 16-bit ms
LC_RGB, LC_WHITE, LC_CCT = 1, 2, 4
CT_RANGE = (2700, 6500)


def light_caps(leds: dict) -> int:
    """The `lc` bit field of /json/info's leds, derived for versions without it."""
    lc = leds.get("lc")
    if isinstance(lc, int):
        return lc & 0x0F
    out = LC_RGB
    if leds.get("rgbw") or leds.get("wv"):
        out |= LC_WHITE
    if leds.get("cct"):
        out |= LC_CCT | LC_WHITE
    return out


def rgbw(rgb: tuple[float, float, float], white: bool) -> list[int]:
    """0-1 RGB to WLED's colour list; with a white channel its white part goes to W."""
    r, g, b = rgb
    if not white:
        return [round(c * 255) for c in (r, g, b)]
    w = min(r, g, b)
    return [round(c * 255) for c in (r - w, g - w, b - w, w)]


class WledBackend(Backend):
    kind = "wled"
    title = "WLED (Wi-Fi LED controllers, local JSON API)"
    product = "WLED"
    experimental = True
    requires = ("aiohttp", "zeroconf")
    fields = (
        {"key": "host", "label": "Host or IP address", "label_de": "Hostname oder IP-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.presence = Presence(ctx, self._probe)
        self.throttle = Throttle(SEND_INTERVAL_S)
        self.sessions: dict[str, object] = {}
        self.info: dict[str, dict] = {}   # bulb id -> last /json/info

    # ------------------------------------------------------------ http
    def _session(self, bid: str):
        import aiohttp
        s = self.sessions.get(bid)
        if s is None or s.closed:
            s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
            self.sessions[bid] = s
        return s

    @staticmethod
    async def _request(session, host: str, path: str, body: dict | None = None) -> dict:
        url = f"http://{host}{path}"
        if body is None:
            async with session.get(url) as r:
                r.raise_for_status()
                return await r.json(content_type=None)
        async with session.post(url, json=body) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _call(self, bulb, path: str, body: dict | None = None) -> dict:
        """One exchange; a network failure marks the bulb lost and raises Offline."""
        import aiohttp
        host = bulb.config["host"]
        try:
            data = await self._request(self._session(bulb.id), host, path, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            self.presence.lost(bulb, str(exc) or type(exc).__name__)
            raise Offline(f"wled {host}: {exc}") from exc
        self.presence.ok(bulb)
        return data if isinstance(data, dict) else {}

    async def _probe(self, bulb) -> dict | None:
        import aiohttp
        try:
            info = await self._request(self._session(bulb.id), bulb.config["host"], "/json/info")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
        self.info[bulb.id] = info
        return {}  # no uptime: a return is only `changed`, never a flick

    async def _post(self, bulb, body: dict) -> None:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        await self.throttle.wait(bulb.id)
        await self._call(bulb, "/json/state", body)

    # ------------------------------------------------------------ contract
    def _lc(self, bulb) -> int:
        return int(bulb.config.get("lc", LC_RGB))

    def caps(self, bulb) -> set[str]:
        lc, caps = self._lc(bulb), {"onoff", "dim"}
        if lc & LC_RGB:
            caps.add("hs")
        if lc & LC_CCT or (lc & LC_RGB and lc & LC_WHITE):
            caps.add("ct")
        return caps

    def step(self, bulb) -> float:
        return 100.0 / 255

    def ct_range(self, bulb) -> tuple[int, int]:
        return CT_RANGE

    def details(self, bulb) -> dict:
        cfg = bulb.config
        info = self.info.get(bulb.id) or {}
        ver = info.get("ver") or cfg.get("ver")
        leds = (info.get("leds") or {}).get("count", cfg.get("leds"))
        return {"model": f"WLED {ver}" if ver else "WLED", "address": cfg.get("host"),
                "ip": info.get("ip") or cfg.get("ip"), "mac": cfg.get("mac"), "leds": leds}

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bool(online), "online": online}

    @staticmethod
    def _level(percent: float) -> dict:
        if percent <= 0:
            return {"on": False}
        return {"on": True, "bri": int(clamp(round(percent * 255 / 100.0), 1, 255))}

    async def apply(self, bulb, percent: float) -> None:
        await self._post(bulb, self._level(percent))

    async def fade(self, bulb, percent: float, seconds: float) -> bool:
        body = self._level(percent)
        body["tt"] = int(clamp(round(seconds * 10), 0, MAX_TT))
        await self._post(bulb, body)
        return True

    async def apply_color(self, bulb, color: dict) -> bool:
        lc = self._lc(bulb)
        white = bool(lc & LC_WHITE)
        if color.get("mode") == "hs" and lc & LC_RGB:
            rgb = colorsys.hsv_to_rgb((float(color["hue"]) % 360) / 360.0, clamp(float(color["sat"]) / 100.0, 0, 1), 1.0)
            seg = {"col": [rgbw(rgb, white)]}
        elif color.get("mode") == "ct" and lc & LC_CCT:
            lo, hi = CT_RANGE
            k = clamp(float(color["kelvin"]), lo, hi)
            seg = {"cct": round((k - lo) * 255 / (hi - lo)), "col": [[0, 0, 0, 255] if white else [255, 255, 255]]}
        elif color.get("mode") == "ct" and lc & LC_RGB and white:
            from ..profiles import kelvin_rgb
            seg = {"col": [rgbw(tuple(c / 255 for c in kelvin_rgb(float(color["kelvin"]))), True)]}
        else:
            return False
        await self._post(bulb, {"seg": seg})
        return True

    async def query(self, bulb) -> dict | None:
        st = await self._call(bulb, "/json/state")
        on = bool(st.get("on"))
        return {"on": on, "percent": round(int(st.get("bri", 0)) * 100 / 255, 1) if on else 0.0}

    async def discover(self, timeout: float) -> list[dict]:
        out, seen = [], set()
        for svc in await mdns_browse(["_wled._tcp.local."], timeout):
            host = mdns_host(svc)
            if not host:
                continue
            mac = mac_suffix(str((svc.get("properties") or {}).get("mac") or ""))
            address = mac if len(mac) == 12 else host
            if address in seen:
                continue
            seen.add(address)
            out.append({"kind": self.kind, "address": address, "name": svc.get("name") or f"WLED {host}",
                        "config": {"host": host}})
        return out

    async def add(self, config: dict) -> dict:
        import aiohttp
        host = str(config.get("host") or "").strip()
        if not host:
            raise UserError(tr("WLED: host is required", "WLED: Hostname fehlt"))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
                info = await self._request(s, host, "/json/info")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            raise UserError(tr(f"no WLED answers at {host} ({exc})",
                               f"Unter {host} antwortet kein WLED ({exc})")) from exc
        if not isinstance(info, dict) or "ver" not in info or not isinstance(info.get("leds"), dict):
            raise UserError(tr(f"{host} is not a WLED controller", f"{host} ist kein WLED-Controller"))
        leds = info["leds"]
        config.update({"host": host, "ver": str(info["ver"]), "lc": light_caps(leds),
                       "leds": int(leds.get("count") or 0)})
        if info.get("ip"):
            config["ip"] = info["ip"]
        mac = mac_suffix(str(info.get("mac") or ""))
        if len(mac) == 12:
            config["mac"] = mac
            config["id"] = mac
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        self.info.pop(bulb.id, None)
        s = self.sessions.pop(bulb.id, None)
        if s is not None:
            await s.close()

    async def resume(self) -> None:
        for s in self.sessions.values():
            await s.close()
        self.sessions.clear()

    async def stop(self) -> None:  # what the strip shows stays
        await self.presence.stop()
        for s in self.sessions.values():
            await s.close()
        self.sessions.clear()
