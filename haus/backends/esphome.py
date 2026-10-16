"""ESPHome lights over the native API (aioesphomeapi): one kept-open,
encrypted connection per device with state push.

The device needs `api: encryption: key:` (base64, 32 bytes); legacy
plaintext devices work with the key left empty. Power-on behaviour is
compiled into the firmware (`restore_mode`), not settable over the API, so it
is not offered. See docs/backends-research.md#esphome.
"""

from __future__ import annotations

import asyncio
import logging

from . import Backend, Offline
from ..common import UserError
from ._lan import Presence, Throttle, mdns_browse, mdns_host

log = logging.getLogger("hauslicht")

TIMEOUT_S = 8.0
RETRY_S = (5, 10, 30, 60)


class Link:
    """One device: client, light entity key, connection state."""

    def __init__(self, client) -> None:
        self.client = client
        self.key: int | None = None
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
        return link

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
        return {"onoff", "dim"}

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
