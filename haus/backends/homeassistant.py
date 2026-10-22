"""Home Assistant lights over HA's WebSocket API (plain aiohttp).

One persistent WebSocket per server (base URL + long-lived access token).
A bulb is one `light.*` entity. State comes pushed (`state_changed` events);
an entity that goes `unavailable` and comes back is reported as a power
return. Power-on behaviour is offered when the integration exposes it as a
`select.<object id>_power_on_behavior` entity (zigbee2mqtt, ZHA).

Colour: the entity's `supported_color_modes` say what it has (hs, xy, rgb,
rgbw, rgbww: `hs`; color_temp: `ct`, its range from `min_color_temp_kelvin`
/ `max_color_temp_kelvin`, else the mired attributes). Learned at add
(`color`, `kelvin`); an older config learns it from the entity's state on
the first apply. A colour is `light.turn_on` with only `hs_color` or
`color_temp_kelvin`: HA keeps the brightness, but turn_on always switches
on, so it goes out only while the entity is on and our last apply lit it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time

from . import Backend, Offline
from ..common import UserError, _language
from ._bulbkit import color_caps, kelvin_range, learn_color, mired_kelvin

log = logging.getLogger("hauslicht")

MIN_INTERVAL_S = 0.15  # per entity: HA forwards to the device, which keeps its own limits
REQUEST_TIMEOUT_S = 10.0
POWERUP_OPTIONS = {  # our value -> option names integrations use (compared lower case)
    "off": ("off",),
    "on": ("on",),
    "restore": ("previous", "previousvalue", "previous_value", "restore", "last state"),
}

GERMAN = {
    "Home Assistant URL is required": "Die Home-Assistant-URL fehlt",
    "an access token is required (Profile → Security → Long-lived access tokens)":
        "Ein Zugriffstoken fehlt (Profil → Sicherheit → Langlebige Zugriffstoken)",
    "Home Assistant refused the token": "Home Assistant hat das Token abgelehnt",
    "Home Assistant not reachable at {url}: {error}": "Home Assistant unter {url} nicht erreichbar: {error}",
    "no light {entity} in Home Assistant": "Kein Licht {entity} in Home Assistant",
}


def _t(text: str) -> str:
    return GERMAN.get(text, text) if _language() == "de" else text


def ws_url(url: str) -> str:
    url = url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[8:]
    elif url.startswith("http://"):
        url = "ws://" + url[7:]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    return url if url.endswith("/api/websocket") else url + "/api/websocket"


class HAConnection:
    """One authenticated WebSocket to a Home Assistant server."""

    def __init__(self, url: str, token: str, on_state=None, on_change=None) -> None:
        self.url, self.token = url, token
        self.on_state = on_state    # callable(entity_id, old_state, new_state)
        self.on_change = on_change  # callable(): connected changed
        self.session = None
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.states: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def ensure(self) -> None:
        async with self.lock:
            if not self.connected:
                await self._connect()

    async def _connect(self) -> None:
        import aiohttp
        await self.close(quiet=True)
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await self.session.ws_connect(ws_url(self.url), heartbeat=30,
                                                    timeout=aiohttp.ClientWSTimeout(ws_close=5))
            first = await asyncio.wait_for(self.ws.receive_json(), REQUEST_TIMEOUT_S)
            if first.get("type") != "auth_required":
                raise ConnectionError(f"unexpected greeting {first.get('type')}")
            await self.ws.send_json({"type": "auth", "access_token": self.token})
            reply = await asyncio.wait_for(self.ws.receive_json(), REQUEST_TIMEOUT_S)
            if reply.get("type") == "auth_invalid":
                raise UserError(_t("Home Assistant refused the token"))
            if reply.get("type") != "auth_ok":
                raise ConnectionError(f"unexpected auth reply {reply.get('type')}")
        except BaseException:
            await self.close(quiet=True)
            raise
        self.reader = asyncio.get_running_loop().create_task(self._read())
        states = await self.request({"type": "get_states"})
        self.states = {s["entity_id"]: s for s in states}
        await self.request({"type": "subscribe_events", "event_type": "state_changed"})
        log.info("home assistant %s connected (%d entities)", self.url, len(self.states))
        if self.on_change:
            self.on_change()

    async def _read(self) -> None:
        import aiohttp
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                for item in (data if isinstance(data := msg.json(), list) else [data]):
                    self._dispatch(item)
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("home assistant connection closed"))
            self.pending.clear()
            if self.on_change:
                self.on_change()

    def _dispatch(self, item: dict) -> None:
        if item.get("type") == "result":
            fut = self.pending.pop(item.get("id"), None)
            if fut is None or fut.done():
                return
            if item.get("success"):
                fut.set_result(item.get("result"))
            else:
                err = item.get("error") or {}
                fut.set_exception(RuntimeError(f"home assistant: {err.get('code')}: {err.get('message')}"))
        elif item.get("type") == "event":
            data = (item.get("event") or {}).get("data") or {}
            entity = data.get("entity_id")
            if not entity:
                return
            old, new = self.states.get(entity), data.get("new_state")
            if new is None:
                self.states.pop(entity, None)
            else:
                self.states[entity] = new
            if self.on_state:
                self.on_state(entity, old, new)

    async def request(self, msg: dict):
        if not self.connected:
            raise ConnectionError("home assistant not connected")
        mid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self.ws.send_json({**msg, "id": mid})
        try:
            return await asyncio.wait_for(fut, REQUEST_TIMEOUT_S)
        finally:
            self.pending.pop(mid, None)

    async def call(self, domain: str, service: str, entity: str, data: dict | None = None) -> None:
        await self.ensure()
        await self.request({"type": "call_service", "domain": domain, "service": service,
                            "service_data": data or {}, "target": {"entity_id": entity}})

    async def close(self, quiet: bool = False) -> None:
        was = self.connected
        if self.reader:
            self.reader.cancel()
            self.reader = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        if self.session is not None:
            await self.session.close()
            self.session = None
        if was and not quiet and self.on_change:
            self.on_change()


HS_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}
CT_RANGE = (2000, 6535)  # HA's defaults (153-500 mired) when an entity gives none


def light_color(state: dict | None) -> dict:
    """{color, kelvin} of a light entity's state."""
    attrs = (state or {}).get("attributes") or {}
    modes = set(attrs.get("supported_color_modes") or ())
    caps, out = [], {}
    if "color_temp" in modes:
        caps.append("ct")
        lo, hi = attrs.get("min_color_temp_kelvin"), attrs.get("max_color_temp_kelvin")
        if not (lo and hi) and attrs.get("min_mireds") and attrs.get("max_mireds"):
            lo, hi = mired_kelvin(attrs["max_mireds"]), mired_kelvin(attrs["min_mireds"])
        if lo and hi and 0 < int(lo) <= int(hi):
            out["kelvin"] = [int(lo), int(hi)]
    if modes & HS_MODES:
        caps.append("hs")
    out["color"] = caps
    return out


