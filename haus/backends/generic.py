"""Generic lights: defined entirely in the Add dialog, by templates.

A transport (command, http, mqtt, serial, udp) carries one message per
action; the templates on / off / brightness / colour / white are those
messages, filled with str.format_map from numbers only:

  {percent} 0-100, {level} 0-255, {hue} 0-360, {sat} 0-100, {kelvin},
  {r} {g} {b} 0-255 (the colour at full brightness), {r_dim} {g_dim} {b_dim}
  (scaled by brightness), {on} 1/0.   {{ and }} are literal braces.

- command: the template is split once (shlex), each argument filled, then
  exec'd without a shell: a value is a number, nothing can inject.
- http: method, URL template, headers; POST/PUT send the action template as
  the body, GET appends it to the URL.
- mqtt: broker, topic template; the action template is the payload. One
  connection per broker (mqtt.BrokerLink, the MQTT backend's).
- serial: port, baud; the action template is a line (\\n, \\r, \\t, \\xNN
  escapes). serial.py's port helpers; the port stays open.
- udp: host, port; the action template is the payload (\\xNN escapes).

Caps follow the templates: dim with brightness, hs with colour, ct with
white; an empty off template is brightness 0. Nothing is discovered.
"""

from __future__ import annotations

import asyncio
import colorsys
import contextlib
import importlib.util
import logging
import os
import re
import shlex
import shutil
import socket
import string
from urllib.parse import urlsplit

from . import Backend, NeedsAccess, Offline
from ..common import UserError
from ._bulbkit import no_access, tr

log = logging.getLogger("hauslicht")

TRANSPORTS = ("command", "http", "mqtt", "serial", "udp")
TEMPLATES = ("on", "off", "brightness", "colour", "white")
PLACEHOLDERS = ("percent", "level", "hue", "sat", "kelvin", "r", "g", "b", "r_dim", "g_dim", "b_dim", "on")
PLACEHOLDER_HINT = " ".join("{%s}" % p for p in PLACEHOLDERS)
TIMEOUT_S = 5.0
STDERR_CHARS = 200
NEEDS = {"http": ("aiohttp", "aiohttp"), "mqtt": ("aiomqtt", "aiomqtt"), "serial": ("pyserial", "serial")}
SAMPLE = {p: 1 for p in PLACEHOLDERS}
ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|n|r|t|0|\\)")
BAD_X = re.compile(r"\\x(?![0-9A-Fa-f]{2})")
SIMPLE = {"n": b"\n", "r": b"\r", "t": b"\t", "0": b"\0", "\\": b"\\"}


def _when(*transports: str) -> dict:
    return {"when": {"transport": list(transports)}}


def _template(key: str, label: str, label_de: str) -> dict:
    return {"key": key, "label": label, "label_de": label_de, "type": "text", "required": False,
            "default": "", "placeholder": PLACEHOLDER_HINT}


# ---------------------------------------------------------------- templates

def check_template(template: str, what: str) -> None:
    """Raise UserError for an unknown placeholder or a malformed template."""
    try:
        for _literal, name, spec, conversion in string.Formatter().parse(template):
            if name is None:
                continue
            if name not in PLACEHOLDERS:
                raise UserError(tr(
                    f"{what}: unknown placeholder {{{name}}}; known: {PLACEHOLDER_HINT} ({{{{ }}}} for braces)",
                    f"{what}: unbekannter Platzhalter {{{name}}}; bekannt: {PLACEHOLDER_HINT} ({{{{ }}}} für Klammern)"))
            if conversion or (spec and "{" in spec):
                raise ValueError("conversion or nested field")
        template.format_map(SAMPLE)
    except UserError:
        raise
    except (ValueError, IndexError, KeyError) as exc:
        raise UserError(tr(f"{what}: bad template ({exc}); {{{{ }}}} for literal braces",
                           f"{what}: fehlerhafte Vorlage ({exc}); {{{{ }}}} für wörtliche Klammern")) from exc


def check_escapes(template: str, what: str) -> None:
    if BAD_X.search(template):
        raise UserError(tr(f"{what}: \\x needs two hex digits (\\x0A)",
                           f"{what}: \\x braucht zwei Hex-Ziffern (\\x0A)"))


def unescape(text: str) -> bytes:
    """\\n \\r \\t \\0 \\\\ and \\xNN to bytes; other text as UTF-8."""
    out, pos = bytearray(), 0
    for m in ESCAPE.finditer(text):
        out += text[pos:m.start()].encode()
        code = m.group(1)
        out += bytes((int(code[1:], 16),)) if code[0] == "x" else SIMPLE[code]
        pos = m.end()
    out += text[pos:].encode()
    return bytes(out)


