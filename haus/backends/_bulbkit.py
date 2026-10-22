"""Helpers for the wiz, lifx, yeelight, govee, huebt, ledble, sysled, usblight and serial backends.

Not a backend: the registry skips modules whose name starts with "_".
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
import time

from ..common import GERMAN, TRANSLATION

log = logging.getLogger("hauslicht")

PROBE_S = 3.0  # how often an unreachable LAN bulb is asked again
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}$")


def tr(en: str, de: str) -> str:
    """A message for the user in the daemon's language (common.GERMAN has no
    entries for these modules, and shared files stay untouched)."""
    return de if TRANSLATION is GERMAN and GERMAN else en


def no_access(node: str) -> str:
    """The NeedsAccess message for a device node this user may not open."""
    return tr(f"No access to {node}: the system setup grants it (Grant access…, or: hauslicht setup-root).",
              f"Kein Zugriff auf {node}: die Systemeinrichtung erlaubt ihn (Zugriff erlauben…, "
              "oder: hauslicht setup-root).")


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def mac_suffix(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.lower())


class Throttle:
    """At most one send per `interval` per bulb. The core keeps only the latest
    want while apply runs, so waiting here coalesces a burst: the next call
    carries the newest value, and the last one is always sent."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.last: dict[str, float] = {}

    async def wait(self, key: str, interval: float | None = None) -> None:
        gap = (self.last.get(key, 0.0) + (self.interval if interval is None else interval)) - time.monotonic()
        if gap > 0:
            await asyncio.sleep(gap)
        self.last[key] = time.monotonic()


class Presence:
    """online per bulb for status(): None until the first answer, False after a
    failed exchange. While a bulb is unreachable a probe asks it every
    PROBE_S; its first answer makes it online again (ctx.changed: the core
    re-sends the level). `probe(bulb)` returns None (no answer) or a dict;
    {"uptime": s} in it is reboot evidence: a bulb back with an uptime shorter
    than the outage lost power, which ctx.power_returned reports. Without such
    evidence a return is only `changed`: a laptop's own Wi-Fi drop must not
    toggle lights."""

    def __init__(self, ctx, probe=None, interval: float = PROBE_S) -> None:
        self.ctx = ctx
        self.probe = probe
        self.interval = interval
        self.online: dict[str, bool | None] = {}
        self.lost_at: dict[str, float] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def get(self, bid: str) -> bool | None:
        return self.online.get(bid)

    def ok(self, bulb, uptime: float | None = None) -> None:
        was = self.online.get(bulb.id)
        self.online[bulb.id] = True
        if was is True:
            return
        if was is None:
            self.ctx.power_returned(bulb.id, None)
        else:
            gap = time.monotonic() - self.lost_at.pop(bulb.id, time.monotonic())
            if uptime is not None and uptime < gap + 5.0:
                self.ctx.power_returned(bulb.id, gap)
            else:
                log.info("%s: reachable again after %.1f s", bulb.id, gap)
        self.ctx.changed()

    def lost(self, bulb, why: str = "") -> None:
        if self.online.get(bulb.id) is False:
            return
        self.online[bulb.id] = False
        self.lost_at[bulb.id] = time.monotonic()
        log.info("%s: unreachable%s", bulb.id, f" ({why})" if why else "")
        self.ctx.changed()
        if self.probe is not None and bulb.id not in self.tasks:
            self.tasks[bulb.id] = asyncio.get_running_loop().create_task(self._watch(bulb))

    async def _watch(self, bulb) -> None:
        try:
            while self.online.get(bulb.id) is False:
                await asyncio.sleep(self.interval)
                with contextlib.suppress(Exception):
                    got = await self.probe(bulb)
                    if got is not None and self.online.get(bulb.id) is False:
                        self.ok(bulb, got.get("uptime"))
        finally:
            self.tasks.pop(bulb.id, None)

    def forget(self, bid: str) -> None:
        self.online.pop(bid, None)
        self.lost_at.pop(bid, None)
        task = self.tasks.pop(bid, None)
        if task:
            task.cancel()

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self.tasks.clear()


# ---------------------------------------------------------------- Bluetooth LE

BLE_FIND_S = 5.0  # a short scan for one address, only when BlueZ does not know it


async def ble_device(address: str, timeout: float = BLE_FIND_S):
    """A BLEDevice for `address` without a scanner of our own where possible:
    BlueZ's object tree first (kept fresh by the mesh backend's scanner when it
    runs), then a short scan for that address alone. None: not around. A scan
    BlueZ refuses (another discovery in progress) counts as not found."""
    from bleak_retry_connector import get_device
    with contextlib.suppress(Exception):
        if (dev := await get_device(address)) is not None:
            return dev
    from bleak import BleakScanner
    try:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    except Exception as exc:  # BleakError "InProgress" while the mesh scans, no adapter...
        log.info("BLE lookup of %s: %s", address, exc)
        return None


class ScanRefused(Exception):
    """ble_scan while RADIO_HELD: BlueZ could not scan (no adapter left)."""


# True while flick mode exclusive holds an adapter through the HCI user
# channel (set by the core around discover). BlueZ then has that adapter no
# more: a refused scan is an error, not a reason to list stale BlueZ devices.
RADIO_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar("radio_held", default=False)


async def ble_scan(match, timeout: float) -> list[dict]:
    """Devices whose advert `match(device, adv)` turns into a discover entry
    (or None). Scans for `timeout`; when BlueZ refuses a second discovery (the
    mesh backend's scanner runs), reads the devices BlueZ already knows. With
    RADIO_HELD a refused scan raises ScanRefused."""
    from bleak import BleakScanner
    found: dict[str, dict] = {}

    def seen(dev, adv) -> None:
        if (entry := match(dev, adv)) is not None:
            found[dev.address.upper()] = entry

    try:
        async with BleakScanner(detection_callback=seen):
            await asyncio.sleep(timeout)
    except Exception as exc:
        if RADIO_HELD.get():
            raise ScanRefused(str(exc)) from exc
        log.info("BLE scan refused (%s): listing the devices BlueZ knows", exc)
        await asyncio.sleep(min(timeout, 2.0))
        for dev, adv in await _bluez_known():
            seen(dev, adv)
    return list(found.values())


async def _bluez_known() -> list[tuple]:
    """(device, advert-like) pairs from BlueZ's object tree, no scanning."""
    from types import SimpleNamespace
    try:
        from bleak_retry_connector.bluez import _get_properties, ble_device_from_properties
        props = await _get_properties() or {}
    except Exception:
        return []
    out = []
    for path, ifaces in props.items():
        dev_props = ifaces.get("org.bluez.Device1")
        if not dev_props:
            continue
        dev = ble_device_from_properties(path, dev_props)
        adv = SimpleNamespace(local_name=dev_props.get("Name"), service_uuids=list(dev_props.get("UUIDs", [])),
                              service_data={}, manufacturer_data={}, rssi=dev_props.get("RSSI"))
        out.append((dev, adv))
    return out
