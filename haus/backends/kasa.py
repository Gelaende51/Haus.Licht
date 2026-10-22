"""TP-Link Kasa and Tapo bulbs, locally, through python-kasa.

Older Kasa bulbs (IOT protocol) need nothing; newer Kasa firmware and every
Tapo bulb need the TP-Link cloud user name and password, used locally for the
KLAP/AES handshake (the session is kept, the handshake is costly). Power-on
behaviour is offered on IOT bulbs only (`hard_on` default behaviour: "last
status" or a custom preset; they cannot stay off). See
docs/backends-research.md#tp-link-kasa--tapo.

Colour: the Light module's features say what the bulb has (`hsv`: `hs`,
`color_temp`: `ct`, whose range is the feature's minimum/maximum: python-kasa
only offers it for a range wider than one value). Learned at add (`color`,
`kelvin`); an older config learns it when the device is first opened. SMART
`set_device_info` without `device_on` keeps a dark bulb dark; IOT
`set_light_state` switches on unless told otherwise (python-kasa adds
on_off 1), so IOT colour carries the brightness last sent and goes out only
while the bulb is lit. No second cache: a request/response protocol, and the
core sends a colour only when it moved or the bulb needs it again.
"""

from __future__ import annotations

import asyncio
import logging

from . import Backend, Offline
from ..common import UserError
from ._bulbkit import color_caps, kelvin_range, learn_color
from ._lan import Presence, Throttle, poll_loop

log = logging.getLogger("hauslicht")

POLL_S = 10.0
TIMEOUT_S = 5
CT_RANGE = (2500, 6500)  # python-kasa's default when a device reports none


def light_color(dev) -> dict:
    """{color, kelvin} from the device's Light module features."""
    from kasa import Module
    light = dev.modules.get(Module.Light)
    caps, out = [], {}
    if light is not None:
        ct = light.get_feature("color_temp")
        if ct is not None:
            caps.append("ct")
            out["kelvin"] = [int(ct.minimum_value), int(ct.maximum_value)]
        if light.has_feature("hsv"):
            caps.append("hs")
    out["color"] = caps
    return out