def values(percent: float, rgb: tuple[float, float, float], hue: float, sat: float, kelvin: int) -> dict:
    p = max(0.0, min(100.0, float(percent)))
    full = tuple(round(c * 255) for c in rgb)
    return {"percent": round(p), "level": round(p * 255 / 100), "hue": round(hue), "sat": round(sat),
            "kelvin": int(kelvin), "r": full[0], "g": full[1], "b": full[2],
            "r_dim": round(full[0] * p / 100), "g_dim": round(full[1] * p / 100),
            "b_dim": round(full[2] * p / 100), "on": 1 if p > 0 else 0}


def parse_headers(text: str) -> dict:
    headers = {}
    for line in re.split(r"\n|\\n", text or ""):
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise UserError(tr(f"Headers: \"Name: value\" per line, not {line.strip()!r}",
                               f"Header: \"Name: Wert\" je Zeile, nicht {line.strip()!r}"))
        headers[name.strip()] = value.strip()
    return headers


class GenericBackend(Backend):
    kind = "generic"
    title = "Generic light (command, HTTP, MQTT, serial, UDP templates)"
    product = "Generic light"
    experimental = True
    autoconnect = False
    fields = (
        {"key": "transport", "label": "Transport", "label_de": "Übertragung", "type": "choice",
         "choices": list(TRANSPORTS), "default": "command"},
        # http
        {"key": "method", "label": "HTTP method", "label_de": "HTTP-Methode", "type": "choice",
         "choices": ["GET", "POST", "PUT"], "default": "POST", **_when("http")},
        {"key": "url", "label": "URL (GET: the action template is appended)",
         "label_de": "URL (GET: die Vorlage der Aktion wird angehängt)", "type": "text", "required": True,
         "default": "", "placeholder": "http://lamp.local/api  " + PLACEHOLDER_HINT, **_when("http")},
        {"key": "headers", "label": "Headers (Name: value, several separated by \\n)",
         "label_de": "Header (Name: Wert, mehrere durch \\n getrennt)", "type": "text", "required": False,
         "default": "", "placeholder": "Content-Type: application/json", **_when("http")},
        # mqtt
        {"key": "broker", "label": "Broker host", "label_de": "Broker-Host", "type": "text", "required": True,
         "default": "", **_when("mqtt")},
        {"key": "broker_port", "label": "Broker port", "label_de": "Broker-Port", "type": "int",
         "default": 1883, **_when("mqtt")},
        {"key": "username", "label": "User name", "label_de": "Benutzername", "type": "text", "required": False,
         "default": "", **_when("mqtt")},
        {"key": "password", "label": "Password", "label_de": "Passwort", "type": "password", "required": False,
         "default": "", **_when("mqtt")},
        {"key": "topic", "label": "Topic", "label_de": "Topic", "type": "text", "required": True, "default": "",
         "placeholder": "lights/desk/set  " + PLACEHOLDER_HINT, **_when("mqtt")},
        # serial
        {"key": "port", "label": "Serial port", "label_de": "Serielle Schnittstelle", "type": "text",
         "required": True, "default": "", "placeholder": "/dev/serial/by-id/…", **_when("serial")},
        {"key": "baud", "label": "Baud rate", "label_de": "Baudrate", "type": "int", "default": 115200,
         **_when("serial")},
        # udp
        {"key": "host", "label": "Host", "label_de": "Host", "type": "text", "required": True, "default": "",
         **_when("udp")},
        {"key": "udp_port", "label": "UDP port", "label_de": "UDP-Port", "type": "int", "required": True,
         "default": 0, **_when("udp")},
        # the actions
        _template("on", "On", "Ein"),
        _template("off", "Off (empty: brightness 0)", "Aus (leer: Helligkeit 0)"),
        _template("brightness", "Brightness", "Helligkeit"),
        _template("colour", "Colour", "Farbe"),
        _template("white", "White", "Weiß"),
        {"key": "min_kelvin", "label": "White: warmest (K)", "label_de": "Weiß: wärmstes (K)", "type": "int",
         "default": 2700},
        {"key": "max_kelvin", "label": "White: coldest (K)", "label_de": "Weiß: kältestes (K)", "type": "int",
         "default": 6500},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.lit: dict[str, bool] = {}
        self.level: dict[str, float] = {}
        self.rgb: dict[str, tuple[float, float, float]] = {}
        self.hs: dict[str, tuple[float, float]] = {}
        self.kelvin: dict[str, int] = {}
        self.ports: dict[str, object] = {}     # bulb id -> open serial port
        self.brokers: dict[tuple, object] = {}  # mqtt link key -> BrokerLink
        self.session = None

    # ------------------------------------------------------------ contract
    @staticmethod
    def transport(cfg: dict) -> str:
        return cfg.get("transport") or "command"

    def caps(self, bulb) -> set[str]:
        cfg = bulb.config
        caps = {"onoff"}
        if cfg.get("brightness"):
            caps.add("dim")
        if cfg.get("colour"):
            caps.add("hs")
        if cfg.get("white"):
            caps.add("ct")
        return caps

    def step(self, bulb) -> float:
        t = str(bulb.config.get("brightness") or "")
        return 100.0 / 255 if any(f"{{{p}" in t for p in ("level", "r_dim", "g_dim", "b_dim")) else 1.0

    def ct_range(self, bulb) -> tuple[int, int]:
        cfg = bulb.config
        return (int(cfg.get("min_kelvin") or 2700), int(cfg.get("max_kelvin") or 6500))

    def details(self, bulb) -> dict:
        cfg, t = bulb.config, self.transport(bulb.config)
        target = None
        if t == "command":
            with contextlib.suppress(ValueError, IndexError):
                target = shlex.split(cfg.get("brightness") or cfg.get("on") or "")[0]
        elif t == "http":
            parts = urlsplit(str(cfg.get("url") or ""))  # no query: it may carry a key
            target = f"{cfg.get('method') or 'POST'} {parts.scheme}://{parts.hostname or ''}{parts.path}"
        elif t == "mqtt":
            target = f"{cfg.get('broker')}:{cfg.get('broker_port') or 1883} {cfg.get('topic')}"
        elif t == "serial":
            target = f"{cfg.get('port')} @ {cfg.get('baud') or 115200}"
        elif t == "udp":
            target = f"{cfg.get('host')}:{cfg.get('udp_port')}"
        return {"model": f"{self.product} ({t})", "transport": t, "address": target}

    def status(self, bulb) -> dict:
        t = self.transport(bulb.config)
        if t == "serial":
            port = str(bulb.config.get("port") or "")
            from .serial import is_url
            return {"connected": bulb.id in self.ports, "online": is_url(port) or os.path.exists(port)}
        if t == "mqtt":
            link = self.brokers.get(self._broker_key(bulb.config))
            connected = bool(link and link.connected)
            return {"connected": connected, "online": True if connected else None}
        return {"connected": True, "online": True}

    async def discover(self, timeout: float) -> list[dict]:
        return []

    # ------------------------------------------------------------ add
    async def add(self, config: dict) -> dict:
        cfg = dict(config)
        t = self.transport(cfg)
        if t not in TRANSPORTS:
            raise UserError(tr(f"Transport: one of {', '.join(TRANSPORTS)}",
                               f"Übertragung: eine von {', '.join(TRANSPORTS)}"))
        cfg["transport"] = t
        if t in NEEDS and importlib.util.find_spec(NEEDS[t][1]) is None:
            raise UserError(tr(f"{t} needs {NEEDS[t][0]}: pip install it into the venv",
                               f"{t} braucht {NEEDS[t][0]}: in die venv installieren (pip install)"))
        for key in TEMPLATES:
            cfg[key] = str(cfg.get(key) or "")
        if not cfg["on"] and not cfg["brightness"]:
            raise UserError(tr("An on or a brightness template is required",
                               "Eine Vorlage für Ein oder für Helligkeit ist nötig"))
        if not cfg["off"] and not cfg["brightness"]:
            raise UserError(tr("Without a brightness template, an off template is required",
                               "Ohne Vorlage für Helligkeit ist eine für Aus nötig"))
        labels = {"on": tr("On", "Ein"), "off": tr("Off", "Aus"), "brightness": tr("Brightness", "Helligkeit"),
                  "colour": tr("Colour", "Farbe"), "white": tr("White", "Weiß")}
        for key in TEMPLATES:
            if not cfg[key]:
                continue
            check_template(cfg[key], labels[key])
            if t in ("serial", "udp"):
                check_escapes(cfg[key], labels[key])
            if t == "command":
                self._check_command(cfg[key], labels[key])
        if cfg["white"]:
            lo, hi = int(cfg.get("min_kelvin") or 2700), int(cfg.get("max_kelvin") or 6500)
            if not 1000 <= lo < hi <= 20000:
                raise UserError(tr("White: warmest below coldest, within 1000–20000 K",
                                   "Weiß: wärmstes unter kältestem, innerhalb 1000–20000 K"))
            cfg["min_kelvin"], cfg["max_kelvin"] = lo, hi
        await getattr(self, f"_add_{t}")(cfg)
        cfg.pop("id", None)  # no device id: the core numbers the bulb
        return cfg

    @staticmethod
    def _check_command(template: str, what: str) -> None:
        try:
            argv = shlex.split(template)
        except ValueError as exc:
            raise UserError(tr(f"{what}: {exc}", f"{what}: {exc}")) from exc
        if not argv:
            raise UserError(tr(f"{what}: no command", f"{what}: kein Befehl"))
        for arg in argv:
            check_template(arg, what)
        prog = argv[0]
        if "{" in prog or not (shutil.which(prog) or (os.sep in prog and os.access(prog, os.X_OK))):
            raise UserError(tr(f"{what}: no program {prog}", f"{what}: kein Programm {prog}"))

    async def _add_command(self, cfg: dict) -> None:
        pass

    async def _add_http(self, cfg: dict) -> None:
        url = str(cfg.get("url") or "").strip()
        check_template(url, "URL")
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise UserError(tr("URL: http://… or https://…", "URL: http://… oder https://…"))
        method = str(cfg.get("method") or "POST").upper()
        if method not in ("GET", "POST", "PUT"):
            raise UserError(tr("HTTP method: GET, POST or PUT", "HTTP-Methode: GET, POST oder PUT"))
        parse_headers(str(cfg.get("headers") or ""))
        cfg.update({"url": url, "method": method})

    async def _add_mqtt(self, cfg: dict) -> None:
        broker, topic = str(cfg.get("broker") or "").strip(), str(cfg.get("topic") or "").strip()
        if not broker or not topic:
            raise UserError(tr("Broker host and topic are required", "Broker-Host und Topic sind nötig"))
        check_template(topic, "Topic")
        cfg.update({"broker": broker, "topic": topic, "broker_port": int(cfg.get("broker_port") or 1883)})
        link = self._broker(cfg)
        try:
            await link.wait_ready()
        except ConnectionError as exc:
            raise UserError(tr(f"MQTT broker {broker}:{cfg['broker_port']} not reachable",
                               f"MQTT-Broker {broker}:{cfg['broker_port']} nicht erreichbar")) from exc

    async def _add_serial(self, cfg: dict) -> None:
        from .serial import by_id_of, is_url
        port = str(cfg.get("port") or "").strip()
        if not is_url(port):
            if not port or not os.path.exists(port):
                raise UserError(tr(f"no serial port {port}", f"Keine serielle Schnittstelle {port}"))
            port = by_id_of(port) or port
            if not os.access(port, os.R_OK | os.W_OK):
                raise NeedsAccess(no_access(os.path.realpath(port)))
        cfg.update({"port": port, "baud": int(cfg.get("baud") or 115200)})

    async def _add_udp(self, cfg: dict) -> None:
        host, port = str(cfg.get("host") or "").strip(), int(cfg.get("udp_port") or 0)
        if not host or not 1 <= port <= 65535:
            raise UserError(tr("UDP: a host and a port 1–65535", "UDP: ein Host und ein Port 1–65535"))
        cfg.update({"host": host, "udp_port": port})

    # ------------------------------------------------------------ sending
    def _values(self, bid: str, percent: float) -> dict:
        hue, sat = self.hs.get(bid, (0.0, 0.0))
        return values(percent, self.rgb.get(bid, (1.0, 1.0, 1.0)), hue, sat, self.kelvin.get(bid, 4000))

    async def _send(self, bulb, key: str, percent: float) -> None:
        cfg = bulb.config
        await getattr(self, f"_send_{self.transport(cfg)}")(bulb, str(cfg.get(key) or ""),
                                                          self._values(bulb.id, percent))

    async def _send_command(self, bulb, template: str, vals: dict) -> None:
        argv = [arg.format_map(vals) for arg in shlex.split(template)]
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise TimeoutError(f"{bulb.id}: {argv[0]} took over {TIMEOUT_S:g} s") from None
        if proc.returncode:
            text = " ".join((err or b"").decode(errors="replace").split())
            if len(text) > STDERR_CHARS:
                text = text[:STDERR_CHARS] + "…"
            raise RuntimeError(f"{bulb.id}: {argv[0]} exit {proc.returncode}" + (f": {text}" if text else ""))

    async def _send_http(self, bulb, template: str, vals: dict) -> None:
        import aiohttp
        cfg = bulb.config
        method = str(cfg.get("method") or "POST").upper()
        url, body = str(cfg.get("url") or "").format_map(vals), template.format_map(vals)
        if method == "GET":
            url, body = url + body, None
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
        async with self.session.request(method, url, data=None if body is None else body.encode(),
                                        headers=parse_headers(str(cfg.get("headers") or ""))) as resp:
            if not 200 <= resp.status < 300:
                raise RuntimeError(f"{bulb.id}: HTTP {resp.status} {resp.reason or ''}".rstrip())

    @staticmethod
    def _broker_key(cfg: dict) -> tuple:
        return (str(cfg.get("broker") or ""), int(cfg.get("broker_port") or 1883),
                str(cfg.get("username") or ""), str(cfg.get("password") or ""), False)

    def _broker(self, cfg: dict):
        from .mqtt import BrokerLink
        key = self._broker_key(cfg)
        if key not in self.brokers:
            self.brokers[key] = BrokerLink(key, lambda *_: None, self.ctx.changed)
        return self.brokers[key]

    async def _send_mqtt(self, bulb, template: str, vals: dict) -> None:
        link = self._broker(bulb.config)
        await link.wait_ready()
        await link.publish(str(bulb.config.get("topic") or "").format_map(vals), template.format_map(vals))

    async def _send_serial(self, bulb, template: str, vals: dict) -> None:
        from .serial import _open_port, is_url
        cfg = bulb.config
        port = str(cfg.get("port") or "")
        link = self.ports.get(bulb.id)
        if link is None:
            if not is_url(port) and not os.path.exists(port):
                raise Offline(f"{bulb.id}: {port} is gone")
            try:
                link = _open_port(port, int(cfg.get("baud") or 115200))
            except OSError as exc:
                if not is_url(port) and not os.path.exists(port):
                    raise Offline(f"{bulb.id}: {port} is gone") from exc
                raise
            self.ports[bulb.id] = link
        try:
            link.write(unescape(template.format_map(vals)))
        except OSError as exc:  # unplugged
            self._close_port(bulb.id)
            if not is_url(port) and not os.path.exists(port):
                raise Offline(f"{bulb.id}: {port} is gone") from exc
            raise

    def _close_port(self, bid: str) -> None:
        link = self.ports.pop(bid, None)
        if link is not None:
            with contextlib.suppress(Exception):
                link.close()

    async def _send_udp(self, bulb, template: str, vals: dict) -> None:
        cfg = bulb.config
        host, port = str(cfg.get("host") or ""), int(cfg.get("udp_port") or 0)
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        family, _type, proto, _name, addr = infos[0]
        with socket.socket(family, socket.SOCK_DGRAM, proto) as sock:
            sock.setblocking(False)
            sock.sendto(unescape(template.format_map(vals)), addr)

    # ------------------------------------------------------------ the API
    async def apply(self, bulb, percent: float) -> None:
        cfg, bid = bulb.config, bulb.id
        if percent <= 0:
            await self._send(bulb, "off" if cfg.get("off") else "brightness", 0)
            self.lit[bid] = False
            self.level[bid] = 0
            return
        if cfg.get("on") and self.lit.get(bid) is not True:
            await self._send(bulb, "on", percent)
        self.lit[bid] = True
        if cfg.get("brightness"):
            await self._send(bulb, "brightness", percent)
        self.level[bid] = percent

    async def apply_color(self, bulb, color: dict) -> bool:
        cfg, bid = bulb.config, bulb.id
        if color.get("mode") == "hs" and cfg.get("colour"):
            hue, sat = float(color["hue"]) % 360, float(color["sat"])
            self.hs[bid] = (hue, sat)
            self.rgb[bid] = colorsys.hsv_to_rgb(hue / 360.0, sat / 100.0, 1.0)
            await self._send(bulb, "colour", self.level.get(bid, 100.0))
            return True
        if color.get("mode") == "ct" and cfg.get("white"):
            lo, hi = self.ct_range(bulb)
            self.kelvin[bid] = max(lo, min(hi, int(color["kelvin"])))
            await self._send(bulb, "white", self.level.get(bid, 100.0))
            return True
        return False

    async def resume(self) -> None:
        self.lit.clear()

    async def remove(self, bulb) -> None:
        self._close_port(bulb.id)
        for d in (self.lit, self.level, self.rgb, self.hs, self.kelvin):
            d.pop(bulb.id, None)
        key = self._broker_key(bulb.config)
        if key in self.brokers and not any(
                b is not bulb and self.transport(b.config) == "mqtt" and self._broker_key(b.config) == key
                for b in self.ctx.bulbs()):
            await self.brokers.pop(key).close()

    async def stop(self) -> None:
        for bid in list(self.ports):
            self._close_port(bid)
        for link in self.brokers.values():
            await link.close()
        self.brokers.clear()
        if self.session is not None:
            await self.session.close()
            self.session = None
