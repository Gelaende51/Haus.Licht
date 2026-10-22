"""WiZ bulbs over their local UDP API (port 38899), with pywizlight.

setPilot {"state", "dimming"}: the firmware rejects or clamps dimming below
10, so 10 % is the lowest lit level. No local power-on setting (it lives in
the app/cloud). UDP drops packets when flooded: at most ~7 sends per second;
pywizlight waits for each answer (and repeats the datagram meanwhile), which
also tells whether the bulb is there.

Colour: pywizlight's bulb type (getSystemConfig moduleName, getModelConfig
cctRange / getUserConfig whiteRange) says what the bulb is: RGB has `hs` and
`ct`, TW (and the TW fans) `ct`, DW none; the white range is the bulb's own.
Learned at add and stored (`color`, `kelvin`); an older config asks once on
the first contact. setPilot with PilotBuilder's r/g/b(/w) or `temp` and the
current `dimming`, never `state`: a colour does not switch the bulb on.
"""

from __future__ import annotations

import asyncio
import logging

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import Presence, Throttle, clamp, color_caps, kelvin_range, learn_color, mac_suffix, tr

log = logging.getLogger("hauslicht")

PORT = 38899
BROADCAST = "255.255.255.255"
SEND_INTERVAL_S = 0.15
SEND_TIMEOUT_S = 3.0  # pywizlight would keep trying for 13 s
MIN_DIMMING = 10
CT_RANGE = (2700, 6500)  # a TW bulb's usual range, when the bulb did not say


async def bulb_color(light) -> dict:
    """{color, kelvin} from pywizlight's bulb type (its own getSystemConfig /
    getModelConfig / getUserConfig); raises like the library."""
    bt = await light.get_bulbtype()
    caps = []
    k = bt.kelvin_range
    if bt.features.color_tmp and k and k.min < k.max:
        caps.append("ct")
    if bt.features.color:
        caps.append("hs")
    out = {"color": caps}
    if "ct" in caps:
        out["kelvin"] = [k.min, k.max]
    return out


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
        self.level: dict[str, int] = {}    # bulb id -> dimming last sent (absent: dark or not known)
        self.colors: dict[str, dict] = {}  # bulb id -> colour params last sent
        self.asked: set[str] = set()       # old configs: bulb type asked once

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, CT_RANGE)

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
        await self._set(bulb, params)
        if percent <= 0:
            self.level.pop(bulb.id, None)
            self.colors.pop(bulb.id, None)  # dark: the next lit colour is sent again
        else:
            self.level[bulb.id] = params["dimming"]
        if "color" not in bulb.config and bulb.id not in self.asked:
            await self._learn(bulb)

    async def _set(self, bulb, params: dict) -> None:
        try:
            await self._send(bulb, params)
        except ConnectionError as exc:
            self.presence.lost(bulb, str(exc))
            self.colors.pop(bulb.id, None)
            raise Offline(str(exc)) from exc
        self.presence.ok(bulb)

    async def _learn(self, bulb) -> None:
        """An older config without `color`: ask the bulb type once."""
        self.asked.add(bulb.id)
        try:
            got = await asyncio.wait_for(bulb_color(self._light(bulb)), 3 * SEND_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - an unknown module name, silence: keep today's caps
            log.info("%s: bulb type not read (%s): brightness only", bulb.id, exc)
            return
        learn_color(self.ctx, bulb, got["color"], got.get("kelvin"))

    async def apply_color(self, bulb, color: dict) -> bool:
        if self.presence.get(bulb.id) is False:
            raise Offline(f"{bulb.id} unreachable")
        level = self.level.get(bulb.id)
        if not level:
            return False  # dark or not driven yet: a colour must not switch it on
        from pywizlight import PilotBuilder
        caps = self.caps(bulb)
        if color.get("mode") == "hs" and "hs" in caps:
            pilot = PilotBuilder(hucolor=(float(color["hue"]) % 360, clamp(float(color["sat"]), 0, 100)))
        elif color.get("mode") == "ct" and "ct" in caps:
            lo, hi = self.ct_range(bulb)
            pilot = PilotBuilder(colortemp=int(clamp(round(float(color["kelvin"])), lo, hi)))
        else:
            return False
        if self.colors.get(bulb.id) == pilot.pilot_params:
            return True
        await self.throttle.wait(bulb.id)
        await self._set(bulb, {**pilot.pilot_params, "dimming": level})  # the brightness stays as it is
        self.colors[bulb.id] = pilot.pilot_params
        return True

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
            try:
                config.update(await bulb_color(light))
            except WizLightError as exc:  # an unknown module name: brightness only
                log.info("WiZ %s: bulb type not known (%s)", host, exc)
                config["color"] = []
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
        for d in (self.level, self.colors):
            d.pop(bulb.id, None)
        light = self.lights.pop(bulb.id, None)
        if light is not None:
            await light.async_close()

    async def resume(self) -> None:
        for light in self.lights.values():
            await light.async_close()  # a fresh socket after the sleep
        self.lights.clear()
        self.colors.clear()

    async def stop(self) -> None:
        await self.presence.stop()
        for light in self.lights.values():
            await light.async_close()
        self.lights.clear()
