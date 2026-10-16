"""Helpers shared by the LAN backends (kasa, tuya, shelly, tasmota, esphome).

Not a backend: the registry skips modules whose name starts with "_".
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("hauslicht")


class Throttle:
    """At most one send per `interval` seconds per device. The core never calls
    apply concurrently within a backend and keeps only the latest want while
    apply runs, so waiting here is what coalesces a burst of screen changes:
    the next call carries the newest value, and the last one is always sent."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: dict[str, float] = {}

    async def wait(self, key: str) -> None:
        delay = self._last.get(key, 0.0) + self.interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def sent(self, key: str) -> None:
        self._last[key] = time.monotonic()

    def forget(self, key: str) -> None:
        self._last.pop(key, None)


class Presence:
    """online / last-seen bookkeeping for polled devices, with uptime-based
    power-return detection (a device whose uptime restarted came back from a
    power cut; the outage is estimated as boot time minus last contact)."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.online: dict[str, bool | None] = {}
        self.seen: dict[str, float] = {}     # bulb id -> wall time of last contact
        self.uptime: dict[str, float] = {}   # bulb id -> last reported uptime (s)

    def get(self, bulb_id: str) -> bool | None:
        return self.online.get(bulb_id)

    def lost(self, bulb_id: str) -> None:
        if self.online.get(bulb_id) is not False:
            self.online[bulb_id] = False
            self.ctx.changed()

    def contact(self, bulb_id: str, uptime: float | None = None) -> None:
        """A successful exchange; `uptime` (seconds) when the device reported it."""
        now = time.time()
        first = bulb_id not in self.seen
        before = self.online.get(bulb_id)
        returned_gap = None
        rebooted = False
        if uptime is not None:
            boot = now - uptime
            old = self.uptime.get(bulb_id)
            if old is not None and uptime < old:  # uptime restarted: the device rebooted
                rebooted = True
                returned_gap = max(0.0, boot - self.seen.get(bulb_id, boot))
            self.uptime[bulb_id] = uptime
        self.seen[bulb_id] = now
        if rebooted and not first:
            # the core notes the power before the outage when it sees the
            # device away, so a reboot between two polls is reported as a
            # loss first (as mock.simulate_return does)
            if before is not False:
                self.online[bulb_id] = False
                self.ctx.changed()
            log.info("%s came back after %.0f s without power", bulb_id, returned_gap)
            self.ctx.power_returned(bulb_id, returned_gap)
        elif first:
            self.ctx.power_returned(bulb_id, None)
        if self.online.get(bulb_id) is not True:
            self.online[bulb_id] = True
            self.ctx.changed()

    def forget(self, bulb_id: str) -> None:
        for d in (self.online, self.seen, self.uptime):
            d.pop(bulb_id, None)


async def mdns_browse(types: list[str], timeout: float) -> list[dict]:
    """Browse mDNS service types for `timeout` seconds:
    [{type, name, server, addresses, port, properties}]. Needs zeroconf
    (comes with aioesphomeapi); without it, nothing is found."""
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        log.info("mDNS discovery needs zeroconf: pip install zeroconf")
        return []
    found: dict[tuple[str, str], None] = {}

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            found[(service_type, name)] = None

    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, types, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        out = []
        for service_type, name in list(found):
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(azc.zeroconf, 1500):
                continue
            props = {}
            for k, v in (info.properties or {}).items():
                try:
                    props[k.decode()] = v.decode() if isinstance(v, bytes) else v
                except (AttributeError, UnicodeDecodeError):
                    continue
            out.append({"type": service_type, "name": name.removesuffix("." + service_type),
                        "server": (info.server or "").rstrip("."),
                        "addresses": info.parsed_addresses(), "port": info.port,
                        "properties": props})
        return out
    finally:
        await browser.async_cancel()
        await azc.async_close()


async def poll_loop(interval: float, bulbs, probe) -> None:
    """Call `await probe(bulb)` for each bulb every `interval` seconds; errors
    are the probe's to record (it marks the bulb lost)."""
    while True:
        for bulb in list(bulbs()):
            try:
                await probe(bulb)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a poll must never end the loop
                log.debug("poll %s: %s", bulb.id, exc)
        await asyncio.sleep(interval)


def mdns_host(svc: dict) -> str | None:
    """host[:port] of a browsed service (IPv4 first), port only when not 80."""
    addrs = sorted(svc.get("addresses") or [], key=lambda a: ":" in a)
    if not addrs:
        return None
    addr = addrs[0] if ":" not in addrs[0] else f"[{addrs[0]}]"
    port = svc.get("port")
    return addr if port in (None, 80) else f"{addr}:{port}"
