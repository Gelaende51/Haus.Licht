"""WiZ bulbs over their local UDP API (port 38899), with pywizlight.

setPilot {"state", "dimming"}: the firmware rejects or clamps dimming below
10, so 10 % is the lowest lit level. No local power-on setting (it lives in
the app/cloud). UDP drops packets when flooded: at most ~7 sends per second;
pywizlight waits for each answer (and repeats the datagram meanwhile), which
also tells whether the bulb is there.
"""

from __future__ import annotations

import asyncio
import logging

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, Throttle, clamp, mac_suffix, tr

log = logging.getLogger("hauslicht")

PORT = 38899
BROADCAST = "255.255.255.255"
SEND_INTERVAL_S = 0.15
SEND_TIMEOUT_S = 3.0  # pywizlight would keep trying for 13 s
MIN_DIMMING = 10


class WizBackend(Backend):
    kind = "wiz"
    title = "WiZ (Wi-Fi, local)"
    experimental = True
    requires = ("pywizlight",)
    fields = (
        {"key": "host", "label": "IP address", "label_de": "IP-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.lights: dict[str, object] = {}
        self.throttle = Throttle(SEND_INTERVAL_S)
        self.presence = Presence(ctx, self._probe)

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"}

    def min_lit(self, bulb) -> float:
        return float(MIN_DIMMING)

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bool(online), "online": online}

    def _light(self, bulb):
        light = self.lights.get(bulb.id)
        if light is None:
            from pywizlight import wizlight
            light = wizlight(bulb.config["host"], port=int(bulb.config.get("port", PORT)),
                             mac=bulb.config.get("mac"))
            self.lights[bulb.id] = light
        return light

    async def _send(self, bulb, params: dict) -> dict:
        from pywizlight.exceptions import WizLightError
        try:
            return await asyncio.wait_for(self._light(bulb).send(
                {"method": "setPilot" if params else "getPilot", "params": params}), SEND_TIMEOUT_S)
        except (WizLightError, OSError, TimeoutError) as exc:
            raise ConnectionError(f"{bulb.config['host']}: {exc}") from exc

    async def _probe(self, bulb) -> dict | None:
        try:
            await self._send(bulb, {})
            return {}
        except ConnectionError:
            return None

    async def apply(self, bulb, percent: float) -> None:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        await self.throttle.wait(bulb.id)
        params = ({"state": False} if percent <= 0 else
                  {"state": True, "dimming": int(clamp(round(percent), MIN_DIMMING, 100))})
        try:
            await self._send(bulb, params)
        except ConnectionError as exc:
            self.presence.lost(bulb, str(exc))
            raise Offline(str(exc)) from exc
        self.presence.ok(bulb)

    async def query(self, bulb) -> dict | None:
        resp = await self._send(bulb, {})
        result = resp.get("result", {})
        return {"on": result.get("state"), "percent": result.get("dimming") if result.get("state") else 0}

    async def discover(self, timeout: float) -> list[dict]:
        from pywizlight.discovery import find_wizlights
        found = await find_wizlights(wait_time=timeout, broadcast_address=BROADCAST)
        return [{"kind": self.kind, "address": b.ip_address, "name": f"WiZ {b.mac_address}",
                 "config": {"host": b.ip_address, "mac": b.mac_address}} for b in found]

    async def add(self, config: dict) -> dict:
        from pywizlight import wizlight
        from pywizlight.exceptions import WizLightError
        host = str(config.get("host", "")).strip()
        light = wizlight(host, port=int(config.get("port", PORT)))
        try:
            mac = await light.getMac()
        except (WizLightError, OSError) as exc:
            raise UserError(tr(f"no WiZ bulb answers at {host} ({exc}); is local communication allowed in the WiZ app?",
                               f"Unter {host} antwortet keine WiZ-Lampe ({exc}); ist die lokale Kommunikation "
                               "in der WiZ-App erlaubt?")) from exc
        finally:
            await light.async_close()
        config["host"] = host
        if mac:
            config["mac"] = mac
            config["id"] = mac_suffix(mac)
        return config

    async def remove(self, bulb) -> None:
        self.presence.forget(bulb.id)
        light = self.lights.pop(bulb.id, None)
        if light is not None:
            await light.async_close()

    async def resume(self) -> None:
        for light in self.lights.values():
            await light.async_close()  # a fresh socket after the sleep
        self.lights.clear()

    async def stop(self) -> None:
        await self.presence.stop()
        for light in self.lights.values():
            await light.async_close()
        self.lights.clear()
