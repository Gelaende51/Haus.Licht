"""Bluetooth SIG mesh bulbs (Ledvance Volkslicht and standard mesh lights).

One GATT proxy connection (MeshLink) reaches every node. The network keys and
the SEQ counter stay at the top of hauslicht.json, where btmesh's provisioning
script (provision_and_toggle.py) keeps them; a bulb's config holds its unicast
address and lightness scale (100 on Ledvance, 65535 per the spec).

Wall switch: a power cut resets the bulb at once, but the BLE link is only
declared dead after its supervision timeout. The proxy bulb adverts exactly
every ~10 s while connected; an advert off that grid means it rebooted (a
flick): ctx.flick(). A bulb that vanished and came back: ctx.power_returned().
Both only after the bulb's boot count says it really rebooted: a Bluetooth
link that merely dropped looks the same from here (see boot_verdict).

A reboot is told from a power-off by how long the bulb was gone (drop ->
back, QUICK_GAP_S): back within it a flick, later a power-off, back lit. An
off-beat advert with no link to read the boot count on is not decided then:
switching off can make the dying bulb advert once (it stays alive 1-5 s), so
the verdict waits for the return (await_bulb).

Flick mode keeps OnPowerUp armed to the after-toggle state, so after each
reboot the opposite value must be in the bulb before the next flick. The
re-arm path is kept short: an off-beat advert cancels the request in flight
on the dead link (no wait for the lock), the verdict comes from the first
boot-count Get, and on a link after a reboot the OnPowerUp Set goes first:
the level waits for it (held), and so do the Gets that keep the boot-count
reference clear and the link-timeout update. Debug log: advert -> connected
-> verdict -> armed. idle(True) drops the link and stops scanning.
Built on btmesh (MIT, github.com/dasimon135/ha-bluetooth-mesh).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

from . import QUICK_GAP_S, Backend, Offline
from ..common import UserError, _, link_lock, save_state
from ..profiles import FAST_FLICK, LEDVANCE_NAME, mesh_id, migrate

try:  # optional: without btmesh the registry lists mesh as unavailable
    import provision_and_toggle as p0
    from btmesh.access import (
        MODEL_GENERIC_ONOFF_SERVER,
        MODEL_HEALTH_SERVER,
        MODEL_LIGHT_LIGHTNESS_SERVER,
        OP_CONFIG_COMPOSITION_DATA_STATUS,
        OP_CONFIG_MODEL_APP_STATUS,
        OP_GENERIC_ONOFF_STATUS,
        OP_LIGHT_LIGHTNESS_STATUS,
        STATUS_NAMES,
        config_composition_data_get,
        config_model_app_bind,
        encode_opcode,
        generic_onoff_get,
        generic_onoff_set,
        light_lightness_get,
        parse_composition_data_status,
        parse_config_model_app_status,
        parse_generic_onoff_status,
        parse_light_lightness_status,
    )
    from btmesh.bearer import find_proxy_node
    from btmesh.crypto import k3
    from btmesh.node import MeshNode
    from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU
    from btmesh.pump import BearerPump
except ImportError:  # pragma: no cover - reported through `requires`
    p0 = None

log = logging.getLogger("hauslicht")

MODEL_GENERIC_LEVEL_SERVER = 0x1002
MODEL_LIGHTNESS_SETUP_SERVER = 0x1301  # range and default
OP_LIGHT_LIGHTNESS_SET = 0x824C
OP_LIGHT_LIGHTNESS_RANGE_GET = 0x8257
OP_LIGHT_LIGHTNESS_RANGE_STATUS = 0x8258
LIGHTNESS_TRANSITION = 0x02  # 2 steps of 100 ms, close to the app's 250 ms
OP_GENERIC_LEVEL_SET = 0x8206
OP_GENERIC_LEVEL_STATUS = 0x8208
LEVEL_MAX_PERCENT = 50  # Generic Level stops here on the Volkslicht
# Generic Power OnOff (Mesh Model spec 3.1.4 / 7.1): what the bulb does when it
# gets power. Get/Status on the server (0x1006), Set on the setup server (0x1007).
MODEL_GENERIC_POWER_ONOFF_SERVER = 0x1006
MODEL_GENERIC_POWER_ONOFF_SETUP_SERVER = 0x1007
OP_DTT_GET, OP_DTT_SET, OP_DTT_STATUS = 0x820D, 0x820E, 0x8210  # Generic Default Transition Time
OP_ONPOWERUP_GET = 0x8211
OP_ONPOWERUP_STATUS = 0x8212
OP_ONPOWERUP_SET = 0x8213  # acknowledged; 0x8214 is the unacknowledged form
POWERUP_CODES = {"off": 0, "on": 1, "restore": 2}
RAW_WAIT_S = 0.4  # how long `raw` collects replies per payload
SEQ_RESERVE = 64  # SEQ numbers written ahead, so a crash never replays one
PROXY_SCAN_S = 20.0
# A bulb that adverts connects in 0.3-7.3 s here (journal 2026-10-10..17,
# scanner paused); bleak's own 20 s only made a hung attempt (02:23:41, bulb
# up) cost 20 s before a fresh advert connected in 2.6 s.
CONNECT_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 3.0
LEDVANCE_CID = 0x052E
SCALE_QUIRKS = {LEDVANCE_CID: 100}  # company id -> lightness scale, overrides the range read
MESH_PROV_UUID = "00001827-0000-1000-8000-00805f9b34fb"  # unprovisioned beacon service

ADVERT_PERIOD_S = 10.0  # connected, the bulb adverts exactly every ~10 s
GRID_TOLERANCE_S = 0.8  # an advert further off that grid: the bulb rebooted
SETTLED_S = 12.0  # ...but only judge that once the connection has settled
WATCH_S = 0.3
CONN_UPDATE_HELPER = "/usr/local/lib/hauslicht/conn-update"
LINK_TIMEOUT_MS = 1000

# Telink vendor model (company 0x052e): VD_GROUP_G_GET is answered with one
# byte that counts these Gets since the bulb booted (RAM: 01 on the first Get
# after power-up, on across reconnects, wraps ff -> 00 -> 01; other Gets leave
# it alone). Read on every new link, it tells a reboot from a link drop.
OP_VD_GROUP_G_GET = 0xC12E05
OP_VD_GROUP_G_STATUS = 0xC42E05
BOOT_SLACK = 4  # a count may skip this many (lost replies, retries); a fresh boot reads 1..this
BOOT_TIMEOUT_S = 1.5  # the bulb answers in ~0.1 s
BOOT_MISSES = 2  # never answered this often: no counter (not a Telink bulb), stop asking
SAME, REBOOTED, UNKNOWN = "same boot", "rebooted", "unknown"
HOLDUP_S = 5.0  # the supply keeps the chip alive up to ~5 s: an advert later than this after an off-beat one is a bulb up again
ARM_WAIT_S = 1.5  # after a new link, held levels and boot-count clearing wait this long for the core's arm
CONN_UPDATE_WAIT_S = 3.0  # the link-timeout update waits at most this long for the first arm


class LinkAborted(ConnectionError):
    """The request in flight was cancelled: the link is being replaced."""


def lightness_scale(cid: int | None, lightness_range: tuple[int, int] | None) -> int:
    """100 (Ledvance's quirk) or 65535 (the spec): the company quirk table
    first, then Light Lightness Range (Ledvance reads 1-100), else the spec."""
    if cid in SCALE_QUIRKS:
        return SCALE_QUIRKS[cid]
    if lightness_range is not None and 0 < lightness_range[1] <= 100:
        return 100
    return 65535


def boot_verdict(last: int | None, value: int | None) -> str:
    """SAME, REBOOTED or UNKNOWN from the boot count before (`last`) and now.

    A count that went on by 1..BOOT_SLACK is the same boot (the link only
    dropped); 1..BOOT_SLACK otherwise is a fresh boot. Both at once (a
    reference near 0 or the wrap; the backend re-arms it out of there) or
    neither (someone else asked many times) cannot be told: UNKNOWN."""
    if last is None or value is None:
        return UNKNOWN
    continued = 1 <= (value - last) % 256 <= BOOT_SLACK
    fresh = 1 <= value <= BOOT_SLACK
    if continued == fresh:
        return UNKNOWN
    return SAME if continued else REBOOTED


def boot_count_clear(value: int) -> bool:
    """A reference no fresh boot can be mistaken for continuing."""
    return BOOT_SLACK < value <= 255 - BOOT_SLACK


def lightness_of(resp) -> int:
    """Where the bulb is heading: during a fade the present value still lags."""
    status = parse_light_lightness_status(p0._full_payload(resp))
    return status.present_lightness if status.target_lightness is None else status.target_lightness


@functools.cache
def _quick_client():
    """BleakClientWithServiceCache whose connect gives up after CONNECT_TIMEOUT_S."""
    from bleak_retry_connector import BleakClientWithServiceCache

    class QuickConnectClient(BleakClientWithServiceCache):
        async def connect(self, **kwargs):
            kwargs["timeout"] = min(kwargs.get("timeout") or CONNECT_TIMEOUT_S, CONNECT_TIMEOUT_S)
            return await super().connect(**kwargs)

    return QuickConnectClient


def _transport():
    """btmesh's local transport, but reusing the bulb's GATT service table:
    rediscovering it took ~6 s of every reconnect. One attempt per device
    object: after a timed-out connect BlueZ removes it, so the library's own
    retries only fail with "device not found"; MeshLink.connect retries with
    the device of a new advert instead."""

    class CachedTransport(p0.LocalTransport):
        async def connect(self, address_or_device, timeout: float = 30.0):
            if isinstance(address_or_device, str):
                return await super().connect(address_or_device, timeout)
            from bleak_retry_connector import establish_connection
            try:
                return await establish_connection(
                    _quick_client(), address_or_device, address_or_device.address,
                    use_services_cache=True, max_attempts=1)
            except Exception as exc:
                raise p0.BearerError(f"connect to {address_or_device.address} failed: {exc}") from exc

    return CachedTransport()


class MeshLink:
    """One GATT proxy connection to the mesh; the proxy relays to every bulb."""

    def __init__(self, state: dict, save) -> None:
        self.state = state
        self.save = save  # writes the whole state file (SEQ reservations)
        self.transport = _transport()
        self.client = None
        self.bearer = None
        self.pump = None
        self.node = None
        self.tid = int(time.time()) & 0xFF
        self.device_hint = None  # callable(after) -> BLEDevice seen advertising just now (and after `after`), or None
        self.scan_pause = None  # async context manager factory: no scanning while connecting
        self.after_connect = None  # async callable(address), e.g. shorten the link timeout
        self.on_change = None  # callable(): connected or closed
        self.connected_at = 0.0
        self.address: str | None = None  # the proxy's BLE address (upper case)
        self.inflight: set[asyncio.Task] = set()  # mesh requests awaiting an answer (abort cancels them)

    @property
    def connected(self) -> bool:
        return self.client is not None and getattr(self.client, "is_connected", False)

    def _reserve_seq(self, seq: int) -> None:
        if seq + 8 >= self.state["seq"]:
            self.state["seq"] = seq + SEQ_RESERVE
            self.save()

    async def connect(self, attempts: int = 3) -> None:
        netkey = bytes.fromhex(self.state["netkey"])
        session: list = []

        def dispatch(msg_type: int, payload: bytes) -> None:
            if session:
                session[0](msg_type, payload)

        # BlueZ forgets an unpaired device soon after the scan stops ("device
        # disappeared"), so every attempt scans afresh right before connecting,
        # unless a scanner that keeps running (the sync daemon's) just saw it.
        failed_at = 0.0  # a retry waits for an advert after the failure: the old device object may be gone
        for attempt in range(1, attempts + 1):
            if self.device_hint:  # the daemon's scanner runs: wait for it, never scan twice
                deadline = time.monotonic() + PROXY_SCAN_S
                while (device := self.device_hint(failed_at)) is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                if device is None:
                    raise ConnectionError("no bulb of our mesh advertising: "
                                          "bulbs switched off at the wall, or out of range")
            else:
                cand, seen = await find_proxy_node(
                    self.transport.scanner(), k3(netkey), timeout=PROXY_SCAN_S
                )
                if cand is None:
                    raise ConnectionError(
                        f"no bulb of our mesh advertising ({len(seen)} other proxies seen): "
                        "bulbs switched off at the wall, or out of range"
                    )
                device = cand.device
            log.info("connecting to proxy %s", device.address)
            try:
                async with (self.scan_pause() if self.scan_pause else contextlib.nullcontext()):
                    self.client, self.bearer = await p0.open_gatt_session(
                        self.transport, device, provisioning=False,
                        on_message=dispatch, attempts=1,
                    )
                self.connected_at = time.monotonic()
                self.address = device.address.upper()
                if self.after_connect:
                    asyncio.get_running_loop().create_task(self.after_connect(device.address))
                break
            except p0.BearerError as exc:
                if attempt == attempts:
                    raise ConnectionError(str(exc)) from exc
                log.info("connect failed (%s); waiting for a new advert", exc)
                failed_at = time.monotonic()
                if not self.device_hint:
                    await asyncio.sleep(1.0)
        self.pump = BearerPump(self.bearer, MSG_TYPE_NETWORK_PDU)
        self.pump.on_error = lambda exc: log.error("network TX failed: %s", exc)
        start_seq = self.state["seq"]
        self.state["seq"] = start_seq + SEQ_RESERVE
        self.save()

        def send(raw: bytes) -> None:
            self.pump.put(raw)
            self._reserve_seq(self.node.ctx.seq)

        self.node = MeshNode(
            netkey=netkey,
            appkey=bytes.fromhex(self.state["appkey"]),
            iv_index=self.state["iv_index"],
            src_addr=self.state["provisioner_addr"],
            send_network_pdu=send,
            seq=start_seq,
        )
        for addr, dev in self.state["devices"].items():
            self.node.add_device(int(addr, 16), bytes.fromhex(dev["device_key"]))

        def on_message(msg_type: int, payload: bytes) -> None:
            if msg_type == MSG_TYPE_NETWORK_PDU:
                self.node.handle_network_pdu(payload)

        session.append(on_message)
        self.pump.start()
        if self.on_change:
            self.on_change()

    async def close(self) -> None:
        was = self.client is not None
        if self.pump:
            await self.pump.stop()
        if self.bearer:
            await self.bearer.stop()
        if self.client:
            await p0._disconnect_quietly(self.client)
        self.client = self.bearer = self.pump = self.node = None
        if was and self.on_change:
            self.on_change()

    async def ensure(self) -> None:
        if not self.connected:
            await self.close()
            await self.connect()

    def _next_tid(self) -> int:
        self.tid = (self.tid + 1) & 0xFF
        return self.tid

    def abort(self) -> int:
        """Cancel every request in flight (a reboot was seen: the link is dead
        and its requests would only time out). Their callers get LinkAborted
        at once and release the lock. Returns how many were cancelled."""
        cancelled = 0
        for task in list(self.inflight):
            if not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    async def _node_request(self, *args, **kw):
        """node.request as a task that abort() can cancel without cancelling
        the caller (the core's worker must survive it)."""
        task = asyncio.get_running_loop().create_task(self.node.request(*args, **kw))
        self.inflight.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current is not None and current.cancelling()):
                raise LinkAborted("request cancelled: the link is being replaced") from None
            raise
        finally:
            self.inflight.discard(task)

    async def _request(self, addr: int, payload: bytes, opcode: int, **kw):
        try:
            return await self._node_request(
                addr, payload, opcode, timeout=REQUEST_TIMEOUT_S, retries=1, **kw
            )
        except TimeoutError:
            return None

    async def set_onoff(self, addr: int, on: bool) -> int | None:
        resp = await self._request(
            addr, generic_onoff_set(on, self._next_tid()), OP_GENERIC_ONOFF_STATUS
        )
        return None if resp is None else parse_generic_onoff_status(p0._full_payload(resp)).present_onoff

    async def set_level(self, addr: int, percent: float, scale: int = 100) -> float | None:
        """Brightness in percent on a bulb of either lightness scale; the
        answer in percent (None: no answer)."""
        if scale == 100:
            return await self.set_percent(addr, percent)
        value = round(max(0.0, min(100.0, percent)) / 100 * scale)
        payload = (encode_opcode(OP_LIGHT_LIGHTNESS_SET) + value.to_bytes(2, "little")
                   + bytes([self._next_tid(), LIGHTNESS_TRANSITION, 0]))
        resp = await self._request(addr, payload, OP_LIGHT_LIGHTNESS_STATUS)
        return None if resp is None else round(lightness_of(resp) / scale * 100, 2)

    async def set_percent(self, addr: int, percent: float) -> float | None:
        """Light Lightness Set in the Ledvance scale: the value IS the percentage.

        The Ledvance app passes its 1-100 slider value unscaled into the SIG
        Light Lightness Set (YonomiMeshCommandHandler2.changeBrightness ->
        TLMeshEngine.setBrightness -> LightnessSetMessage), and the firmware
        ignores values above that range. Sent in the app's complete form:
        transition 0x02 (200 ms) and no delay.
        """
        percent = max(0.0, min(100.0, percent))
        if percent < LEVEL_MAX_PERCENT:
            return await self.set_half_percent(addr, percent)
        value = round(percent)
        payload = (encode_opcode(OP_LIGHT_LIGHTNESS_SET) + value.to_bytes(2, "little")
                   + bytes([self._next_tid(), LIGHTNESS_TRANSITION, 0]))
        resp = await self._request(addr, payload, OP_LIGHT_LIGHTNESS_STATUS)
        return None if resp is None else lightness_of(resp)

    async def set_half_percent(self, addr: int, percent: float) -> float | None:
        """Generic Level Set: the firmware keeps Level at twice the Lightness
        resolution (-32768 + 2 x percent) but clamps it at +100, i.e. 50 %.
        Below that it gives 0.5 % steps, the only value between off and 1 %."""
        steps = round(percent * 2)
        payload = (encode_opcode(OP_GENERIC_LEVEL_SET) + (steps - 0x8000).to_bytes(2, "little", signed=True)
                   + bytes([self._next_tid(), LIGHTNESS_TRANSITION, 0]))
        resp = await self._request(addr, payload, OP_GENERIC_LEVEL_STATUS)
        if resp is None:
            return None
        params = p0._full_payload(resp)[2:]
        level = params[2:4] if len(params) >= 5 else params[0:2]  # target while fading
        return (int.from_bytes(level, "little", signed=True) + 0x8000) / 2

    async def status(self, addr: int, scale: int = 100) -> dict:
        onoff = await self._request(addr, generic_onoff_get(), OP_GENERIC_ONOFF_STATUS)
        light = await self._request(addr, light_lightness_get(), OP_LIGHT_LIGHTNESS_STATUS)
        percent = None if light is None else lightness_of(light)
        if percent is not None and scale != 100:
            percent = round(percent / scale * 100, 2)
        return {
            "bulb": f"0x{addr:04x}",
            "on": None if onoff is None else bool(parse_generic_onoff_status(p0._full_payload(onoff)).present_onoff),
            "percent": percent,
        }

    async def get_onpowerup(self, addr: int) -> int | None:
        resp = await self._request(addr, encode_opcode(OP_ONPOWERUP_GET), OP_ONPOWERUP_STATUS)
        return None if resp is None or not resp.params else resp.params[0]

    async def set_onpowerup(self, addr: int, value: int) -> int | None:
        """A flash write in the bulb: send only when the value changes."""
        resp = await self._request(addr, encode_opcode(OP_ONPOWERUP_SET) + bytes([value]), OP_ONPOWERUP_STATUS)
        return None if resp is None or not resp.params else resp.params[0]

    async def default_transition(self, addr: int, value: int | None = None) -> int | None:
        """Generic Default Transition Time: read it, or set it (a flash write)."""
        payload = encode_opcode(OP_DTT_GET) if value is None else encode_opcode(OP_DTT_SET) + bytes([value])
        resp = await self._request(addr, payload, OP_DTT_STATUS)
        return None if resp is None or not resp.params else resp.params[0]

    async def boot_count(self, addr: int) -> int | None:
        """The vendor model's count of VD_GROUP_G_GETs since boot (None: no answer)."""
        try:
            resp = await self._node_request(addr, encode_opcode(OP_VD_GROUP_G_GET), OP_VD_GROUP_G_STATUS,
                                            timeout=BOOT_TIMEOUT_S, retries=1)
        except TimeoutError:
            return None
        return resp.params[0] if resp.params else None

    async def lightness_range(self, addr: int) -> tuple[int, int] | None:
        resp = await self._request(addr, encode_opcode(OP_LIGHT_LIGHTNESS_RANGE_GET),
                                   OP_LIGHT_LIGHTNESS_RANGE_STATUS)
        if resp is None or len(resp.params) < 5 or resp.params[0] != 0:
            return None
        return int.from_bytes(resp.params[1:3], "little"), int.from_bytes(resp.params[3:5], "little")

    async def composition(self, addr: int):
        try:
            resp = await self._node_request(addr, config_composition_data_get(0),
                                            OP_CONFIG_COMPOSITION_DATA_STATUS, dev_key=True, timeout=15)
        except TimeoutError:
            return None
        return parse_composition_data_status(p0._full_payload(resp))

    async def bind_model(self, addr: int, model: int) -> None:
        resp = await self._request(
            addr,
            config_model_app_bind(addr, self.state["appkey_index"], model),
            OP_CONFIG_MODEL_APP_STATUS,
            dev_key=True,
        )
        if resp is None:
            raise TimeoutError(f"0x{addr:04x}: no answer to the bind of model 0x{model:04x}")
        status = parse_config_model_app_status(p0._full_payload(resp)).status
        if status != 0:
            raise RuntimeError(
                f"0x{addr:04x}: bind of model 0x{model:04x} failed: {STATUS_NAMES.get(status, status)}"
            )

    async def raw(self, addr: int, payloads: str) -> list[dict]:
        """Exploration aid: send app-keyed access payloads, list every reply.
        node.received is a bounded deque: in a long-running daemon its length
        stops growing, so collect through the callback instead."""
        results = []
        previous = self.node.on_message
        for payload in payloads.split(","):
            replies: list = []

            def collect(m, replies=replies):
                if m.src == addr:
                    replies.append(m)
                if previous:
                    previous(m)

            self.node.on_message = collect
            try:
                self.node.send_access(addr, bytes.fromhex(payload))
                await asyncio.sleep(RAW_WAIT_S)
            finally:
                self.node.on_message = previous
            results.append({"bulb": f"0x{addr:04x}", "sent": payload,
                            "replies": [f"{m.opcode:x}:{m.params.hex()}" for m in replies] or "none"})
        return results


async def shorten_link_timeout(address: str, delay: float = 1.0) -> None:
    """Ask for a 1 s link timeout (the bulb asks ~5 s), so a power cut frees
    the connection at once. Root helper via sudo, installed by setup-root.sh."""
    if not os.path.exists(CONN_UPDATE_HELPER):
        return
    await asyncio.sleep(delay)  # after the bulb's own parameter request
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", CONN_UPDATE_HELPER, address, str(LINK_TIMEOUT_MS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _err = await proc.communicate()
    log.info("link timeout: %s", out.decode().strip() or proc.returncode)


class MeshBackend(Backend):
    kind = "mesh"
    title = "Bluetooth mesh (Ledvance Volkslicht, SIG mesh lights)"
    experimental = False
    requires = ("btmesh[ble]", "bleak", "bleak-retry-connector")
    modules = ("btmesh", "bleak", "bleak_retry_connector")
    autoconnect = False  # adopting provisions the bulb into our mesh: only by hand ("Add bulb…", `hauslicht adopt`)
    fields = ()  # adoption is `hauslicht adopt`; `add mesh unicast=0x0002` re-adds a removed node

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.link: MeshLink | None = None
        self.lock = asyncio.Lock()  # one mesh exchange at a time
        self.online: bool | None = None  # None until the first contact
        self.lost_at = 0.0
        self.lost_long_ago = False
        self.lost_link_since = 0.0
        self.lost_address: str | None = None
        self.presence_task: asyncio.Task | None = None
        self.adverts: dict[str, float] = {}  # BLE address -> last advert
        self.rssi: dict[str, int] = {}  # BLE address -> RSSI of its last advert (dBm)
        self.last_device = None
        self.last_seen: float | None = None
        self.bulb_seen = asyncio.Event()
        self.force_reconnect = False
        self.last_flick_at = 0.0  # when an off-beat advert toggled
        self.dtt_checked: set[int] = set()  # A9: bulbs whose default transition was looked at
        self.advert_period = ADVERT_PERIOD_S
        self.stopping = False  # shutting down: our own disconnect is no flick
        self.read_on: dict[int, float] = {}  # addr -> connected_at of the link OnPowerUp was read on
        self.device_powerup: dict[int, int | None] = {}
        self.bind_tried: set[int] = set()
        self.discovering: dict | None = None
        self.scanner = None
        self.tasks: list[asyncio.Task] = []
        self.boot_link = 0.0  # connected_at of the link the boot counts were read on
        self.boot_counts: dict[int, int] = {}  # addr -> last vendor boot count (kept clear of 1..slack)
        self.boot_verdicts: dict[int, tuple[float, str]] = {}  # addr -> (connected_at, verdict)
        self.boot_misses: dict[int, int] = {}  # addr -> unanswered reads while never answered
        self.boot_read_on: dict[int, float] = {}  # addr -> connected_at of the link the count was last read on
        self.boot_unread: set[int] = set()  # addrs whose reboot was counted without reading the count
        self.clear_on: dict[int, float] = {}  # addr -> connected_at of the link whose boot reference awaits clearing
        self.suspects: dict[int, float] = {}  # addr -> off-beat advert awaiting the boot verdict
        self.back_seen: dict[int, float] = {}  # addr -> first advert HOLDUP_S after its off-beat one (up again)
        self.reports: dict[int, int] = {}  # addr -> flicks/returns reported (an older call's argument is stale)
        self.rearm: dict[int, float] = {}  # addr -> connected_at of a link after a reboot, OnPowerUp not yet sent
        self.held: dict[int, tuple[float, int]] = {}  # addr -> (percent, scale) waiting for the OnPowerUp Set
        self.arm_on = 0.0  # connected_at of the link whose first arming is done (the link-timeout update waits)
        self.advert_at = 0.0  # the advert that started the reconnect (debug timings)
        self.verdict_at: dict[int, float] = {}  # addr -> when the verdict was read (debug timings)
        self.idle_mode = False  # every bulb idle: no link, no scanning

    # ------------------------------------------------------------ helpers

    @staticmethod
    def addr(bulb) -> int:
        return int(bulb.config["unicast"], 16)

    @staticmethod
    def scale(bulb) -> int:
        return int(bulb.config.get("scale", 100))

    def macs(self) -> dict[str, object]:
        """BLE address (upper case) -> bulb, for the mesh bulbs configured."""
        devices = self.ctx.state.get("devices", {})
        out = {}
        for bulb in self.ctx.bulbs():
            dev = devices.get(f"0x{self.addr(bulb):04x}")
            if dev:
                out[dev["address"].upper()] = bulb
        return out

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim", "flick", "powerup"}

    def product_name(self, config: dict) -> str:
        return LEDVANCE_NAME if int(config.get("scale", 100)) == 100 else "Mesh light"

    def step(self, bulb) -> float:
        scale = self.scale(bulb)
        return 1.0 if scale == 100 else 100 / scale  # Ledvance: whole percent (0.5 only as the glow floor, min_lit)

    def min_lit(self, bulb) -> float:
        return 0.5 if self.scale(bulb) == 100 else 1.0

    def details(self, bulb) -> dict:
        """Unicast address, BLE MAC, CID/PID/VID (stored at adoption) and the
        last advert's RSSI."""
        key = f"0x{self.addr(bulb):04x}"
        dev = self.ctx.state.get("devices", {}).get(key) or {}
        mac = str(dev.get("address", "")).upper()
        out = {"model": self.product_name(bulb.config), "address": key, "mac": mac or None,
               "rssi": self.rssi.get(mac)}
        for k in ("cid", "pid"):
            if isinstance(dev.get(k), int):
                out[k] = f"0x{dev[k]:04x}"
        if isinstance(dev.get("vid"), int):
            out["firmware"] = f"0x{dev['vid']:04x}"
        return out

    def status(self, bulb) -> dict:
        mac = next((m for m, b in self.macs().items() if b is bulb), None)
        seen = self.adverts.get(mac) if mac else None
        return {"connected": bool(self.link and self.link.connected), "online": self.online,
                "advert_age": None if seen is None else round(time.monotonic() - seen, 1)}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not all(k in self.ctx.state for k in ("netkey", "appkey", "seq", "devices")):
            log.warning("mesh: no network keys in %s: run `hauslicht adopt`", self.ctx.state_path.name)
            self.online = False
            return
        self.link = MeshLink(self.ctx.state, self.ctx.save)
        self.link.device_hint = self.fresh_device
        # Connecting to a freshly booted bulb while discovery runs took ~11 s;
        # stopping discovery first costs 0-2 s, then the connect takes 0.4-3 s.
        self.link.scan_pause = self.scan_paused
        self.link.after_connect = self.after_connect
        self.link.on_change = self.ctx.changed
        loop = asyncio.get_running_loop()
        self.tasks = [loop.create_task(self.watch_adverts()), loop.create_task(self.watch())]

    async def stop(self) -> None:
        self.stopping = True
        for task in self.tasks + ([self.presence_task] if self.presence_task else []):
            task.cancel()
        if self.link:
            await self.link.close()

    async def resume(self) -> None:
        log.info("mesh: resumed from suspend")
        if self.online is False and self.presence_task is not None and not self.idle_mode:
            # the Bluetooth scan does not survive suspend: watch again
            self.presence_task.cancel()
            self.bulb_seen.clear()  # adverts seen before the sleep tell nothing now
            self.presence_task = asyncio.get_running_loop().create_task(self.await_bulb())
        async with self.lock:
            if self.link:
                await self.link.close()  # the BLE link did not survive the sleep

    async def idle(self, idle: bool) -> None:
        """Every mesh bulb idle (OnPowerUp "on" confirmed, nothing sent): drop
        the link and stop scanning, so no reconnects and no flick watch run.
        False: scan again; the next apply/set_powerup connects."""
        if self.link is None or idle == self.idle_mode:
            return
        if idle:
            self.idle_mode = True
            if self.presence_task is not None:
                self.presence_task.cancel()
                self.presence_task = None
            self.link.abort()
            async with self.lock:
                self.force_reconnect = False
                self.suspects.clear()
                self.back_seen.clear()
                await self.link.close()
                if self.scanner is not None:
                    with contextlib.suppress(Exception):
                        await self.scanner.stop()
            log.info("mesh: every bulb idle: link dropped, scanning stopped")
            return
        async with self.lock:
            self.idle_mode = False
            if self.scanner is not None:
                with contextlib.suppress(Exception):
                    await self.scanner.start()
        if self.online is False and not self.stopping:
            self.bulb_seen.clear()  # the scanner was off: wait for a new advert
            self.presence_task = asyncio.get_running_loop().create_task(self.await_bulb())
        log.info("mesh: a bulb is driven again: scanning")

    # ------------------------------------------------------------ driving

    async def _ensure(self) -> None:
        """Link up, holding the lock; Offline when no bulb of the mesh adverts."""
        if self.link is None or self.online is False:
            raise Offline("mesh unreachable")
        if self.idle_mode:
            raise Offline("mesh idle: no bulb in use")
        if self.force_reconnect:  # the old link is dead, only not timed out yet
            self.force_reconnect = False
            await self.link.close()
        try:
            await self._link_up()
        except ConnectionError as exc:
            if "no bulb of our mesh" in str(exc):  # none advertising: switched off
                self.bulb_lost(long_ago=self.online is None)
                raise Offline(str(exc)) from exc
            await self.link.close()
            raise

    async def _link_up(self, judge=()) -> None:
        """link.ensure(); on a new link read the boot count of the proxy bulb
        (and of the bulbs in `judge`) and keep the verdict against the last one."""
        await self.link.ensure()
        at = self.link.connected_at
        if at == self.boot_link:
            return
        self.boot_link = at
        proxy = self.macs().get(self.link.address or "")
        addrs = {self.addr(b) for b in judge} | ({self.addr(proxy)} if proxy is not None else set())
        for addr in sorted(addrs):
            verdict = await self._read_boot(addr)
            self.boot_verdicts[addr] = (at, verdict)
            if verdict != SAME:  # a reboot (or cannot be told): OnPowerUp goes first on this link
                self.rearm[addr] = at
            self._settle_suspect(addr, verdict)
        self.tasks = [t for t in self.tasks if not t.done()]
        self.tasks.append(asyncio.get_running_loop().create_task(self._after_arm_later(at)))

    def _report(self, addr: int, call, *args) -> None:
        """ctx.flick / ctx.power_returned; calls asked before it carry stale values.
        A reboot counted without its boot count read on this link leaves the
        reference stale: the next read of a fresh count is that same boot."""
        self.reports[addr] = self.reports.get(addr, 0) + 1
        link_at = self.link.connected_at if self.link is not None and self.link.connected else None
        if link_at is None or self.boot_read_on.get(addr) != link_at:
            self.boot_unread.add(addr)
        call(*args)

    def _bulb_at(self, addr: int):
        return next((b for b in self.ctx.bulbs() if self.addr(b) == addr), None)

    def _settle_suspect(self, addr: int, verdict: str) -> None:
        """An off-beat advert of `addr` is waiting for this verdict, read on a
        new link: the same boot is a link drop; a reboot (or when it cannot be
        told) is decided by how long the bulb was gone (_decide), right here,
        so the core's next call already carries the new state."""
        at = self.suspects.pop(addr, None)
        back = self.back_seen.pop(addr, None)
        if at is None or self.stopping:
            return
        bulb = self._bulb_at(addr)
        if bulb is None:
            return
        if verdict == SAME:
            log.info("%s: off-beat advert, but the boot count went on: link drop, no flick", bulb.id)
            return
        if verdict == UNKNOWN:
            log.info("%s: off-beat advert, boot count unknown: counted as a reboot", bulb.id)
        link_at = self.link.connected_at if self.link is not None else time.monotonic()
        self._decide(bulb, at, link_at if back is None else min(back, link_at))

    def _decide(self, bulb, drop: float, back: float) -> None:
        """A reboot after an off-beat advert at `drop`, the bulb up again at
        `back`: within QUICK_GAP_S a flick, later a power-off: back lit. While
        the core holds the bulb as lost it decides itself (power_returned: a
        toggle of the power before the loss, or on)."""
        gap = max(0.0, back - drop)
        addr = self.addr(bulb)
        if self.online is False:
            log.info("%s: up again %.1f s after the off-beat advert", bulb.id, gap)
            self._report(addr, self.ctx.power_returned, bulb.id, gap)
        elif gap <= QUICK_GAP_S:
            self._report(addr, self.ctx.flick, bulb.id)
        else:
            log.info("%s: up again %.1f s after the off-beat advert: a power-off, not a flick", bulb.id, gap)
            self._report(addr, self.ctx.power_returned, bulb.id, gap)

    async def _read_boot(self, addr: int) -> str:
        """Read the boot count and judge it against the last one from this
        first Get. Moving the new reference out of 1..BOOT_SLACK and the wrap
        (a few more Gets) waits until after the OnPowerUp Set (_after_arm)."""
        self.clear_on.pop(addr, None)  # a reference left unclear on an earlier link stays as read
        if self.boot_misses.get(addr, 0) >= BOOT_MISSES:
            return UNKNOWN
        try:
            value = await self.link.boot_count(addr)
        except LinkAborted:
            return UNKNOWN  # no answer to count against the bulb
        except Exception as exc:  # the link died under the read
            log.info("0x%04x: boot count read failed: %s", addr, exc)
            value = None
        last = self.boot_counts.get(addr)
        if value is None:
            if last is None:
                self.boot_misses[addr] = self.boot_misses.get(addr, 0) + 1
                if self.boot_misses[addr] >= BOOT_MISSES:
                    log.info("0x%04x: no boot count (vendor Get unanswered): drops and reboots look alike", addr)
            log.info("0x%04x: boot count unanswered", addr)
            return UNKNOWN
        self.boot_misses.pop(addr, None)
        self.boot_read_on[addr] = self.link.connected_at
        verdict = boot_verdict(last, value)
        if addr in self.boot_unread:
            self.boot_unread.discard(addr)
            if 1 <= value <= BOOT_SLACK:  # 02:23: unanswered on the return's link, the link dropped, then 1
                verdict = SAME
                log.info("0x%04x: first boot count since the reboot already counted", addr)
        self.verdict_at[addr] = time.monotonic()
        self.boot_counts[addr] = value
        if not boot_count_clear(value):
            self.clear_on[addr] = self.link.connected_at
        log.info("0x%04x: boot count %s -> %d: %s", addr, "-" if last is None else last, value, verdict)
        return verdict

    async def _clear_boot(self, addr: int) -> None:
        """Move the boot-count reference out of 1..BOOT_SLACK and the wrap, so
        "continued" and "fresh" can never both fit the next read. Under the lock."""
        self.clear_on.pop(addr, None)
        value = self.boot_counts.get(addr)
        if value is None:
            return
        for _i in range(2 * BOOT_SLACK + 2):
            if boot_count_clear(value):
                break
            try:
                more = await self.link.boot_count(addr)
            except Exception:
                more = None
            if more is None:
                break
            value = more
        self.boot_counts[addr] = value
        log.debug("0x%04x: boot count reference moved to %d", addr, value)

    async def _after_arm(self, addr: int, at: float) -> None:
        """After the OnPowerUp Set on the link `at` (or the wait for it ran
        out): the held level, then the boot-count clearing. Under the lock."""
        link = self.link
        if link is None or not link.connected or link.connected_at != at:
            return
        self.arm_on = at
        if self.rearm.pop(addr, None) is not None:
            verdict_at = self.verdict_at.get(addr, at)
            now = time.monotonic()
            if 0.0 < at - self.advert_at < 60.0:
                log.debug("0x%04x re-arm timings: advert -> connected %.2f s -> verdict %.2f s -> armed %.2f s"
                          " (%.2f s in all)", addr, at - self.advert_at, verdict_at - at, now - verdict_at,
                          now - self.advert_at)
            else:
                log.debug("0x%04x re-arm timings: connected -> verdict %.2f s -> armed %.2f s",
                          addr, verdict_at - at, now - verdict_at)
        held = self.held.pop(addr, None)
        if held is not None:
            try:
                got = await link.set_level(addr, *held)
            except Exception as exc:
                got = None
                log.info("0x%04x: held level not sent: %s", addr, exc)
            if got is None:
                self.held.setdefault(addr, held)  # the next arm or wait sends it
            else:
                log.debug("0x%04x: held level %.1f %% sent after the arm", addr, held[0])
        if self.clear_on.get(addr) == at:
            await self._clear_boot(addr)

    async def _after_arm_later(self, at: float) -> None:
        """A new link's held levels and clearing, should no arm come (the
        core's arm back-off, or a bulb it does not arm)."""
        await asyncio.sleep(ARM_WAIT_S)
        async with self.lock:
            for addr in sorted(set(self.rearm) | set(self.held) | set(self.clear_on)):
                await self._after_arm(addr, at)
            if self.link is not None and self.link.connected_at == at:
                self.arm_on = at

    async def after_connect(self, address: str) -> None:
        """The link-timeout update (an HCI connection update), after the
        first OnPowerUp Set: never in the way of the re-arm."""
        helper, fast = os.path.exists(CONN_UPDATE_HELPER), self.fast_flick()
        if not (helper or fast) or self.link is None:
            return
        at = self.link.connected_at
        deadline = at + CONN_UPDATE_WAIT_S
        while self.arm_on != at and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self.link is None or self.link.connected_at != at or not self.link.connected:
            return
        if helper:
            await shorten_link_timeout(address, delay=max(0.0, at + 1.0 - time.monotonic()))
        proxy = self.macs().get(address.upper())
        if fast and proxy is not None:
            await self.check_transition(self.addr(proxy))

    def fast_flick(self) -> bool:
        return self.ctx.state.get("settings", {}).get("flick") in FAST_FLICK

    async def check_transition(self, addr: int) -> None:
        """A9 (flick mode fast): a bulb that fades by default (Generic Default
        Transition Time not 0) is set to 0, so a flick shows at once. Once per
        bulb and run; unproven on the Volkslicht (see dev/flick-tuning-notes.md)."""
        if addr in self.dtt_checked or self.link is None:
            return
        self.dtt_checked.add(addr)
        async with self.lock:
            got = await self.link.default_transition(addr)
            if got is None:
                log.info("0x%04x: no Default Transition Time answer (A9 not available)", addr)
            elif got == 0:
                log.info("0x%04x: Default Transition Time 0: no fade", addr)
            else:
                new = await self.link.default_transition(addr, 0)
                log.info("0x%04x: Default Transition Time 0x%02x -> %s", addr, got,
                         "0" if new == 0 else f"not set ({new})")

    async def judge_boot(self, bulbs, since: float) -> dict[str, str]:
        """bulb id -> SAME / REBOOTED / UNKNOWN for a link set up after `since`
        (monotonic): connects if needed. UNKNOWN when there is no such link."""
        if self.idle_mode:
            return {bulb.id: UNKNOWN for bulb in bulbs}
        async with self.lock:
            if self.force_reconnect:  # the old link is dead, only not timed out yet
                self.force_reconnect = False
                await self.link.close()
            try:
                await self._link_up(bulbs)
            except Exception as exc:
                log.info("boot check: no link (%s)", exc)
                await self.link.close()
        out = {}
        for bulb in bulbs:
            at, verdict = self.boot_verdicts.get(self.addr(bulb), (0.0, UNKNOWN))
            out[bulb.id] = verdict if at > since else UNKNOWN
        return out

    def _contact(self) -> None:
        if self.online is not True:
            self.online = True
            self.ctx.changed()

    async def apply(self, bulb, percent: float) -> None:
        """On a link after a reboot whose OnPowerUp is not sent yet the level
        is held and goes out right after the Set (_after_arm). A level asked
        before a flick/return was reported is stale: the core sends anew."""
        addr = self.addr(bulb)
        asked = self.reports.get(addr, 0)
        async with self.lock:
            try:
                await self._ensure()
                if self.reports.get(addr, 0) != asked:
                    log.debug("0x%04x: level %.1f %% asked before the flick: skipped", addr, percent)
                    return
                if self.rearm.get(addr) == self.link.connected_at:
                    self.held[addr] = (percent, self.scale(bulb))
                    log.debug("0x%04x: level %.1f %% waits for the OnPowerUp Set", addr, percent)
                    return
                self.held.pop(addr, None)
                got = await self.link.set_level(addr, percent, self.scale(bulb))
            except LinkAborted as exc:
                raise Offline(str(exc)) from exc
            if got is None:
                await self.link.close()
                raise ConnectionError(f"0x{addr:04x}: no answer")
            self._contact()

    async def set_powerup(self, bulb, value: str) -> bool:
        """Read once per link (a flash write in the bulb: Set only on change);
        no Get first when a Set follows anyway (its Status tells the value).
        Then the held level and the boot-count clearing (_after_arm)."""
        want = POWERUP_CODES[value]
        addr = self.addr(bulb)
        asked = self.reports.get(addr, 0)
        async with self.lock:
            try:
                await self._ensure()
                at = self.link.connected_at
                if self.reports.get(addr, 0) != asked and self.device_powerup.get(addr) == want:
                    return True  # asked before the flick; the core asks again with the new target
                ok = await self._arm(addr, want, value, at)
            except LinkAborted as exc:
                raise Offline(str(exc)) from exc
            await self._after_arm(addr, at)
            return ok

    async def _arm(self, addr: int, want: int, value: str, at: float) -> bool:
        known = self.device_powerup.get(addr)
        if self.read_on.get(addr) != at and (known is None or known == want):
            got = await self.read_onpowerup(addr)
            log.info("OnPowerUp read: 0x%04x=%s", addr, got)
            if got is None:
                return False  # unanswered: no Set into silence now
            self.read_on[addr] = at
            self.device_powerup[addr] = got
        self._contact()
        if self.device_powerup.get(addr) == want:
            return True
        got = await self.link.set_onpowerup(addr, want)
        self.device_powerup[addr] = got
        if got is not None:
            self.read_on[addr] = at  # the Status says what the bulb holds
        if got == want:
            log.info("OnPowerUp of 0x%04x set to %d (%s)", addr, want, value)
            return True
        log.warning("OnPowerUp Set on 0x%04x: answer %s", addr, got)
        return False

    async def read_onpowerup(self, addr: int) -> int | None:
        """OnPowerUp Get; when unanswered, bind the AppKey to the Generic Power
        OnOff (setup) server once (bulbs adopted before it was bound) and retry."""
        got = await self.link.get_onpowerup(addr)
        if got is None and addr not in self.bind_tried:
            self.bind_tried.add(addr)
            log.info("0x%04x: no OnPowerUp answer; binding the AppKey to models 0x1006/0x1007", addr)
            try:
                for model in (MODEL_GENERIC_POWER_ONOFF_SERVER, MODEL_GENERIC_POWER_ONOFF_SETUP_SERVER):
                    await self.link.bind_model(addr, model)
                got = await self.link.get_onpowerup(addr)
            except Exception as exc:
                log.warning("0x%04x: bind failed: %s", addr, exc)
        return got

    async def query(self, bulb) -> dict | None:
        async with self.lock:
            await self._ensure()
            return await self.link.status(self.addr(bulb), self.scale(bulb))

    async def raw(self, bulb, payloads: str) -> list[dict]:
        async with self.lock:
            await self._ensure()
            return await self.link.raw(self.addr(bulb), payloads)

    async def add(self, config: dict) -> dict:
        unicast = str(config.get("unicast", ""))
        devices = self.ctx.state.get("devices", {})
        try:
            key = f"0x{int(unicast, 16):04x}"
        except ValueError:
            key = None
        if key is None or key not in devices:
            raise UserError(_("mesh bulbs are adopted with `hauslicht adopt` while the service is stopped"))
        return {"unicast": key, "scale": int(config.get("scale", 100)), "id": key[2:]}

    async def discover(self, timeout: float) -> list[dict]:
        """Unprovisioned beacons (service 0x1827): bulbs `hauslicht adopt` can take."""
        found: dict[str, dict] = {}
        if self.scanner is not None and not self.idle_mode:  # the daemon's scanner runs: never a second one
            self.discovering = found
            try:
                await asyncio.sleep(timeout)
            finally:
                self.discovering = None
        else:
            from bleak import BleakScanner
            async with BleakScanner(detection_callback=lambda d, a: self._record(found, d, a)):
                await asyncio.sleep(timeout)
        return list(found.values())

    @staticmethod
    def _record(found: dict, dev, adv) -> None:
        if MESH_PROV_UUID not in (adv.service_uuids or []):
            return
        data = adv.service_data.get(MESH_PROV_UUID, b"")
        found[dev.address.upper()] = {"kind": "mesh", "address": dev.address.upper(),
                                      "name": dev.name or adv.local_name or "",
                                      "config": {"uuid": data[:16].hex()}}

    # ------------------------------------------------------------ presence and flicks

    async def watch(self) -> None:
        """A link that died (power cut, out of range) means the bulb is lost."""
        while True:
            await asyncio.sleep(WATCH_S)
            if self.online is False or self.link is None or self.stopping or self.idle_mode:
                continue
            if self.link.client is not None and not self.link.connected:
                self.bulb_lost()
            elif self.force_reconnect and not self.lock.locked():
                async with self.lock:
                    if self.force_reconnect:
                        self.force_reconnect = False
                        await self.link.close()

    def bulb_lost(self, long_ago: bool = False) -> None:
        """The proxy bulb vanished: most likely switched off at the wall."""
        if self.online is False:
            return
        self.online = False
        # an off-beat advert still waiting for its verdict was the drop
        self.lost_at = min([time.monotonic(), *self.suspects.values()])
        self.lost_long_ago = long_ago
        self.lost_link_since = self.link.connected_at
        self.lost_address = self.link.address if self.link.client is not None else None
        # requests in flight on the dead link would only time out holding the lock
        if cancelled := self.link.abort():
            log.debug("link lost: %d request(s) cancelled", cancelled)
        self.bulb_seen.clear()
        log.info("bulb unreachable%s", " (at start)" if long_ago else ": switched off?")
        self.presence_task = asyncio.get_running_loop().create_task(self.await_bulb())
        self.ctx.changed()

    async def await_bulb(self) -> None:
        """Wait for the scanner to see the bulbs again: the switch went on, or
        only the Bluetooth link dropped (the boot count tells)."""
        async with self.lock:
            await self.link.close()
        macs = self.macs()
        lost = [macs[self.lost_address]] if self.lost_address in macs else list(macs.values())
        # off-beat adverts with no verdict yet (no link since): decided on this return
        pending = {self.addr(b): self.suspects[self.addr(b)] for b in lost if self.addr(b) in self.suspects}
        # The old link of an off-beat advert already judged on a new link dies
        # shortly after; a link set up after that advert dying is a new drop.
        handled = self.lost_link_since < self.last_flick_at and not self.suspects
        if not (handled and self.fresh_device() is not None):
            # (after a judged off-beat advert the bulb is up already: no need to wait)
            await self._wait_back(pending)
        gap = time.monotonic() - self.lost_at
        if handled:
            log.info("bulb back %.1f s after the drop: its off-beat advert was judged already", gap)
        elif self.lost_long_ago:
            # Never reached since the start: no outage was seen, power stays as saved.
            log.info("bulb reached %.1f s after the start", gap)
            for bulb in lost:
                self.ctx.power_returned(bulb.id, None)
        else:
            log.info("bulb back %.1f s after the drop", gap)
            verdicts = await self.judge_boot(lost, self.lost_at)
            for bulb in lost:
                addr = self.addr(bulb)
                if addr in pending:  # settled on the new link (_settle_suspect), or decided by the advert now
                    if self.suspects.pop(addr, None) is not None:
                        back = self.back_seen.pop(addr, time.monotonic())
                        log.info("%s: off-beat advert, no boot count read: counted as a reboot", bulb.id)
                        self._decide(bulb, pending[addr], back)
                    continue
                verdict = verdicts.get(bulb.id, UNKNOWN)
                if verdict == SAME:
                    log.info("%s: boot count went on: the link dropped, the bulb kept power; no flick", bulb.id)
                    continue
                if verdict == UNKNOWN:
                    log.info("%s: boot count unknown: drop or reboot cannot be told; counted as power back", bulb.id)
                self._report(self.addr(bulb), self.ctx.power_returned, bulb.id, gap)
        self.online = True
        self.ctx.changed()

    async def _wait_back(self, pending: dict[int, float]) -> None:
        """Until the bulb adverts again (bulb_seen: set while it is lost).
        After an off-beat advert only one HOLDUP_S later counts: the dying
        bulb may still advert right after it."""
        while True:
            if pending and any(a in self.back_seen for a in pending):
                return
            await self.bulb_seen.wait()  # cleared when the bulb was lost: an advert during the close counts
            if not pending:
                return
            self.bulb_seen.clear()

    async def verify_flick(self, bulb, at: float) -> None:
        """An off-beat advert: a reboot (a flick, or a power-off whose dying
        bulb adverted) or a link the bulb dropped. Reconnect and read the boot
        count; the new link's first read settles it (_settle_suspect). No
        link: the bulb is without power, and only its return tells a flick
        from a power-off (await_bulb), never a guess now."""
        addr = self.addr(bulb)
        self.suspects.setdefault(addr, at)
        if self.online is not False:  # else the loss is being waited out: await_bulb decides
            await self.judge_boot([bulb], at)
        if addr not in self.suspects or self.stopping or self.idle_mode or self.online is False:
            return
        log.info("%s: off-beat advert, no link since: without power; its return tells flick or power-off", bulb.id)
        self.bulb_lost()

    def offbeat(self, bulb, now: float) -> None:
        """An off-beat advert of the bulb we are connected to: the link is
        dead (a reboot) or the bulb dropped it. Cancel the request in flight
        at once (its caller releases the lock), then reconnect and judge."""
        self.last_flick_at = self.advert_at = now
        self.force_reconnect = True
        self.suspects[self.addr(bulb)] = now
        self.back_seen.pop(self.addr(bulb), None)
        cancelled = self.link.abort()
        if cancelled:
            log.debug("off-beat advert: %d request(s) on the old link cancelled", cancelled)
        self.tasks = [t for t in self.tasks if not t.done()]
        self.tasks.append(asyncio.get_running_loop().create_task(self.verify_flick(bulb, now)))

    def fresh_device(self, after: float = 0.0):
        """A bulb as just seen advertising (and after `after`), for a connect without scanning."""
        if self.last_device is not None and self.last_seen > after and time.monotonic() - self.last_seen < 5.0:
            return self.last_device
        return None

    @contextlib.asynccontextmanager
    async def scan_paused(self):
        """BlueZ sets up connections slowly (10 s) while discovery runs."""
        scanner = self.scanner
        if scanner is not None:
            t0 = time.monotonic()
            with contextlib.suppress(Exception):
                await scanner.stop()
            log.debug("scanner stop took %.2f s", time.monotonic() - t0)
        try:
            yield
        finally:
            if scanner is not None:
                with contextlib.suppress(Exception):
                    await scanner.start()

    def on_advert(self, dev, adv) -> None:
        """The scanner saw an advert (any device)."""
        if self.stopping or self.idle_mode:
            return
        if self.discovering is not None:
            self._record(self.discovering, dev, adv)
        mac = dev.address.upper()
        bulb = self.macs().get(mac)
        if bulb is None:
            return
        now = time.monotonic()
        prev, self.adverts[mac] = self.adverts.get(mac), now
        if isinstance(getattr(adv, "rssi", None), int):
            self.rssi[mac] = adv.rssi
        self.last_device, self.last_seen = dev, now
        addr = self.addr(bulb)
        suspect = self.suspects.get(addr)
        if suspect is not None and now - suspect >= HOLDUP_S:
            self.back_seen.setdefault(addr, now)  # up again after its off-beat advert
        if self.online is False:
            if not self.bulb_seen.is_set():
                self.advert_at = now
            self.bulb_seen.set()
            return
        link = self.link
        if not (link and link.connected and link.address == mac):
            return  # the grid only holds for the bulb we are connected to
        gap = None if prev is None else now - prev
        if gap is not None and 9.0 <= gap <= 11.0:  # learn the connected rhythm
            self.advert_period = 0.8 * self.advert_period + 0.2 * gap
        k = 0 if gap is None else round(gap / self.advert_period)
        off_grid = gap is not None and gap > 0.3 and (
            k == 0 or abs(gap - k * self.advert_period) > GRID_TOLERANCE_S)
        # Only on a link set up after the last flick and settled: right after a
        # reboot the bulb adverts every 2-3 s until it is connected again.
        if (off_grid and link.connected_at > self.last_flick_at
                and now - link.connected_at > SETTLED_S and not self.force_reconnect):
            log.info("off-beat advert %.1f s after the last one (period %.2f s): flick?",
                     gap, self.advert_period)
            self.offbeat(bulb, now)

    async def watch_adverts(self) -> None:
        """One scanner for the daemon's lifetime (two at once make BlueZ answer
        'InProgress'). It sees the bulb come back after a loss, notices an
        off-beat advert while connected (a reboot before the link times out),
        and keeps the device fresh for connecting without another scan."""
        from bleak import BleakScanner

        # DuplicateData: report every advert, or BlueZ drops repeats and the
        # 10 s rhythm shows gaps of 20 s and more.
        self.scanner = BleakScanner(detection_callback=self.on_advert, bluez={"filters": {"DuplicateData": True}})
        await self.scanner.start()
        await asyncio.Event().wait()


# ---------------------------------------------------------------- CLI side (daemon stopped)


def device_addrs(state: dict, which: str | None) -> list[int]:
    """Unicast addresses for `which`: None/all, 0x0002, 0002 or mesh:0002."""
    addrs = sorted(int(k, 16) for k in state.get("devices", {}))
    if which in (None, "all"):
        return addrs
    want = int(which.split(":")[-1], 16)
    if want not in addrs:
        raise SystemExit(_("unknown bulb {which}; known: {known}").format(
            which=which, known=", ".join(f"0x{a:04x}" for a in addrs)))
    return [want]


async def run_command(link: MeshLink, cmd: str, arg: str | None, addrs: list[int], state: dict) -> list:
    """One control command on an open link (no daemon); returns per-bulb results."""
    scales = {int(e["config"]["unicast"], 16): int(e["config"].get("scale", 100))
              for e in state.get("bulbs", {}).values() if e.get("kind") == "mesh" and "unicast" in e.get("config", {})}
    if cmd == "status":
        return [await link.status(a, scales.get(a, 100)) for a in addrs]
    results = []
    for a in addrs:
        if cmd in ("on", "off"):
            got = await link.set_onoff(a, cmd == "on")
            results.append({"bulb": f"0x{a:04x}", "on": None if got is None else bool(got)})
        elif cmd == "raw":
            results += await link.raw(a, arg)
        elif cmd == "dim":
            got = await link.set_level(a, float(arg), scales.get(a, 100))
            results.append({"bulb": f"0x{a:04x}", "percent": got})
    return results


async def direct_control(state_path: Path, cmd: str, arg: str | None, which: str | None) -> list:
    """on/off/dim/status/raw while no daemon runs: connect, send, disconnect."""
    state = json.loads(state_path.read_text())
    with link_lock(state_path):
        link = MeshLink(state, lambda: save_state(state_path, state))
        try:
            await link.connect()
            return await run_command(link, cmd, arg, device_addrs(state, which), state)
        finally:
            await link.close()


async def setup(state: dict, addrs: list[int], state_path: Path) -> None:
    """AppKey Add + OnOff bind (phase0's configure), then every model the node
    lists of Lightness, Level, Health and Power OnOff; detects the lightness
    scale and makes sure each node is a bulb in hauslicht.json."""
    save = lambda: save_state(state_path, state)  # noqa: E731
    migrate(state)
    link = MeshLink(state, save)
    await link.connect()
    try:
        for a in addrs:
            print(f"--- bulb 0x{a:04x}")
            await p0.configure(link.node, a, bytes.fromhex(state["appkey"]), state, link.pump)
            comp = await link.composition(a)
            listed = set(comp.elements[0].sig_models) if comp and comp.elements else None
            for model in (MODEL_LIGHT_LIGHTNESS_SERVER, MODEL_GENERIC_LEVEL_SERVER, MODEL_LIGHTNESS_SETUP_SERVER,
                          MODEL_HEALTH_SERVER, MODEL_GENERIC_POWER_ONOFF_SERVER,
                          MODEL_GENERIC_POWER_ONOFF_SETUP_SERVER):
                if listed is not None and model not in listed:
                    continue
                await link.bind_model(a, model)
                print(f"  model 0x{model:04x} bound")
            key = f"0x{a:04x}"
            state["devices"][key]["models"] = [
                m for m in (MODEL_GENERIC_ONOFF_SERVER, MODEL_LIGHT_LIGHTNESS_SERVER, MODEL_GENERIC_LEVEL_SERVER)
                if listed is None or m in listed]
            cid = comp.cid if comp else None
            if comp:  # shown in Configure → Bulbs (details)
                state["devices"][key].update({"cid": comp.cid, "pid": comp.pid, "vid": comp.vid})
            scale = lightness_scale(cid, await link.lightness_range(a))
            print(f"  company 0x{cid or 0:04x}, lightness scale {scale}")
            bid = mesh_id(a)
            entry = state["bulbs"].get(bid)
            if entry is None:
                state["bulbs"][bid] = {"kind": "mesh",
                                       "name": f"{LEDVANCE_NAME if cid == LEDVANCE_CID else 'Mesh light'} - {a:04x}",
                                       "profile": "default", "power": True,
                                       "config": {"unicast": key, "scale": scale}}
            else:
                entry.setdefault("config", {}).update({"unicast": key, "scale": scale})
            save()
    finally:
        await link.close()


async def adopt(args, state_path: Path) -> int:
    p0.setup_logging(args.verbose)
    p0.SCAN_UNPROVISIONED_S = args.scan
    state = p0.load_or_create_state(state_path)
    migrate(state)
    with link_lock(state_path):
        try:
            addr = await p0.provision(
                SimpleNamespace(device_uuid=None, static_oob=None), state, state_path, p0.LocalTransport()
            )
            # The new bulb advertises Node Identity for about a minute; any
            # bulb already in the mesh can be the proxy meanwhile, and the
            # very first one simply needs the wait.
            for attempt in range(4):
                try:
                    await setup(state, [addr], state_path)
                    break
                except ConnectionError as exc:
                    if attempt == 3:
                        raise
                    print(f"  {exc}; retrying in 20 s")
                    await asyncio.sleep(20)
        except p0.Phase0Failure as failure:
            p0.print_diagnostic(failure.phase, failure, failure.causes)
            return 1
    print("\n" + _("bulb {addr} adopted. Try: hauslicht dim 30 {addr}").format(addr=f"0x{addr:04x}"))
    return 0


async def adverts(state: dict, seconds: float) -> int:
    """Debug aid: every advert of an adopted bulb, with its service data."""
    from bleak import BleakScanner
    addrs = {d["address"].upper() for d in state.get("devices", {}).values()}
    start = time.monotonic()

    def seen(dev, adv) -> None:
        if dev.address.upper() in addrs:
            data = {k[4:8]: v.hex() for k, v in adv.service_data.items()}
            print(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 10) % 10} +{time.monotonic() - start:5.1f}s "
                  f"{dev.address} rssi {adv.rssi} {data}", flush=True)

    async with BleakScanner(detection_callback=seen, bluez={"filters": {"DuplicateData": True}}):
        await asyncio.sleep(seconds)
    return 0
