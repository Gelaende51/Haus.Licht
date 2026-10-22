"""ESPHome lights over the native API (aioesphomeapi): one kept-open,
encrypted connection per device with state push.

The device needs `api: encryption: key:` (base64, 32 bytes); legacy
plaintext devices work with the key left empty. Power-on behaviour is
compiled into the firmware (`restore_mode`), not settable over the API, so it
is not offered. See docs/backends-research.md#esphome.

Colour: the light entity's `supported_color_modes` say what it has (a mode
with the RGB capability bit: `hs`; with COLOR_TEMPERATURE or COLD_WARM_WHITE:
`ct`, its range from the entity's min/max mireds). Learned at add (`color`,
`kelvin`); an older config learns it on the first connection. A colour is one
light_command with `color_mode` and `rgb` (at full colour brightness, white
channels off) or `color_temperature` (mireds), without `state` or
`brightness`: the level stays, and it goes out only while the light is on
(by our last apply and the pushed state).
"""

from __future__ import annotations

import asyncio
import logging

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import color_caps, hs_rgb, kelvin_range, learn_color, mired_kelvin
from ._lan import Presence, Throttle, mdns_browse, mdns_host

log = logging.getLogger("hauslicht")

TIMEOUT_S = 8.0
RETRY_S = (5, 10, 30, 60)
CT_RANGE = (2700, 6500)  # when the entity gives no mireds
RGB, CT, CWWW = 1 << 5, 1 << 3, 1 << 4  # aioesphomeapi LightColorCapability bits
# ColorMode values, preferred first: the plainest mode that shows the colour
HS_MODES = (35, 39, 51, 47)    # RGB, RGB_WHITE, RGB_COLD_WARM_WHITE, RGB_COLOR_TEMPERATURE
CT_MODES = (11, 19, 47, 51)    # COLOR_TEMPERATURE, COLD_WARM_WHITE, RGB_COLOR_TEMPERATURE, RGB_COLD_WARM_WHITE


def light_color(info) -> dict:
    """{color, kelvin} of an aioesphomeapi LightInfo."""
    modes = [int(m) for m in (getattr(info, "supported_color_modes", None) or ())]
    caps, out = [], {}
    if any(m & (CT | CWWW) for m in modes):
        caps.append("ct")
        lo, hi = float(info.min_mireds or 0), float(info.max_mireds or 0)
        if 0 < lo < hi:
            out["kelvin"] = [mired_kelvin(hi), mired_kelvin(lo)]
    if any(m & RGB for m in modes):
        caps.append("hs")
    out["color"] = caps
    return out


class Link:
    """One device: client, light entity key, connection state."""

    def __init__(self, client) -> None:
        self.client = client
        self.key: int | None = None
        self.info = None                  # the light's LightInfo
        self.connected = False
        self.state: dict | None = None    # {on, percent} as last pushed
        self.retry: asyncio.Task | None = None
        self.closing = False


