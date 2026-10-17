"""Tuya (Smart Life) bulbs, locally, through tinytuya. Experimental.

tinytuya is synchronous: every exchange runs in a worker thread, one at a
time per device, on a kept-open socket (a Tuya device takes one local
connection; the phone app is locked out while we hold it). Each device needs
its id, local key and protocol version (`python -m tinytuya wizard`). Data
points are detected per device: "B" bulbs switch on DP 20, mode DP 21,
brightness DP 22 (10-1000); older "A" bulbs DP 1/2/3 (25-255). No power-on
setting: it is not standard across products.
See docs/backends-research.md#tuya-local.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

from . import Backend, Offline
from ..common import UserError
from ._lan import Presence, Throttle, poll_loop

log = logging.getLogger("hauslicht")

POLL_S = 10.0
TIMEOUT_S = 4
VERSIONS = ["3.1", "3.2", "3.3", "3.4", "3.5"]
# switch, mode, brightness DPs and the brightness range
DPS = {"B": ("20", "21", "22", 10, 1000), "A": ("1", "2", "3", 25, 255)}
NETWORK_ERRORS = {"901", "902", "905"}  # connect, timeout, offline
KEY_ERROR = "914"                       # wrong local key or version


class TuyaBackend(Backend):
    kind = "tuya"
    title = "Tuya / Smart Life (local, needs the local key)"
    experimental = True
    requires = ("tinytuya",)
    fields = (
        {"key": "host", "label": "Host or IP address", "label_de": "Hostname oder IP-Adresse",
         "type": "text", "required": True},
        {"key": "device_id", "label": "Device id", "label_de": "Geräte-ID",
         "type": "text", "required": True},
        {"key": "local_key", "label": "Local key", "label_de": "Lokaler Schlüssel",
         "type": "password", "required": True},
        {"key": "version", "label": "Protocol version", "label_de": "Protokollversion",
         "type": "choice", "required": False, "default": "3.3", "choices": VERSIONS},
        {"key": "bulb_type", "label": "Data points", "label_de": "Datenpunkte",
         "type": "choice", "required": False, "default": "auto", "choices": ["auto", "A", "B"]},
    )

    # discovery: the ports devices broadcast their presence on (tests change them)
    udp_ports = (6666, 6667, 7000)
    udp_bind = ""

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.presence = Presence(ctx)
        self.throttle = Throttle(0.4)
        self.devices: dict[str, object] = {}      # bulb id -> tinytuya.Device
        self.locks: dict[str, asyncio.Lock] = {}
        self.last: dict[str, dict] = {}
        self.poller: asyncio.Task | None = None

    # ------------------------------------------------------------------ device
    @staticmethod
    def _make(config: dict):
        import tinytuya
        dev = tinytuya.Device(config["device_id"], address=config["host"], local_key=config["local_key"],
                              version=float(config.get("version") or 3.3), persist=True,
                              connection_timeout=TIMEOUT_S, connection_retry_limit=1,
                              connection_retry_delay=0)
        port = config.get("port")
        if port:
            dev.port = int(port)
        return dev

    async def _run(self, bulb, fn, *args):
        """fn(*args) in a thread, one at a time per device; tinytuya error
        dicts become exceptions (network ones Offline)."""
        dev = self.devices.get(bulb.id)
        if dev is None:
            dev = self.devices[bulb.id] = self._make(bulb.config)
        lock = self.locks.setdefault(bulb.id, asyncio.Lock())
        async with lock:
            try:
                result = await asyncio.to_thread(getattr(dev, fn), *args)
            except OSError as exc:
                self._drop(bulb.id)
                self.presence.lost(bulb.id)
                raise Offline(f"tuya {bulb.config.get('host')}: {exc}") from exc
        return self._check(bulb, result)

    def _check(self, bulb, result):
        if isinstance(result, dict) and ("Error" in result or "Err" in result):
            err = str(result.get("Err", ""))
            msg = result.get("Error", err)
            if err in NETWORK_ERRORS:
                self._drop(bulb.id)
                self.presence.lost(bulb.id)
                raise Offline(f"tuya {bulb.config.get('host')}: {msg}")
            if err == KEY_ERROR:
                raise UserError(f"Tuya {bulb.config.get('host')}: wrong local key or protocol version")
            raise RuntimeError(f"tuya {bulb.config.get('host')}: {msg}")
        return result

    def _drop(self, bulb_id: str) -> None:
        dev = self.devices.pop(bulb_id, None)
        if dev is not None:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _detect(dps: dict) -> str | None:
        if "22" in dps or "20" in dps:
            return "B"
        if "3" in dps and "1" in dps:
            return "A"
        return None

    def _type(self, bulb) -> str:
        t = bulb.config.get("bulb_type")
        return t if t in DPS else "B"

    # ------------------------------------------------------------------ contract
    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"}

    def step(self, bulb) -> float:
        _, _, _, _, hi = DPS[self._type(bulb)]
        return 100 / hi  # B: 0.1 (10-1000), A: 100/255

    def min_lit(self, bulb) -> float:
        _, _, _, lo, hi = DPS[self._type(bulb)]
        return round(100 * lo / hi, 1)  # B: 1.0, A: 9.8

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
        for bid in list(self.devices):
            self._drop(bid)

    async def suspend(self) -> None:
        if self.poller:
            self.poller.cancel()
            self.poller = None

    async def resume(self) -> None:
        for bid in list(self.devices):
            self._drop(bid)
        await self.start()

    async def remove(self, bulb) -> None:
        self._drop(bulb.id)
        self.presence.forget(bulb.id)
        self.last.pop(bulb.id, None)

    async def apply(self, bulb, percent: float) -> None:
        sw, mode, br, lo, hi = DPS[self._type(bulb)]
        await self.throttle.wait(bulb.id)
        try:
            if percent <= 0:
                await self._run(bulb, "set_value", sw, False)
            else:
                value = max(lo, min(hi, round(percent * hi / 100)))
                await self._run(bulb, "set_multiple_values", {sw: True, mode: "white", br: value})
        finally:
            self.throttle.sent(bulb.id)
        self.last[bulb.id] = {"on": percent > 0, "percent": float(percent)}
        if self.presence.get(bulb.id) is not True:
            self.presence.contact(bulb.id)

    def status(self, bulb) -> dict:
        online = self.presence.get(bulb.id)
        return {"connected": bulb.id in self.devices and bool(online), "online": online}

    async def query(self, bulb) -> dict | None:
        await self._probe(bulb)
        return self.last.get(bulb.id)

    async def _probe(self, bulb) -> None:
        sw, _, br, _, hi = DPS[self._type(bulb)]
        dps = (await self._run(bulb, "status") or {}).get("dps", {})
        if sw in dps:
            on = bool(dps[sw])
            self.last[bulb.id] = {"on": on, "percent": round(100 * int(dps.get(br, hi)) / hi, 1) if on else 0.0}
        self.presence.contact(bulb.id)

    async def discover(self, timeout: float) -> list[dict]:
        """Listen for the devices' own UDP broadcasts (6666 plain, 6667/7000
        encrypted); nothing is sent."""
        return await asyncio.to_thread(self._listen, timeout)

    def _listen(self, timeout: float) -> list[dict]:
        from tinytuya import decrypt_udp
        socks = []
        for port in self.udp_ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.udp_bind, port))
            except OSError as exc:
                log.info("tuya discovery: port %d busy (%s)", port, exc)
                s.close()
                continue
            s.settimeout(0.2)
            socks.append(s)
        found: dict[str, dict] = {}
        end = time.monotonic() + timeout
        try:
            while socks and time.monotonic() < end:
                for s in socks:
                    try:
                        data, addr = s.recvfrom(4096)
                    except (socket.timeout, OSError):
                        continue
                    try:
                        info = json.loads(decrypt_udp(data))
                    except Exception:  # noqa: BLE001 - not a Tuya announcement
                        continue
                    dev_id = info.get("gwId") or info.get("devId")
                    if not dev_id or dev_id in found:
                        continue
                    ip = info.get("ip") or addr[0]
                    version = str(info.get("version") or "3.3")
                    found[dev_id] = {"kind": self.kind, "address": ip, "name": f"Tuya {dev_id}",
                                     "config": {"host": ip, "device_id": dev_id,
                                                "version": version if version in VERSIONS else "3.3"}}
        finally:
            for s in socks:
                s.close()
        return list(found.values())

    async def add(self, config: dict) -> dict:
        for key in ("host", "device_id", "local_key"):
            if not str(config.get(key) or "").strip():
                raise UserError(f"Tuya: {key.replace('_', ' ')} is required")
            config[key] = str(config[key]).strip()
        config.setdefault("version", "3.3")
        probe = type("Probe", (), {"id": "tuya:add", "config": config})()
        try:
            st = await self._run(probe, "status")
        except Offline as exc:
            raise UserError(f"Tuya {config['host']} does not answer: {exc}") from exc
        except RuntimeError as exc:
            raise UserError(str(exc)) from exc
        finally:
            self._drop(probe.id)
            self.presence.forget(probe.id)
        dps = (st or {}).get("dps", {})
        kind = config.get("bulb_type")
        if kind not in DPS:
            kind = self._detect(dps)
            if kind is None:
                raise UserError(f"Tuya {config['host']}: no known bulb data points in {sorted(dps)}; "
                                "pick A (DP 1-3) or B (DP 20-22)")
        config["bulb_type"] = kind
        config["id"] = config["device_id"]
        return config