class KasaBackend(Backend):
    kind = "kasa"
    title = "TP-Link Kasa / Tapo (local, python-kasa)"
    experimental = True
    requires = ("python-kasa",)
    modules = ("kasa",)
    fields = (
        {"key": "host", "label": "Host or IP address", "label_de": "Hostname oder IP-Adresse",
         "type": "text", "required": True},
        {"key": "username", "label": "TP-Link account e-mail (Tapo, newer Kasa)",
         "label_de": "E-Mail des TP-Link-Kontos (Tapo, neuere Kasa)",
         "type": "text", "required": False, "default": ""},
        {"key": "password", "label": "TP-Link account password", "label_de": "Passwort des TP-Link-Kontos",
         "type": "password", "required": False, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.presence = Presence(ctx)
        self.throttle = Throttle(0.3)
        self.devices: dict[str, object] = {}   # bulb id -> kasa Device
        self.last: dict[str, float] = {}       # bulb id -> last percent sent
        self.poller: asyncio.Task | None = None

    # ------------------------------------------------------------------ device
    async def _open(self, config: dict):
        """A connected, updated kasa Device for `config` (tests replace this)."""
        from kasa import Credentials, Discover
        creds = None
        if config.get("username") or config.get("password"):
            creds = Credentials(config.get("username") or "", config.get("password") or "")
        dev = await Discover.discover_single(config["host"], credentials=creds, discovery_timeout=TIMEOUT_S)
        await dev.update()
        return dev

    async def _device(self, bulb):
        dev = self.devices.get(bulb.id)
        if dev is None:
            dev = await self._guard(bulb, self._open(bulb.config))
            self.devices[bulb.id] = dev
            if "color" not in bulb.config:  # an older config: learn it from this device
                got = light_color(dev)
                learn_color(self.ctx, bulb, got["color"], got.get("kelvin"))
        return dev

    async def _guard(self, bulb, coro):
        """Await `coro`, mapping lost connections to Offline."""
        from kasa.exceptions import AuthenticationError, KasaException, TimeoutError as KasaTimeout
        try:
            return await coro
        except AuthenticationError as exc:
            raise UserError(f"Kasa {bulb.config.get('host')}: the TP-Link account was refused ({exc})") from exc
        except (KasaTimeout, asyncio.TimeoutError, OSError) as exc:
            self.presence.lost(bulb.id)
            raise Offline(f"kasa {bulb.config.get('host')}: {exc}") from exc
        except KasaException as exc:
            if "connect" in str(exc).lower() or "unable to" in str(exc).lower():
                self.presence.lost(bulb.id)
                raise Offline(f"kasa {bulb.config.get('host')}: {exc}") from exc
            raise

    @staticmethod
    def _is_iot(dev) -> bool:
        from kasa.iot import IotDevice
        return isinstance(dev, IotDevice)

    # ------------------------------------------------------------------ contract
    def caps(self, bulb) -> set[str]:
        caps = {"onoff", "dim"}
        if bulb.config.get("family") == "iot":
            caps.add("powerup")
        return caps | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, CT_RANGE)

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
        for dev in self.devices.values():
            try:
                await dev.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.devices.clear()

    async def suspend(self) -> None:
        if self.poller:
            self.poller.cancel()
            self.poller = None

    async def resume(self) -> None:
        for dev in self.devices.values():  # sessions did not survive the sleep
            try:
                await dev.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.devices.clear()
        await self.start()

    async def remove(self, bulb) -> None:
        dev = self.devices.pop(bulb.id, None)
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.presence.forget(bulb.id)
        self.last.pop(bulb.id, None)

    async def apply(self, bulb, percent: float) -> None:
        from kasa import Module
        dev = await self._device(bulb)
        await self.throttle.wait(bulb.id)
        try:
            if percent <= 0:
                await self._guard(bulb, dev.turn_off())
            else:
                level = max(1, min(100, round(percent)))
                if self._is_iot(dev):
                    # IOT set_light_state switches on with the brightness
                    await self._guard(bulb, dev.modules[Module.Light].set_brightness(level))
                else:
                    # SMART: one request for on + level (set_brightness alone keeps it off)
                    await self._guard(bulb, dev.protocol.query(
                        {"set_device_info": {"device_on": True, "brightness": level}}))
        finally:
            self.throttle.sent(bulb.id)
        self.last[bulb.id] = percent
        if self.presence.get(bulb.id) is not True:
            self.presence.contact(bulb.id)

    async def apply_color(self, bulb, color: dict) -> bool:
        from kasa import Module
        if not self.last.get(bulb.id):
            return False  # dark or not driven yet: never switch it on
        caps = self.caps(bulb)
        if color.get("mode") == "hs" and "hs" in caps:
            want = {"hue": round(float(color["hue"])) % 360, "sat": int(max(0, min(100, round(float(color["sat"])))))}
        elif color.get("mode") == "ct" and "ct" in caps:
            lo, hi = self.ct_range(bulb)
            want = {"kelvin": int(max(lo, min(hi, round(float(color["kelvin"])))))}
        else:
            return False
        dev = await self._device(bulb)
        light = dev.modules[Module.Light]
        # IOT: python-kasa sends on_off 1 with it, and brightness unless given: the level we keep
        level = max(1, min(100, round(self.last[bulb.id]))) if self._is_iot(dev) else None
        await self.throttle.wait(bulb.id)
        try:
            if "hue" in want:
                await self._guard(bulb, light.set_hsv(want["hue"], want["sat"], level))
            else:
                await self._guard(bulb, light.set_color_temp(want["kelvin"], brightness=level))
        finally:
            self.throttle.sent(bulb.id)
        return True

    async def set_powerup(self, bulb, value: str) -> bool:
        dev = await self._device(bulb)
        if not self._is_iot(dev) or not hasattr(dev, "LIGHT_SERVICE"):
            return False
        if value == "off":
            # Kasa bulbs have no "stay off" after power returns
            log.info("%s: Kasa bulbs cannot stay off at power-up; left as is", bulb.id)
            return False
        current = await self._guard(bulb, dev._query_helper(dev.LIGHT_SERVICE, "get_default_behavior"))
        soft = current.get("soft_on") or {"mode": "last_status"}
        if value == "restore":
            hard = {"mode": "last_status"}
        else:
            level = max(1, min(100, round(self.last.get(bulb.id) or 100)))
            hard = {"mode": "customize_preset", "brightness": level}
            ct = (current.get("hard_on") or {}).get("color_temp")
            if ct:
                hard["color_temp"] = ct
        await self._guard(bulb, dev._query_helper(dev.LIGHT_SERVICE, "set_default_behavior",
                                                  {"soft_on": soft, "hard_on": hard}))
        return True

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bulb.id in self.devices and bool(online), "online": online}

    async def query(self, bulb) -> dict | None:
        from kasa import Module
        dev = await self._device(bulb)
        await self._guard(bulb, dev.update())
        light = dev.modules.get(Module.Light)
        on = bool(dev.is_on)
        return {"on": on, "percent": float(light.brightness) if (on and light) else 0.0}

    async def _probe(self, bulb) -> None:
        dev = await self._device(bulb)
        await self._guard(bulb, dev.update())
        self.presence.contact(bulb.id)

    async def discover(self, timeout: float) -> list[dict]:
        """UDP broadcast on 9999 (Kasa) and 20002 (Tapo / new Kasa)."""
        from kasa import Discover
        found = await Discover.discover(discovery_timeout=max(1, int(timeout)))
        out = []
        for host, dev in found.items():
            name = None
            try:
                name = dev.alias
            except Exception:  # noqa: BLE001 - not updated / needs credentials
                pass
            try:
                model = dev.model
            except Exception:  # noqa: BLE001
                model = ""
            out.append({"kind": self.kind, "address": host, "name": name or model or host,
                        "config": {"host": host}})
        return out

    async def add(self, config: dict) -> dict:
        from kasa import Module
        from kasa.exceptions import AuthenticationError, KasaException
        host = str(config.get("host") or "").strip()
        if not host:
            raise UserError("Kasa/Tapo: host is required")
        config["host"] = host
        try:
            dev = await self._open(config)
        except AuthenticationError as exc:
            raise UserError(f"{host}: this device needs the TP-Link account e-mail and password "
                            f"it was set up with ({exc})") from exc
        except (KasaException, asyncio.TimeoutError, OSError) as exc:
            raise UserError(f"Kasa/Tapo {host} does not answer: {exc}") from exc
        try:
            light = dev.modules.get(Module.Light)
            if light is None or not light.has_feature("brightness"):
                raise UserError(f"{host} ({dev.model}) is not a dimmable light")
            config["family"] = "iot" if self._is_iot(dev) else "smart"
            config.update(light_color(dev))
            config["model"] = dev.model
            mac = str(dev.mac or "").replace(":", "").replace("-", "").lower()
            if mac and mac.strip("0"):
                config["id"] = mac
        finally:
            try:
                await dev.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return config