class EsphomeBackend(Backend):
    kind = "esphome"
    title = "ESPHome light (native API)"
    experimental = True
    requires = ("aioesphomeapi",)
    fields = (
        {"key": "host", "label": "Host or IP address", "label_de": "Hostname oder IP-Adresse",
         "type": "text", "required": True},
        {"key": "encryption_key", "label": "API encryption key (base64)",
         "label_de": "API-Verschlüsselungsschlüssel (base64)",
         "type": "password", "required": False, "default": ""},
        {"key": "port", "label": "API port", "label_de": "API-Port",
         "type": "int", "required": False, "default": 6053},
        {"key": "entity", "label": "Light entity (object id; empty: the first light)",
         "label_de": "Licht-Entität (Objekt-ID; leer: das erste Licht)",
         "type": "text", "required": False, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.presence = Presence(ctx)
        self.throttle = Throttle(0.1)
        self.links: dict[str, Link] = {}
        self.running = False
        self.lit: dict[str, bool] = {}   # bulb id -> our last apply lit it

    # ------------------------------------------------------------------ connection
    def _client(self, config: dict):
        """A new APIClient for `config` (tests replace this)."""
        from aioesphomeapi import APIClient
        return APIClient(config["host"], int(config.get("port") or 6053), None,
                         noise_psk=(config.get("encryption_key") or None), client_info="hauslicht")

    async def _connect(self, bulb_id: str, config: dict) -> tuple[Link, object]:
        """Connect, find the light; returns (link, device_info). Raises the
        library's errors."""
        from aioesphomeapi import LightInfo, LightState
        link = self.links.get(bulb_id)
        if link is None or link.client is None:
            link = self.links[bulb_id] = Link(self._client(config))
        client = link.client

        async def on_stop(expected: bool) -> None:
            link.connected = False
            if link.closing:
                return
            self.presence.lost(bulb_id)
            log.info("esphome %s: disconnected", bulb_id)
            self._schedule_retry(bulb_id, config)

        await asyncio.wait_for(client.connect(on_stop=on_stop, login=True), TIMEOUT_S)
        try:
            info = await client.device_info()
            entities, _ = await client.list_entities_services()
            lights = [e for e in entities if isinstance(e, LightInfo)]
            want = (config.get("entity") or "").strip()
            light = next((e for e in lights if not want or e.object_id == want), None)
            if light is None:
                raise UserError(f"ESPHome {config['host']}: no light entity"
                                + (f" {want!r}; found {[e.object_id for e in lights]}" if want else ""))
            link.key = light.key
            link.info = light

            def on_state(state) -> None:
                if isinstance(state, LightState) and state.key == link.key:
                    link.state = {"on": bool(state.state),
                                  "percent": round(state.brightness * 100, 1) if state.state else 0.0}

            client.subscribe_states(on_state)
        except BaseException:
            link.closing = True
            await client.disconnect()
            raise
        link.connected = True
        link.closing = False
        return link, info

    def _schedule_retry(self, bulb_id: str, config: dict) -> None:
        link = self.links.get(bulb_id)
        if not self.running or link is None or (link.retry and not link.retry.done()):
            return
        link.retry = asyncio.create_task(self._retry(bulb_id, config))

    async def _retry(self, bulb_id: str, config: dict) -> None:
        n = 0
        while self.running and bulb_id in self.links and not self.links[bulb_id].connected:
            await asyncio.sleep(RETRY_S[min(n, len(RETRY_S) - 1)])
            n += 1
            try:
                await self._connect(bulb_id, config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("esphome %s: reconnect failed: %s", bulb_id, exc)
                continue
            self.presence.contact(bulb_id)

    async def _ensure(self, bulb) -> Link:
        from aioesphomeapi import APIConnectionError, InvalidAuthAPIError, InvalidEncryptionKeyAPIError, \
            RequiresEncryptionAPIError
        link = self.links.get(bulb.id)
        if link is not None and link.connected:
            self._learn(bulb, link)
            return link
        if link is not None and link.retry and not link.retry.done():
            raise Offline(f"esphome {bulb.config.get('host')}: reconnecting")
        try:
            link, _ = await self._connect(bulb.id, bulb.config)
        except (InvalidEncryptionKeyAPIError, RequiresEncryptionAPIError, InvalidAuthAPIError) as exc:
            raise UserError(f"ESPHome {bulb.config.get('host')}: wrong or missing API encryption key ({exc})") \
                from exc
        except (APIConnectionError, asyncio.TimeoutError, OSError) as exc:
            self.presence.lost(bulb.id)
            self._schedule_retry(bulb.id, bulb.config)
            raise Offline(f"esphome {bulb.config.get('host')}: {exc}") from exc
        self.presence.contact(bulb.id)
        self._learn(bulb, link)
        return link

    def _learn(self, bulb, link: Link) -> None:
        if "color" not in bulb.config and link.info is not None:  # an older config
            got = light_color(link.info)
            learn_color(self.ctx, bulb, got["color"], got.get("kelvin"))

    async def _close(self, bulb_id: str) -> None:
        link = self.links.pop(bulb_id, None)
        if link is None:
            return
        link.closing = True
        if link.retry:
            link.retry.cancel()
        try:
            await link.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ contract
    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, CT_RANGE)

    def step(self, bulb) -> float:
        return 1.0  # a float in the API; the output's own resolution is not reported

    def min_lit(self, bulb) -> float:
        return 1.0

    async def start(self) -> None:
        self.running = True
        for bulb in self.ctx.bulbs():
            try:
                await self._ensure(bulb)
            except (Offline, UserError) as exc:
                log.info("esphome %s: %s", bulb.id, exc)

    async def stop(self) -> None:
        self.running = False
        for bid in list(self.links):
            await self._close(bid)

    async def suspend(self) -> None:
        await self.stop()

    async def resume(self) -> None:
        await self.start()

    async def remove(self, bulb) -> None:
        await self._close(bulb.id)
        self.presence.forget(bulb.id)
        self.lit.pop(bulb.id, None)

    async def apply(self, bulb, percent: float) -> None:
        from aioesphomeapi import APIConnectionError
        link = await self._ensure(bulb)
        await self.throttle.wait(bulb.id)
        try:
            if percent <= 0:
                link.client.light_command(link.key, state=False)
            else:
                link.client.light_command(link.key, state=True,
                                          brightness=max(0.01, min(1.0, percent / 100)))
        except APIConnectionError as exc:
            link.connected = False
            self.presence.lost(bulb.id)
            self._schedule_retry(bulb.id, bulb.config)
            raise Offline(f"esphome {bulb.config.get('host')}: {exc}") from exc
        finally:
            self.throttle.sent(bulb.id)
        self.lit[bulb.id] = percent > 0

    async def apply_color(self, bulb, color: dict) -> bool:
        from aioesphomeapi import APIConnectionError
        if not self.lit.get(bulb.id):
            return False  # dark or not driven yet: never switch it on
        link = await self._ensure(bulb)
        if link.state is not None and not link.state.get("on"):
            return False  # switched off elsewhere since
        modes = [int(m) for m in (getattr(link.info, "supported_color_modes", None) or ())]
        caps = self.caps(bulb)
        if color.get("mode") == "hs" and "hs" in caps:
            mode = next((m for m in HS_MODES if m in modes), None)
            r, g, b = hs_rgb(color)
            kw = {"rgb": (r / 255, g / 255, b / 255), "color_brightness": 1.0}
            if mode in (39, 47):        # RGB_WHITE, RGB_COLOR_TEMPERATURE: white channel off
                kw["white"] = 0.0
            elif mode == 51:            # RGB_COLD_WARM_WHITE
                kw.update(cold_white=0.0, warm_white=0.0)
        elif color.get("mode") == "ct" and "ct" in caps:
            mode = next((m for m in CT_MODES if m in modes), None)
            lo, hi = self.ct_range(bulb)
            kw = {"color_temperature": float(mired_kelvin(max(lo, min(hi, float(color["kelvin"])))))}
            if mode in (47, 51):        # colour and white together: the white part only
                kw["color_brightness"] = 0.0
            if mode == 47:              # RGB_COLOR_TEMPERATURE has a white level (the WHITE bit)
                kw["white"] = 1.0
        else:
            return False
        if mode is None:
            return False
        await self.throttle.wait(bulb.id)
        try:
            link.client.light_command(link.key, color_mode=mode, **kw)
        except APIConnectionError as exc:
            link.connected = False
            self.presence.lost(bulb.id)
            self._schedule_retry(bulb.id, bulb.config)
            raise Offline(f"esphome {bulb.config.get('host')}: {exc}") from exc
        finally:
            self.throttle.sent(bulb.id)
        return True

    def status(self, bulb) -> dict:
        link = self.links.get(bulb.id)
        return {"connected": bool(link and link.connected), "online": self.presence.get(bulb.id)}

    async def query(self, bulb) -> dict | None:
        link = await self._ensure(bulb)
        return link.state

    async def discover(self, timeout: float) -> list[dict]:
        found = []
        for svc in await mdns_browse(["_esphomelib._tcp.local."], timeout):
            host = mdns_host({**svc, "port": None})
            if not host:
                continue
            config = {"host": host}
            if svc.get("port") and svc["port"] != 6053:
                config["port"] = svc["port"]
            props = svc.get("properties") or {}
            found.append({"kind": self.kind, "address": host,
                          "name": props.get("friendly_name") or svc["name"], "config": config})
        return found

    async def add(self, config: dict) -> dict:
        from aioesphomeapi import APIConnectionError, InvalidAuthAPIError, InvalidEncryptionKeyAPIError, \
            RequiresEncryptionAPIError
        host = str(config.get("host") or "").strip()
        if not host:
            raise UserError("ESPHome: host is required")
        config["host"] = host
        tmp = f"esphome:add:{host}"
        try:
            link, info = await self._connect(tmp, config)
            config.update(light_color(link.info))
        except RequiresEncryptionAPIError as exc:
            raise UserError(f"ESPHome {host} uses encryption: enter its API encryption key") from exc
        except (InvalidEncryptionKeyAPIError, InvalidAuthAPIError) as exc:
            raise UserError(f"ESPHome {host}: wrong API encryption key ({exc})") from exc
        except (APIConnectionError, asyncio.TimeoutError, OSError) as exc:
            raise UserError(f"ESPHome {host} does not answer: {exc}") from exc
        finally:
            await self._close(tmp)
        mac = str(getattr(info, "mac_address", "") or "").replace(":", "").lower()
        if mac:
            config["id"] = mac if not config.get("entity") else f"{mac}-{config['entity']}"
        return config