def powerup_entity(entity: str) -> str:
    return "select." + entity.split(".", 1)[-1] + "_power_on_behavior"


def powerup_option(options: list, value: str) -> str | None:
    wanted = POWERUP_OPTIONS[value]
    return next((o for o in options if str(o).lower() in wanted), None)


class HomeAssistantBackend(Backend):
    kind = "homeassistant"
    title = "Home Assistant (lights of a Home Assistant server)"
    experimental = True  # tested against a fake WebSocket server only
    requires = ("aiohttp",)
    fields = (
        {"key": "url", "label": "Server URL, e.g. http://homeassistant.local:8123",
         "label_de": "Server-URL, z. B. http://homeassistant.local:8123",
         "type": "text", "required": True, "default": "http://homeassistant.local:8123"},
        {"key": "token", "label": "Long-lived access token (empty: the one of this server's other lights)",
         "label_de": "Langlebiges Zugriffstoken (leer: das der anderen Lichter dieses Servers)",
         "type": "password", "required": False, "default": ""},
        {"key": "entity", "label": "Light entity, e.g. light.desk", "label_de": "Licht-Entität, z. B. light.desk",
         "type": "text", "required": True},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.conns: dict[tuple[str, str], HAConnection] = {}
        self.last_sent: dict[str, float] = {}  # entity -> monotonic time of the last call
        self.lost_at: dict[str, float] = {}    # entity -> when it went unavailable
        self.lit: dict[str, bool] = {}         # bulb id -> our last apply lit it

    # ------------------------------------------------------------ helpers

    def conn(self, url: str, token: str) -> HAConnection:
        key = (url.rstrip("/"), token)
        if key not in self.conns:
            self.conns[key] = HAConnection(key[0], token, self._on_state, self._changed)
        return self.conns[key]

    def conn_of(self, bulb) -> HAConnection:
        return self.conn(bulb.config["url"], bulb.config["token"])

    def _changed(self) -> None:
        if self.ctx is not None:
            self.ctx.changed()

    def _on_state(self, entity: str, old: dict | None, new: dict | None) -> None:
        for bulb in self.ctx.bulbs():
            if bulb.config.get("entity") != entity:
                continue
            was = old is not None and old.get("state") != "unavailable"
            now = new is not None and new.get("state") != "unavailable"
            if was and not now:
                self.lost_at[entity] = time.monotonic()
                self.ctx.changed()
            elif now and not was and entity in self.lost_at:
                gap = time.monotonic() - self.lost_at.pop(entity)
                self.ctx.power_returned(bulb.id, gap)
                self.ctx.changed()

    async def _pace(self, key: str) -> None:
        wait = self.last_sent.get(key, 0.0) + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_sent[key] = time.monotonic()

    def _powerup_entity(self, bulb) -> str | None:
        conn = self.conns.get((bulb.config.get("url", "").rstrip("/"), bulb.config.get("token")))
        sel = powerup_entity(bulb.config["entity"])
        if conn is not None and conn.states:
            return sel if sel in conn.states else None
        return sel if bulb.config.get("powerup") else None

    # ------------------------------------------------------------ contract

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | ({"powerup"} if self._powerup_entity(bulb) else set()) | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, CT_RANGE)

    def step(self, bulb) -> float:
        return 100 / 255  # HA keeps brightness 0-255

    def min_lit(self, bulb) -> float:
        return 1.0  # brightness_pct 1 -> 3/255

    async def stop(self) -> None:
        for conn in self.conns.values():
            conn.on_change = None
            await conn.close()

    async def resume(self) -> None:
        for conn in self.conns.values():
            await conn.close()

    def status(self, bulb) -> dict:
        conn = self.conns.get((bulb.config.get("url", "").rstrip("/"), bulb.config.get("token")))
        if conn is None or not conn.connected:
            return {"connected": False, "online": None}
        state = conn.states.get(bulb.config["entity"])
        return {"connected": True,
                "online": None if state is None else state.get("state") != "unavailable"}

    async def apply(self, bulb, percent: float) -> None:
        conn, entity = self.conn_of(bulb), bulb.config["entity"]
        await conn.ensure()
        state = conn.states.get(entity)
        if state is not None and state.get("state") == "unavailable":
            raise Offline(f"{entity} unavailable")
        if "color" not in bulb.config and state is not None:  # an older config: learn it
            got = light_color(state)
            learn_color(self.ctx, bulb, got["color"], got.get("kelvin"))
        await self._pace(entity)
        if percent <= 0:
            await conn.call("light", "turn_off", entity)
        else:
            await conn.call("light", "turn_on", entity, {"brightness_pct": round(min(percent, 100.0), 1)})
        self.lit[bulb.id] = percent > 0

    async def apply_color(self, bulb, color: dict) -> bool:
        if not self.lit.get(bulb.id):
            return False  # dark or not driven yet: turn_on would switch it on
        conn, entity = self.conn_of(bulb), bulb.config["entity"]
        await conn.ensure()
        state = conn.states.get(entity)
        if state is None or state.get("state") != "on":
            return False  # off (or gone) since: leave it
        caps = self.caps(bulb)
        if color.get("mode") == "hs" and "hs" in caps:
            data = {"hs_color": [round(float(color["hue"]) % 360, 1),
                                 round(max(0.0, min(100.0, float(color["sat"]))), 1)]}
        elif color.get("mode") == "ct" and "ct" in caps:
            lo, hi = self.ct_range(bulb)
            data = {"color_temp_kelvin": int(max(lo, min(hi, round(float(color["kelvin"])))))}
        else:
            return False
        await self._pace(entity)
        await conn.call("light", "turn_on", entity, data)
        return True

    async def set_powerup(self, bulb, value: str) -> bool:
        conn = self.conn_of(bulb)
        await conn.ensure()
        sel = self._powerup_entity(bulb)
        state = conn.states.get(sel) if sel else None
        if state is None:
            return False
        option = powerup_option(state.get("attributes", {}).get("options", []), value)
        if option is None:
            log.warning("%s: no power-on option for %s in %s", bulb.id, value,
                        state.get("attributes", {}).get("options"))
            return False
        if state.get("state") == option:
            return True
        await conn.call("select", "select_option", sel, {"option": option})
        return True

    async def query(self, bulb) -> dict | None:
        conn = self.conn_of(bulb)
        await conn.ensure()
        state = conn.states.get(bulb.config["entity"])
        if state is None:
            return None
        bright = state.get("attributes", {}).get("brightness")
        return {"on": state.get("state") == "on",
                "percent": None if bright is None else round(bright / 255 * 100, 1)}

    async def add(self, config: dict) -> dict:
        url = str(config.get("url") or "").strip().rstrip("/")
        if not url:
            raise UserError(_t("Home Assistant URL is required"))
        token = str(config.get("token") or "").strip()
        if not token:  # the token of this server's other lights
            token = next((b.config.get("token") for b in self.ctx.bulbs()
                          if b.config.get("url", "").rstrip("/") == url and b.config.get("token")), "")
        if not token:
            raise UserError(_t("an access token is required (Profile → Security → Long-lived access tokens)"))
        entity = str(config.get("entity") or "").strip()
        if entity and "." not in entity:
            entity = "light." + entity
        conn = self.conn(url, token)
        try:
            await conn.ensure()
        except UserError:
            raise
        except Exception as exc:
            raise UserError(_t("Home Assistant not reachable at {url}: {error}").format(url=url, error=exc)) from exc
        if not entity.startswith("light.") or entity not in conn.states:
            raise UserError(_t("no light {entity} in Home Assistant").format(entity=entity))
        return {"url": url, "token": token, "entity": entity, **light_color(conn.states[entity]),
                "powerup": powerup_entity(entity) in conn.states, "id": entity.split(".", 1)[1]}

    async def remove(self, bulb) -> None:
        url, token = bulb.config.get("url", "").rstrip("/"), bulb.config.get("token")
        if not any(b is not bulb and b.config.get("url", "").rstrip("/") == url
                   and b.config.get("token") == token for b in self.ctx.bulbs()):
            conn = self.conns.pop((url, token), None)
            if conn is not None:
                await conn.close(quiet=True)

    async def discover(self, timeout: float) -> list[dict]:
        """The lights of the servers already configured that are not added yet."""
        servers = {(b.config.get("url", "").rstrip("/"), b.config.get("token")) for b in self.ctx.bulbs()}
        added = {b.config.get("entity") for b in self.ctx.bulbs()}
        found = []
        for url, token in sorted(s for s in servers if s[0] and s[1]):
            conn = self.conn(url, token)
            try:
                await asyncio.wait_for(conn.ensure(), timeout)
            except Exception as exc:
                log.info("discover homeassistant %s: %s", url, exc)
                continue
            for entity, state in sorted(conn.states.items()):
                if entity.startswith("light.") and entity not in added:
                    found.append({"kind": self.kind, "address": f"{url} {entity}",
                                  "name": state.get("attributes", {}).get("friendly_name") or entity,
                                  "config": {"url": url, "entity": entity}})
        return found
