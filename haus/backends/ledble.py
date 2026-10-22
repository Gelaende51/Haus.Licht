"""Cheap Bluetooth LED bulbs and strips (Triones, LEDBlue, LEDBLE, QHM,
Dream~), with led-ble. Experimental.

Sharing BlueZ with the mesh backend: no scanner of our own. The device is
looked up in BlueZ's object tree (the mesh scanner keeps it fresh) and only
otherwise found by a short scan for that one address. led-ble connects when
there is something to send and drops the link after 120 s without commands.
Brightness 0-255 on the white channel when the device shows white, else on
its colour (white when it shows none).

Colour: led-ble's model table (`model_data.color_modes`, by the model byte
of the first state reply) says whether the device has RGB channels (`hs`);
every model it knows has, and none a colour temperature (one white channel:
no `ct`). Learned on the first state read (add does not connect). Sent as
`set_rgb(rgb, level)` at the brightness last applied, only while our last
apply lit it and the device says on, and not again while the same colour is
on this link; brightness changes keep the colour sent.
"""

from __future__ import annotations

import logging

from . import Backend
from ..common import UserError
from ._bulbkit import MAC_RE, Throttle, ble_device, ble_scan, clamp, color_caps, hs_rgb, learn_color, mac_suffix, tr

log = logging.getLogger("hauslicht")

NAME_PREFIXES = ("Triones", "LEDBLE", "LEDBlue", "QHM-", "Dream~")
SEND_INTERVAL_S = 0.1


class LedBleBackend(Backend):
    kind = "ledble"
    bluetooth = True
    title = "Bluetooth LED (Triones, LEDBlue, QHM, Dream~)"
    experimental = True
    requires = ("led-ble", "bleak", "bleak-retry-connector")
    modules = ("led_ble", "bleak", "bleak_retry_connector")
    fields = (
        {"key": "address", "label": "Bluetooth address", "label_de": "Bluetooth-Adresse",
         "type": "text", "required": True, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.leds: dict[str, object] = {}
        self.known: set[str] = set()  # bulb ids whose state was read on this link
        self.level: dict[str, int] = {}             # bulb id -> the 0-255 level last applied (0: off)
        self.sent: dict[str, tuple] = {}            # bulb id -> the colour (full-value RGB) last sent
        self.online: dict[str, bool | None] = {}
        self.throttle = Throttle(SEND_INTERVAL_S)

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | color_caps(bulb.config)

    def step(self, bulb) -> float:
        return 100 / 255  # 8-bit channels

    def min_lit(self, bulb) -> float:
        return 1.0

    def status(self, bulb) -> dict:
        led = self.leds.get(bulb.id)
        client = getattr(led, "_client", None)
        return {"connected": bool(client is not None and client.is_connected), "online": self.online.get(bulb.id)}

    def _set_online(self, bulb, value: bool) -> None:
        if self.online.get(bulb.id) != value:
            self.online[bulb.id] = value
            self.ctx.changed()

    async def _led(self, bulb):
        led = self.leds.get(bulb.id)
        if led is None:
            from led_ble import LEDBLE
            dev = await ble_device(bulb.config["address"])
            if dev is None:
                raise ConnectionError(f"{bulb.config['address']} not seen: switched off or out of range")
            led = self.leds[bulb.id] = LEDBLE(dev)
        if bulb.id not in self.known:
            await led.update()  # connects, learns the protocol and what the device shows
            self.known.add(bulb.id)
            self._learn(bulb, led)
        return led

    def _learn(self, bulb, led) -> None:
        from flux_led.const import COLOR_MODES_RGB
        try:
            modes = set(led.model_data.color_modes)
        except (AssertionError, AttributeError):  # no state reply yet: the model is not known
            return
        learn_color(self.ctx, bulb, ["hs"] if modes & COLOR_MODES_RGB else [])

    async def _drop(self, bid: str) -> None:
        led = self.leds.pop(bid, None)
        self.known.discard(bid)
        self.sent.pop(bid, None)
        if led is not None:
            try:
                await led.stop()
            except Exception as exc:
                log.debug("ledble stop %s: %s", bid, exc)

    async def apply(self, bulb, percent: float) -> None:
        await self.throttle.wait(bulb.id)
        try:
            led = await self._led(bulb)
            if percent <= 0:
                await led.turn_off()
                self.level[bulb.id] = 0
            else:
                level = int(clamp(round(percent / 100 * 255), 1, 255))
                if not led.on:
                    await led.turn_on()
                if led.w or led.effect:
                    await led.set_brightness(level)
                else:  # the colour we sent, else what it shows (rgb_unscaled drifts by rounding)
                    rgb = self.sent.get(bulb.id) or led.rgb_unscaled
                    await led.set_rgb(rgb if any(rgb) else (255, 255, 255), level)
                self.level[bulb.id] = level
        except ConnectionError:
            self._set_online(bulb, False)
            raise
        except Exception as exc:  # BleakError, BleakNotFoundError, CharacteristicMissingError, timeouts
            await self._drop(bulb.id)
            self._set_online(bulb, False)
            raise ConnectionError(f"{bulb.config['address']}: {exc}") from exc
        self._set_online(bulb, True)

    async def apply_color(self, bulb, color: dict) -> bool:
        led = self.leds.get(bulb.id)
        level = self.level.get(bulb.id, 0)
        if led is None or not level or not led.on:
            return False  # dark or not driven yet: never switch it on
        if color.get("mode") != "hs" or "hs" not in self.caps(bulb):
            return False
        rgb = hs_rgb(color)
        if self.sent.get(bulb.id) == rgb and not led.w and not led.effect:
            return True  # already shown: no second write on the radio
        await self.throttle.wait(bulb.id)
        try:
            await led.set_rgb(rgb, level)
        except Exception as exc:  # BleakError, timeouts
            await self._drop(bulb.id)
            self._set_online(bulb, False)
            raise ConnectionError(f"{bulb.config['address']}: {exc}") from exc
        self.sent[bulb.id] = rgb
        return True

    async def query(self, bulb) -> dict | None:
        led = await self._led(bulb)
        await led.update()
        return {"on": led.on, "percent": round(led.brightness / 255 * 100, 1) if led.on else 0}

    async def discover(self, timeout: float) -> list[dict]:
        def match(dev, adv):
            name = dev.name or adv.local_name or ""
            if not name.startswith(NAME_PREFIXES):
                return None
            return {"kind": self.kind, "address": dev.address.upper(), "name": name,
                    "config": {"address": dev.address.upper()}}
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
        await self._drop(bulb.id)
        self.online.pop(bulb.id, None)
        self.level.pop(bulb.id, None)

    async def suspend(self) -> None:
        for bid in list(self.leds):
            await self._drop(bid)

    async def resume(self) -> None:
        for bid in list(self.leds):
            await self._drop(bid)

    async def stop(self) -> None:
        for bid in list(self.leds):
            await self._drop(bid)
