"""MQTT lights: zigbee2mqtt devices, or any light with a command topic.

One aiomqtt connection per broker (host, port, user), kept up by a task that
reconnects. zigbee2mqtt: the bulb is a friendly name under the base topic
(default `zigbee2mqtt`); the device list comes from the retained
`<base>/bridge/devices`, the state from `<base>/<name>`, presence from
`<base>/<name>/availability` (when z2m's availability is on; offline → online
is a power return). Power-on behaviour is z2m's `power_on_behavior` (the ZCL
StartUpOnOff attribute in the bulb), offered when the device exposes it.
Generic: publish a payload template to a command topic, nothing read back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from . import Backend, Offline
from ..common import UserError, _language

log = logging.getLogger("hauslicht")

MIN_INTERVAL_S = 0.25  # per light: a Zigbee network takes only a few commands/s per device
TRANSITION_S = 0.2
CONNECT_TIMEOUT_S = 5.0
CONFIRM_S = 3.0  # how long a power-on change may take to show in the state
RECONNECT_S = (1, 2, 5, 10, 30)
Z2M_POWERUP = {"off": "off", "on": "on", "restore": "previous"}

GERMAN = {
    "MQTT broker {host}:{port} not reachable": "MQTT-Broker {host}:{port} nicht erreichbar",
    "no device list from zigbee2mqtt on {topic}": "Keine Geräteliste von zigbee2mqtt unter {topic}",
    "no zigbee2mqtt device {name}; known lights: {known}":
        "Kein zigbee2mqtt-Gerät {name}; bekannte Lichter: {known}",
    "{name} is no dimmable light": "{name} ist kein dimmbares Licht",
    "a command topic is required": "Ein Befehls-Topic fehlt",
    "a device name is required": "Ein Gerätename fehlt",
}


def _t(text: str) -> str:
    return GERMAN.get(text, text) if _language() == "de" else text


def link_key(config: dict) -> tuple:
    return (str(config.get("host", "")).strip(), int(config.get("port") or 1883),
            str(config.get("username") or ""), str(config.get("password") or ""), bool(config.get("tls")))


def light_expose(device: dict) -> dict | None:
    """The z2m `light` expose with a brightness feature, or None."""
    for expose in (device.get("definition") or {}).get("exposes") or []:
        if expose.get("type") == "light" and any(
                f.get("name") == "brightness" for f in expose.get("features") or []):
            return expose
    return None


def powerup_values(device: dict) -> list[str]:
    for expose in (device.get("definition") or {}).get("exposes") or []:
        if expose.get("property") == "power_on_behavior" or expose.get("name") == "power_on_behavior":
            return list(expose.get("values") or [])
    return []


def render(template: str, percent: float, scale: int) -> str:
    return (template.replace("{value}", str(round(percent / 100 * scale)))
            .replace("{percent}", f"{percent:g}"))


class BrokerLink:
    """One broker connection; subscriptions survive reconnects."""

    def __init__(self, key: tuple, on_message, on_change) -> None:
        self.host, self.port, self.username, self.password, self.tls = key
        self.on_message = on_message  # callable(link, topic, payload bytes)
        self.on_change = on_change    # callable(): connected changed
        self.topics: set[str] = set()
        self.client = None
        self.ready = asyncio.Event()
        self.task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self.ready.is_set()

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        import aiomqtt
        attempt = 0
        while True:
            try:
                tls = None
                if self.tls:
                    import ssl
                    tls = ssl.create_default_context()
                async with aiomqtt.Client(self.host, self.port, username=self.username or None,
                                          password=self.password or None, tls_context=tls,
                                          timeout=CONNECT_TIMEOUT_S) as client:
                    self.client = client
                    for topic in sorted(self.topics):
                        await client.subscribe(topic)
                    attempt = 0
                    self.ready.set()
                    log.info("mqtt %s:%d connected", self.host, self.port)
                    if self.on_change:
                        self.on_change()
                    async for msg in client.messages:
                        payload = msg.payload if isinstance(msg.payload, bytes) else str(msg.payload).encode()
                        self.on_message(self, str(msg.topic), payload)
            except aiomqtt.MqttError as exc:
                log.info("mqtt %s:%d: %s", self.host, self.port, exc)
            finally:
                was = self.ready.is_set()
                self.client = None
                self.ready.clear()
                if was and self.on_change:
                    self.on_change()
            await asyncio.sleep(RECONNECT_S[min(attempt, len(RECONNECT_S) - 1)])
            attempt += 1

    async def wait_ready(self, timeout: float = CONNECT_TIMEOUT_S) -> None:
        self.start()
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
        except TimeoutError as exc:
            raise ConnectionError(f"mqtt broker {self.host}:{self.port} not reachable") from exc

    async def subscribe(self, topic: str) -> None:
        if topic in self.topics:
            return
        self.topics.add(topic)
        if self.client is not None:
            await self.client.subscribe(topic)

    async def publish(self, topic: str, payload: str | bytes) -> None:
        if self.client is None:
            raise ConnectionError(f"mqtt broker {self.host}:{self.port} not connected")
        await self.client.publish(topic, payload)

    async def close(self) -> None:
        self.on_change = None
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None


class MqttBackend(Backend):
    kind = "mqtt"
    title = "MQTT (zigbee2mqtt, or any light with a command topic)"
    experimental = True  # tested against an in-process broker and a fake zigbee2mqtt only
    requires = ("aiomqtt",)
    fields = (
        {"key": "mode", "label": "Kind", "label_de": "Art", "type": "choice", "required": False,
         "default": "zigbee2mqtt", "choices": ["zigbee2mqtt", "generic"]},
        {"key": "host", "label": "Broker host", "label_de": "Broker-Host", "type": "text", "required": True},
        {"key": "port", "label": "Broker port", "label_de": "Broker-Port", "type": "int", "required": False,
         "default": 1883},
        {"key": "username", "label": "User name", "label_de": "Benutzername", "type": "text",
         "required": False, "default": ""},
        {"key": "password", "label": "Password (empty: the one of this broker's other lights)",
         "label_de": "Passwort (leer: das der anderen Lichter dieses Brokers)", "type": "password",
         "required": False, "default": ""},
        {"key": "tls", "label": "TLS", "label_de": "TLS", "type": "bool", "required": False, "default": False},
        {"key": "base_topic", "label": "zigbee2mqtt base topic", "label_de": "zigbee2mqtt-Basis-Topic",
         "type": "text", "required": False, "default": "zigbee2mqtt"},
        {"key": "device", "label": "zigbee2mqtt friendly name", "label_de": "zigbee2mqtt-Gerätename",
         "type": "text", "required": False, "default": ""},
        {"key": "command_topic", "label": "Generic: command topic", "label_de": "Generisch: Befehls-Topic",
         "type": "text", "required": False, "default": ""},
        {"key": "payload", "label": "Generic: payload, {percent} or {value} (0…scale) is filled in",
         "label_de": "Generisch: Nutzlast, {percent} oder {value} (0…Skala) wird eingesetzt",
         "type": "text", "required": False, "default": "{percent}"},
        {"key": "off_payload", "label": "Generic: payload for off", "label_de": "Generisch: Nutzlast für Aus",
         "type": "text", "required": False, "default": "OFF"},
        {"key": "scale", "label": "Generic: full scale of {value}", "label_de": "Generisch: Vollausschlag von {value}",
         "type": "int", "required": False, "default": 100},
        {"key": "availability_topic", "label": "Generic: availability topic (online/offline)",
         "label_de": "Generisch: Verfügbarkeits-Topic (online/offline)", "type": "text",
         "required": False, "default": ""},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.links: dict[tuple, BrokerLink] = {}
        self.devices: dict[tuple, list] = {}       # (link key, base) -> z2m device list
        self.states: dict[tuple, dict] = {}        # (link key, topic) -> last JSON state
        self.avail: dict[tuple, bool] = {}         # (link key, availability topic) -> online
        self.lost_at: dict[tuple, float] = {}
        self.last_sent: dict[str, float] = {}

    # ------------------------------------------------------------ helpers

    def link(self, config: dict) -> BrokerLink:
        key = link_key(config)
        if key not in self.links:
            self.links[key] = BrokerLink(key, self._on_message, self._changed)
        return self.links[key]

    def _changed(self) -> None:
        self.ctx.changed()

    @staticmethod
    def is_z2m(config: dict) -> bool:
        return config.get("mode", "zigbee2mqtt") == "zigbee2mqtt"

    @staticmethod
    def topics(config: dict) -> tuple[str, str | None, str | None]:
        """(command topic, state topic, availability topic)."""
        if MqttBackend.is_z2m(config):
            base = f"{config.get('base_topic') or 'zigbee2mqtt'}/{config['device']}"
            return f"{base}/set", base, f"{base}/availability"
        return config["command_topic"], None, config.get("availability_topic") or None

    def _on_message(self, link: BrokerLink, topic: str, payload: bytes) -> None:
        key = (link.host, link.port, link.username, link.password, link.tls)
        text = payload.decode(errors="replace").strip()
        if topic.endswith("/bridge/devices"):
            with contextlib.suppress(ValueError):
                self.devices[(key, topic[: -len("/bridge/devices")])] = json.loads(text)
            return
        bulbs = [b for b in self.ctx.bulbs() if link_key(b.config) == key]
        avail_bulbs = [b for b in bulbs if self.topics(b.config)[2] == topic]
        if topic.endswith("/availability") or avail_bulbs:
            with contextlib.suppress(ValueError):
                data = json.loads(text)
                text = data.get("state", text) if isinstance(data, dict) else text
            self._availability(avail_bulbs, (key, topic), text.lower() == "online")
        elif not topic.endswith(("/set", "/get")) and "/bridge/" not in topic:
            with contextlib.suppress(ValueError):
                data = json.loads(text)
                if isinstance(data, dict):
                    self.states.setdefault((key, topic), {}).update(data)

    def _availability(self, bulbs: list, akey: tuple, online: bool) -> None:
        was = self.avail.get(akey)
        self.avail[akey] = online
        if was is True and not online:
            self.lost_at[akey] = time.monotonic()
        elif online and was is False and akey in self.lost_at:
            gap = time.monotonic() - self.lost_at.pop(akey)
            for bulb in bulbs:
                self.ctx.power_returned(bulb.id, gap)
        if was != online and bulbs:
            self.ctx.changed()

    async def _pace(self, key: str) -> None:
        wait = self.last_sent.get(key, 0.0) + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_sent[key] = time.monotonic()

    async def _subscribe(self, link: BrokerLink, config: dict) -> None:
        if self.is_z2m(config):
            await link.subscribe(f"{config.get('base_topic') or 'zigbee2mqtt'}/#")
        elif config.get("availability_topic"):
            await link.subscribe(config["availability_topic"])

    async def _ready(self, bulb) -> BrokerLink:
        link = self.link(bulb.config)
        await self._subscribe(link, bulb.config)
        await link.wait_ready()
        return link

    # ------------------------------------------------------------ contract

    def caps(self, bulb) -> set[str]:
        values = bulb.config.get("powerup_values") or []
        powerup = self.is_z2m(bulb.config) and "on" in values and "off" in values
        return {"onoff", "dim"} | ({"powerup"} if powerup else set())

    def min_lit(self, bulb) -> float:
        return 1.0  # z2m brightness 1/254 lights, but many bulbs clamp to their own minimum

    async def start(self) -> None:
        for bulb in self.ctx.bulbs():
            await self._subscribe(self.link(bulb.config), bulb.config)
            self.link(bulb.config).start()

    async def stop(self) -> None:
        for link in self.links.values():
            await link.close()

    def status(self, bulb) -> dict:
        link = self.links.get(link_key(bulb.config))
        connected = bool(link and link.connected)
        _cmd, state_topic, avail_topic = self.topics(bulb.config)
        online = self.avail.get((link_key(bulb.config), avail_topic)) if avail_topic else None
        if online is None and connected and (link_key(bulb.config), state_topic) in self.states:
            online = True  # no availability published, but the state came
        return {"connected": connected, "online": online if connected else None}

    async def apply(self, bulb, percent: float) -> None:
        cmd, _state, avail_topic = self.topics(bulb.config)
        if avail_topic and self.avail.get((link_key(bulb.config), avail_topic)) is False:
            raise Offline(f"{bulb.id} offline")
        link = await self._ready(bulb)
        await self._pace(cmd)
        if self.is_z2m(bulb.config):
            if percent <= 0:
                msg = {"state": "OFF", "transition": TRANSITION_S}
            else:
                msg = {"state": "ON", "brightness": max(1, min(254, round(percent / 100 * 254))),
                       "transition": TRANSITION_S}
            await link.publish(cmd, json.dumps(msg))
        elif percent <= 0:
            await link.publish(cmd, str(bulb.config.get("off_payload", "OFF")))
        else:
            await link.publish(cmd, render(str(bulb.config.get("payload") or "{percent}"), percent,
                                           int(bulb.config.get("scale") or 100)))

    async def set_powerup(self, bulb, value: str) -> bool:
        if "powerup" not in self.caps(bulb):
            return False
        want = Z2M_POWERUP[value]
        if want not in bulb.config.get("powerup_values", []):
            return False
        cmd, state_topic, _avail = self.topics(bulb.config)
        link = await self._ready(bulb)
        skey = (link_key(bulb.config), state_topic)
        if self.states.get(skey, {}).get("power_on_behavior") == want:
            return True
        await self._pace(cmd)
        await link.publish(cmd, json.dumps({"power_on_behavior": want}))
        deadline = time.monotonic() + CONFIRM_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if self.states.get(skey, {}).get("power_on_behavior") == want:
                return True
        return False

    async def query(self, bulb) -> dict | None:
        _cmd, state_topic, _avail = self.topics(bulb.config)
        state = self.states.get((link_key(bulb.config), state_topic)) if state_topic else None
        if not state:
            return None
        bright = state.get("brightness")
        return {"on": str(state.get("state", "")).upper() == "ON",
                "percent": None if bright is None else round(bright / 254 * 100, 1)}

    def _fill_password(self, config: dict) -> None:
        if config.get("password"):
            return
        for b in self.ctx.bulbs():
            c = b.config
            if (c.get("host"), int(c.get("port") or 1883), c.get("username") or "") == (
                    config.get("host"), int(config.get("port") or 1883), config.get("username") or "") \
                    and c.get("password"):
                config["password"] = c["password"]
                return

    async def _device_list(self, link: BrokerLink, base: str, timeout: float) -> list | None:
        key = (link_key_of(link), base)
        await link.subscribe(f"{base}/#")
        await link.wait_ready(timeout)
        deadline = time.monotonic() + timeout
        while key not in self.devices and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return self.devices.get(key)

    async def add(self, config: dict) -> dict:
        config = dict(config)
        config["host"] = str(config.get("host") or "").strip()
        config["port"] = int(config.get("port") or 1883)
        self._fill_password(config)
        link = self.link(config)
        if not self.is_z2m(config):
            if not str(config.get("command_topic") or "").strip():
                raise UserError(_t("a command topic is required"))
            for k in ("base_topic", "device"):
                config.pop(k, None)
            try:
                await link.wait_ready()
            except ConnectionError as exc:
                raise UserError(_t("MQTT broker {host}:{port} not reachable").format(
                    host=config["host"], port=config["port"])) from exc
            await self._subscribe(link, config)
            return config
        name = str(config.get("device") or "").strip()
        if not name:
            raise UserError(_t("a device name is required"))
        base = config.get("base_topic") or "zigbee2mqtt"
        try:
            devices = await self._device_list(link, base, CONNECT_TIMEOUT_S)
        except ConnectionError as exc:
            raise UserError(_t("MQTT broker {host}:{port} not reachable").format(
                host=config["host"], port=config["port"])) from exc
        if devices is None:
            raise UserError(_t("no device list from zigbee2mqtt on {topic}").format(topic=f"{base}/bridge/devices"))
        device = next((d for d in devices if d.get("friendly_name") == name), None)
        if device is None:
            known = ", ".join(d["friendly_name"] for d in devices if light_expose(d)) or "-"
            raise UserError(_t("no zigbee2mqtt device {name}; known lights: {known}").format(name=name, known=known))
        if light_expose(device) is None:
            raise UserError(_t("{name} is no dimmable light").format(name=name))
        for k in ("command_topic", "payload", "off_payload", "scale", "availability_topic"):
            config.pop(k, None)
        config.update({"device": name, "base_topic": base, "powerup_values": powerup_values(device),
                       "id": name.replace(" ", "-").replace("/", "-").lower()})
        return config

    async def remove(self, bulb) -> None:
        key = link_key(bulb.config)
        if not any(b is not bulb and link_key(b.config) == key for b in self.ctx.bulbs()):
            link = self.links.pop(key, None)
            if link is not None:
                await link.close()

    async def discover(self, timeout: float) -> list[dict]:
        """zigbee2mqtt lights on the brokers already configured, not added yet."""
        seen, found = set(), []
        added = {(link_key(b.config), b.config.get("base_topic"), b.config.get("device")) for b in self.ctx.bulbs()}
        for bulb in self.ctx.bulbs():
            if not self.is_z2m(bulb.config):
                continue
            key, base = link_key(bulb.config), bulb.config.get("base_topic") or "zigbee2mqtt"
            if (key, base) in seen:
                continue
            seen.add((key, base))
            try:
                devices = await self._device_list(self.link(bulb.config), base, timeout) or []
            except ConnectionError as exc:
                log.info("discover mqtt: %s", exc)
                continue
            for d in devices:
                name = d.get("friendly_name")
                if light_expose(d) is None or (key, base, name) in added:
                    continue
                found.append({"kind": self.kind, "address": f"{key[0]}:{key[1]} {base}/{name}", "name": name,
                              "config": {"mode": "zigbee2mqtt", "host": key[0], "port": key[1],
                                         "username": key[2], "tls": key[4], "base_topic": base,
                                         "device": name}})
        return found


def link_key_of(link: BrokerLink) -> tuple:
    return (link.host, link.port, link.username, link.password, link.tls)
