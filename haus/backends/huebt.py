"""Philips Hue bulbs over Bluetooth (GATT), with HueBLE. Experimental.

The bulb must be pairable: in the Hue app, Settings > Voice assistants (or
a factory reset) makes it discoverable; HueBLE pairs on the first connect.
Brightness 1-254.

Sharing BlueZ with the mesh backend: no scanner of our own. The device is
looked up in BlueZ's object tree (the mesh scanner keeps it fresh) and only
otherwise found by a short scan for that one address. A link is opened when
there is something to send and closed after IDLE_S without changes. HueBLE
reconnects forever after an unexpected drop; we switch that off, a dropped
link is simply reopened by the next change.

Colour: HueBLE's `supports_colour_xy` (`hs`) and `supports_colour_temp`
(`ct`), known once a link is up (the GATT characteristics the bulb has), are
learned on the first link (add does not connect). The white range is
HueBLE's assumed 153-500 mired (`minimum_mireds` / `maximum_mireds`: the bulb
does not report one). Sent as `set_colour_xy` (hue/saturation via sRGB to
CIE xy) or `set_colour_temp` (mired), each one GATT write without power or
brightness, only while our last apply switched the bulb on, and not again
while the same value is on this link.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from . import Backend
from ..common import UserError
from ._bulbkit import (MAC_RE, Throttle, ble_device, ble_scan, clamp, color_caps, kelvin_range, learn_color,
                       mac_suffix, mired_kelvin, tr)

log = logging.getLogger("hauslicht")

HUE_SERVICE = "0000fe0f-0000-1000-8000-00805f9b34fb"
IDLE_S = 120.0
SEND_INTERVAL_S = 0.1
MIRED_RANGE = (153, 500)  # HueBLE's MIN_MIREDS / MAX_MIREDS (assumed, not read from the bulb)


def _hueble():
    import HueBLE
    HueBLE.DEFAULT_MAX_RECONNECT_ATTEMPTS = 0  # no reconnect loop: links are on demand
    return HueBLE


class HueBtBackend(Backend):
    kind = "huebt"
    bluetooth = True
    title = "Philips Hue Bluetooth"
    experimental = True
    requires = ("HueBLE", "bleak", "bleak-retry-connector")
    modules = ("HueBLE", "bleak", "bleak_retry_connector")
    fields = (
        {"key": "address", "label": "Bluetooth address", "label_de": "Bluetooth-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.lights: dict[str, object] = {}
        self.on: dict[str, bool] = {}
        self.sent: dict[str, tuple] = {}   # bulb id -> the colour last written on this link
        self.online: dict[str, bool | None] = {}
        self.idle: dict[str, asyncio.TimerHandle] = {}
        self.throttle = Throttle(SEND_INTERVAL_S)
        self.tasks: set[asyncio.Task] = set()

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | color_caps(bulb.config)

    def ct_range(self, bulb) -> tuple[int, int]:
        return kelvin_range(bulb.config, (mired_kelvin(MIRED_RANGE[1]), mired_kelvin(MIRED_RANGE[0])))

    def step(self, bulb) -> float:
        return 100 / 254  # brightness 1-254

    def min_lit(self, bulb) -> float:
        return 0.5  # level 1 of 254 is lit

    def status(self, bulb) -> dict:
        light = self.lights.get(bulb.id)
        return {"connected": bool(light is not None and light.connected), "online": self.online.get(bulb.id)}

    def _set_online(self, bulb, value: bool) -> None:
        if self.online.get(bulb.id) != value:
            self.online[bulb.id] = value
            self.ctx.changed()

    async def _link(self, bulb):
        light = self.lights.get(bulb.id)
        if light is not None and light.connected:
            return light
        hb = _hueble()
        dev = await ble_device(bulb.config["address"])
        if dev is None:
            raise ConnectionError(f"{bulb.config['address']} not seen: switched off or out of range")
        light = hb.HueBleLight(dev)
        try:
            await light.connect(max_attempts=2, connection_timeout=15, wait_timeout=20)
        except hb.HueBleError as exc:
            raise ConnectionError(f"{bulb.config['address']}: {exc}") from exc
        self.lights[bulb.id] = light
        self.on.pop(bulb.id, None)
        self.sent.pop(bulb.id, None)
        self._learn(bulb, light)
        self.ctx.changed()
        return light

    def _learn(self, bulb, light) -> None:
        """What the bulb's GATT table offers, stored once (add does not connect)."""
        caps = (["ct"] if light.supports_colour_temp else []) + (["hs"] if light.supports_colour_xy else [])
        kelvin = None
        if light.supports_colour_temp and light.minimum_mireds and light.maximum_mireds:
            kelvin = [mired_kelvin(light.maximum_mireds), mired_kelvin(light.minimum_mireds)]
        learn_color(self.ctx, bulb, caps, kelvin)

    def _arm_idle(self, bulb) -> None:
        if (handle := self.idle.pop(bulb.id, None)) is not None:
            handle.cancel()

        def close() -> None:
            self.idle.pop(bulb.id, None)
            task = asyncio.get_running_loop().create_task(self._close(bulb.id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        self.idle[bulb.id] = asyncio.get_running_loop().call_later(IDLE_S, close)

    async def _close(self, bid: str) -> None:
        light = self.lights.pop(bid, None)
        self.on.pop(bid, None)
        self.sent.pop(bid, None)
        if light is not None:
            with contextlib.suppress(Exception):
                await light.disconnect()
            self.ctx.changed()

    async def apply(self, bulb, percent: float) -> None:
        await self.throttle.wait(bulb.id)
        try:
            light = await self._link(bulb)
            hb = _hueble()
            try:
                if percent <= 0:
                    await light.set_power(False)
                    self.on[bulb.id] = False
                else:
                    await light.set_brightness(int(clamp(round(percent / 100 * 254), 1, 254)))
                    if not self.on.get(bulb.id):
                        await light.set_power(True)
                        self.on[bulb.id] = True
            except hb.HueBleError as exc:
                await self._close(bulb.id)
                raise ConnectionError(f"{bulb.config['address']}: {exc}") from exc
        except ConnectionError:
            self._set_online(bulb, False)
            raise  # the core retries with its back-off: each retry is one lookup, no standing scan
        self._set_online(bulb, True)
        self._arm_idle(bulb)

    async def apply_color(self, bulb, color: dict) -> bool:
        light = self.lights.get(bulb.id)
        if not self.on.get(bulb.id) or light is None or not light.connected:
            return False  # dark, not driven yet, or the link is gone (its state with it): leave it
        caps = self.caps(bulb)
        if color.get("mode") == "hs" and "hs" in caps:
            from .hue import hs_xy
            want = ("xy", *hs_xy(color))
        elif color.get("mode") == "ct" and "ct" in caps:
            lo, hi = self.ct_range(bulb)
            mired = int(clamp(mired_kelvin(clamp(float(color["kelvin"]), lo, hi)), *MIRED_RANGE))
            want = ("ct", mired)
        else:
            return False
        if self.sent.get(bulb.id) == want:
            return True  # already on this link: no second write
        await self.throttle.wait(bulb.id)
        try:
            hb = _hueble()
            try:
                if want[0] == "xy":
                    await light.set_colour_xy(want[1], want[2])
                else:
                    await light.set_colour_temp(want[1])
            except hb.HueBleError as exc:
                await self._close(bulb.id)
                raise ConnectionError(f"{bulb.config['address']}: {exc}") from exc
        except ConnectionError:
            self._set_online(bulb, False)
            raise
        self.sent[bulb.id] = want
        self._arm_idle(bulb)
        return True

    async def query(self, bulb) -> dict | None:
        light = await self._link(bulb)
        on = await light.poll_power_state()
        bri = await light.poll_brightness()
        self._arm_idle(bulb)
        return {"on": bool(on), "percent": round(bri / 254 * 100, 1) if on else 0}

    async def discover(self, timeout: float) -> list[dict]:
        def match(dev, adv):
            if HUE_SERVICE not in [u.lower() for u in (adv.service_uuids or [])]:
                return None
            return {"kind": self.kind, "address": dev.address.upper(),
                    "name": dev.name or adv.local_name or "Hue", "config": {"address": dev.address.upper()}}
        return await ble_scan(match, timeout)

    async def add(self, config: dict) -> dict:
        address = str(config.get("address", "")).strip().upper()
        if not MAC_RE.match(address):
            raise UserError(tr("Bluetooth address like AA:BB:CC:DD:EE:FF expected",
                               "Bluetooth-Adresse wie AA:BB:CC:DD:EE:FF erwartet"))
        config["address"] = address
        config["id"] = mac_suffix(address)
        return config

    async def remove(self, bulb) -> None:
        if (handle := self.idle.pop(bulb.id, None)) is not None:
            handle.cancel()
        await self._close(bulb.id)
        self.online.pop(bulb.id, None)

    async def suspend(self) -> None:
        for bid in list(self.lights):
            await self._close(bid)

    async def resume(self) -> None:
        for bid in list(self.lights):
            await self._close(bid)

    async def stop(self) -> None:
        for handle in self.idle.values():
            handle.cancel()
        self.idle.clear()
        for bid in list(self.lights):
            await self._close(bid)
