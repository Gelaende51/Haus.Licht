#!/usr/bin/env python3
"""hauslicht: control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux.

    hauslicht on | off               switch every bulb (or one: on 0x0002)
    hauslicht dim 40                 brightness in percent (0 switches off)
    hauslicht status                 present state of every bulb
    hauslicht list                   bulbs in hauslicht.json
    hauslicht adopt                  provision a factory-fresh bulb, then set it up
    hauslicht setup                  (re)bind AppKey, OnOff and Lightness on every bulb
    hauslicht sync                   daemon: follow the screen backlight; adds a
                                     "keyboard brightness" slider to Plasma's applet
    hauslicht mode [MODE]            what the slider does: independent | minimum |
                                     maximum (default) | coupling
    hauslicht track [WHEN]           follow the screen: always (default) | night
                                     (only while Night Light is on) | never
    hauslicht screen-state [on|off]  bulbs off while the screen is blanked or the
                                     laptop sleeps, never below 0.5 % while on
    hauslicht lid-state [on|off]     bulbs off while the lid is closed
    hauslicht toggle                 switch the bulbs on/off (the widget's middle click)
    hauslicht slider 60              move the slider from the command line
    hauslicht pause | resume         stop / restart following (daemon running)

While the daemon runs it owns the Bluetooth link and the mesh sequence number;
the other commands then go to it over a Unix socket instead of connecting.

Built on btmesh (MIT, github.com/dasimon135/ha-bluetooth-mesh); provisioning and
the first configuration step reuse that repo's phase0/provision_and_toggle.py.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import math
import os
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import provision_and_toggle as p0  # noqa: E402
from btmesh.access import (  # noqa: E402
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_HEALTH_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
    OP_CONFIG_MODEL_APP_STATUS,
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_LIGHTNESS_STATUS,
    STATUS_NAMES,
    config_model_app_bind,
    encode_opcode,
    generic_onoff_get,
    generic_onoff_set,
    light_lightness_get,
    parse_config_model_app_status,
    parse_generic_onoff_status,
    parse_light_lightness_status,
)
from btmesh.bearer import find_proxy_node  # noqa: E402
from btmesh.crypto import k3  # noqa: E402
from btmesh.node import MeshNode  # noqa: E402
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU  # noqa: E402
from btmesh.pump import BearerPump  # noqa: E402

STATE_PATH = HERE / "hauslicht.json"
LOCK_PATH = HERE / "hauslicht.lock"
SOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hauslicht.sock"
BACKLIGHT_DIR = Path("/sys/class/backlight")

MODEL_GENERIC_LEVEL_SERVER = 0x1002
MODEL_LIGHTNESS_SETUP_SERVER = 0x1301  # range and default
OP_LIGHT_LIGHTNESS_SET = 0x824C
LIGHTNESS_TRANSITION = 0x02  # 2 steps of 100 ms, close to the app's 250 ms
OP_GENERIC_LEVEL_SET = 0x8206
OP_GENERIC_LEVEL_STATUS = 0x8208
LEVEL_MAX_PERCENT = 50  # Generic Level stops here on the Volkslicht
RAW_WAIT_S = 0.4  # how long `raw` collects replies per payload
SEQ_RESERVE = 64  # SEQ numbers written ahead, so a crash never replays one
PROXY_SCAN_S = 20.0
REQUEST_TIMEOUT_S = 3.0

log = logging.getLogger("hauslicht")


# ------------------------------------------------------------- i18n

GERMAN = {
    "hauslicht: control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux.":
        "hauslicht: Ledvance-Volkslicht-Lampen (Bluetooth SIG Mesh) unter Linux steuern.",
    "no {name} yet: run `hauslicht adopt` first": "noch keine {name}: zuerst `hauslicht adopt` ausführen",
    "unknown bulb {which}; known: {known}": "unbekannte Lampe {which}; bekannt: {known}",
    "another hauslicht process holds the mesh link (for adopt/setup: systemctl --user stop hauslicht first)":
        "ein anderer hauslicht-Prozess hält die Mesh-Verbindung "
        "(für adopt/setup zuerst: systemctl --user stop hauslicht)",
    "the sync daemon is not running": "der Sync-Dienst läuft nicht",
    "(no answer: switched off at the wall?)": "(keine Antwort: am Wandschalter aus?)",
    "bulb {addr} adopted. Try: hauslicht dim 30 {addr}": "Lampe {addr} übernommen. Probieren: hauslicht dim 30 {addr}",
    "no backlight device in /sys/class/backlight": "keine Hintergrundbeleuchtung in /sys/class/backlight",
    "unicast address like 0x0002 (default: all)": "Unicast-Adresse wie 0x0002 (Standard: alle)",
    "send one hex access payload, print replies": "eine Hex-Nachricht senden, Antworten ausgeben",
    "seconds to wait for a fresh bulb": "Sekunden Wartezeit auf eine frische Lampe",
    "how the slider shapes the bulbs (daemon running)": "wie der Regler die Lampen formt (Dienst läuft)",
    "set the slider, 0-100 (daemon running)": "Regler setzen, 0-100 (Dienst läuft)",
    "when the screen drives the bulbs (daemon running)": "wann der Bildschirm die Lampen steuert (Dienst läuft)",
    "bulbs off while the lid is closed": "Lampen aus bei geschlossenem Deckel",
    "switch the bulbs on or off (daemon running)": "Lampen ein- oder ausschalten (Dienst läuft)",
    "bulbs off while the screen is off, never below 0.5 %% while on":
        "Lampen aus bei ausgeschaltetem Bildschirm, sonst nie unter 0,5 %%",
    "print bulb %% against screen %% for a mode": "Lampen-%% über Bildschirm-%% für einen Modus ausgeben",
    "seconds between backlight reads": "Sekunden zwischen zwei Helligkeitsabfragen",
    "switch bulbs on": "Lampen einschalten",
    "switch bulbs off": "Lampen ausschalten",
    "show bulb state": "Zustand der Lampen anzeigen",
    "set brightness in percent, 0 switches off": "Helligkeit in Prozent, 0 schaltet aus",
    "stop following the screen": "dem Bildschirm nicht mehr folgen",
    "follow the screen again": "dem Bildschirm wieder folgen",
    "list adopted bulbs": "übernommene Lampen auflisten",
    "take a factory-fresh bulb into the mesh": "eine zurückgesetzte Lampe ins Mesh aufnehmen",
    "bind keys and models on adopted bulbs again": "Schlüssel und Modelle der Lampen neu binden",
    "daemon: follow the screen, provide the Plasma slider": "Dienst: dem Bildschirm folgen, Plasma-Regler bereitstellen",
}


def _language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value and value not in ("C", "POSIX"):
            return value.split(":")[0][:2]
    return "en"


TRANSLATION = GERMAN if _language() == "de" else {}


def _(text: str) -> str:
    """User-facing string in the user's language (English and German so far)."""
    return TRANSLATION.get(text, text)


# ----------------------------------------------------------------- state


def load_state() -> dict:
    if not STATE_PATH.exists():
        sys.exit(_("no {name} yet: run `hauslicht adopt` first").format(name=STATE_PATH.name))
    return json.loads(STATE_PATH.read_text())


def device_addrs(state: dict, which: str | None) -> list[int]:
    addrs = sorted(int(k, 16) for k in state["devices"])
    if which in (None, "all"):
        return addrs
    want = int(which, 16)
    if want not in addrs:
        sys.exit(_("unknown bulb {which}; known: {known}").format(
            which=which, known=", ".join(f"0x{a:04x}" for a in addrs)))
    return [want]


@contextlib.contextmanager
def link_lock():
    """One process at a time talks to the mesh: they share one SEQ counter."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        sys.exit(_("another hauslicht process holds the mesh link "
                   "(for adopt/setup: systemctl --user stop hauslicht first)"))
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ------------------------------------------------------------------ link


def lightness_of(resp) -> int:
    """Where the bulb is heading: during a fade the present value still lags."""
    status = parse_light_lightness_status(p0._full_payload(resp))
    return status.present_lightness if status.target_lightness is None else status.target_lightness


class CachedTransport(p0.LocalTransport):
    """btmesh's local transport, but reusing the bulb's GATT service table:
    rediscovering it took ~6 s of every reconnect."""

    async def connect(self, address_or_device, timeout: float = 30.0):
        if isinstance(address_or_device, str):
            return await super().connect(address_or_device, timeout)
        from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
        try:
            return await establish_connection(
                BleakClientWithServiceCache, address_or_device, address_or_device.address,
                use_services_cache=True, max_attempts=3)
        except Exception as exc:
            raise p0.BearerError(f"connect to {address_or_device.address} failed: {exc}") from exc


class MeshLink:
    """One GATT proxy connection to the mesh; the proxy relays to every bulb."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.transport = CachedTransport()
        self.client = None
        self.bearer = None
        self.pump: BearerPump | None = None
        self.node: MeshNode | None = None
        self.tid = int(time.time()) & 0xFF
        self.device_hint = None  # callable -> BLEDevice seen advertising just now, or None
        self.scan_pause = None  # async context manager factory: no scanning while connecting
        self.after_connect = None  # async callable(address), e.g. shorten the link timeout
        self.connected_at = 0.0

    @property
    def connected(self) -> bool:
        return self.client is not None and getattr(self.client, "is_connected", False)

    def _reserve_seq(self, seq: int) -> None:
        if seq + 8 >= self.state["seq"]:
            self.state["seq"] = seq + SEQ_RESERVE
            p0.save_state(STATE_PATH, self.state)

    async def connect(self, attempts: int = 3) -> None:
        netkey = bytes.fromhex(self.state["netkey"])
        session: list = []

        def dispatch(msg_type: int, payload: bytes) -> None:
            if session:
                session[0](msg_type, payload)

        # BlueZ forgets an unpaired device soon after the scan stops ("device
        # disappeared"), so every attempt scans afresh right before connecting,
        # unless a scanner that keeps running (the sync daemon's) just saw it.
        for attempt in range(1, attempts + 1):
            if self.device_hint:  # the daemon's scanner runs: wait for it, never scan twice
                deadline = time.monotonic() + PROXY_SCAN_S
                while (device := self.device_hint()) is None and time.monotonic() < deadline:
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
                if self.after_connect:
                    asyncio.get_running_loop().create_task(self.after_connect(device.address))
                break
            except p0.BearerError as exc:
                if attempt == attempts:
                    raise ConnectionError(str(exc)) from exc
                log.info("connect failed (%s); scanning again", exc)
                await asyncio.sleep(1.0)
        self.pump = BearerPump(self.bearer, MSG_TYPE_NETWORK_PDU)
        self.pump.on_error = lambda exc: log.error("network TX failed: %s", exc)
        start_seq = self.state["seq"]
        self.state["seq"] = start_seq + SEQ_RESERVE
        p0.save_state(STATE_PATH, self.state)

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

    async def close(self) -> None:
        if self.pump:
            await self.pump.stop()
        if self.bearer:
            await self.bearer.stop()
        if self.client:
            await p0._disconnect_quietly(self.client)
        self.client = self.bearer = self.pump = self.node = None

    async def ensure(self) -> None:
        if not self.connected:
            await self.close()
            await self.connect()

    def _next_tid(self) -> int:
        self.tid = (self.tid + 1) & 0xFF
        return self.tid

    async def _request(self, addr: int, payload: bytes, opcode: int, **kw):
        try:
            return await self.node.request(
                addr, payload, opcode, timeout=REQUEST_TIMEOUT_S, retries=1, **kw
            )
        except TimeoutError:
            return None

    async def set_onoff(self, addr: int, on: bool) -> int | None:
        resp = await self._request(
            addr, generic_onoff_set(on, self._next_tid()), OP_GENERIC_ONOFF_STATUS
        )
        return None if resp is None else parse_generic_onoff_status(p0._full_payload(resp)).present_onoff

    async def set_percent(self, addr: int, percent: float) -> int | None:
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

    async def status(self, addr: int) -> dict:
        onoff = await self._request(addr, generic_onoff_get(), OP_GENERIC_ONOFF_STATUS)
        light = await self._request(addr, light_lightness_get(), OP_LIGHT_LIGHTNESS_STATUS)
        return {
            "bulb": f"0x{addr:04x}",
            "on": None if onoff is None else bool(parse_generic_onoff_status(p0._full_payload(onoff)).present_onoff),
            "percent": None if light is None else lightness_of(light),
        }

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


# -------------------------------------------------------------- commands


async def run_command(link: MeshLink, cmd: str, arg: str | None, which: str | None) -> list:
    """Execute one control command on an open link; returns per-bulb results."""
    addrs = device_addrs(link.state, which)
    if cmd == "status":
        return [await link.status(a) for a in addrs]
    results = []
    for a in addrs:
        if cmd in ("on", "off"):
            got = await link.set_onoff(a, cmd == "on")
            results.append({"bulb": f"0x{a:04x}", "on": None if got is None else bool(got)})
        elif cmd == "raw":
            # Exploration aid: send one app-keyed access payload, list every reply.
            # node.received is a bounded deque: in a long-running daemon its
            # length stops growing, so collect through the callback instead.
            previous = link.node.on_message
            for payload in arg.split(","):
                replies: list = []

                def collect(m, replies=replies):
                    if m.src == a:
                        replies.append(m)
                    if previous:
                        previous(m)

                link.node.on_message = collect
                try:
                    link.node.send_access(a, bytes.fromhex(payload))
                    await asyncio.sleep(RAW_WAIT_S)
                finally:
                    link.node.on_message = previous
                results.append({"bulb": f"0x{a:04x}", "sent": payload,
                                "replies": [f"{m.opcode:x}:{m.params.hex()}" for m in replies] or "none"})
        elif cmd == "dim":
            got = await link.set_percent(a, float(arg))
            results.append({"bulb": f"0x{a:04x}", "percent": got})
    return results


def print_results(results: list) -> int:
    missing = 0
    for r in results:
        fields = ", ".join(f"{k}={v}" for k, v in r.items() if k != "bulb")
        if any(v is None for v in r.values()):
            missing += 1
            fields += "  " + _("(no answer: switched off at the wall?)")
        print(f"{r['bulb']}: {fields}")
    return 1 if missing else 0


async def via_daemon(request: dict) -> dict | None:
    """Send one request to a running sync daemon; None when none is running."""
    if not SOCK_PATH.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCK_PATH))
    except (ConnectionRefusedError, FileNotFoundError):
        return None
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=30)
    writer.close()
    return json.loads(line)


async def cmd_control(args) -> int:
    request = {"cmd": args.cmd, "arg": getattr(args, "percent", None), "which": args.bulb}
    reply = await via_daemon(request)
    if reply is not None:
        if "error" in reply:
            print(reply["error"], file=sys.stderr)
            return 1
        return print_results(reply["results"])
    if args.cmd in ("pause", "resume", "mode", "track", "screen-state", "lid-state", "toggle", "slider"):
        print(_("the sync daemon is not running"), file=sys.stderr)
        return 1
    state = load_state()
    with link_lock():
        link = MeshLink(state)
        try:
            await link.connect()
            return print_results(await run_command(link, args.cmd, request["arg"], args.bulb))
        except ConnectionError as exc:
            print(f"hauslicht: {exc}", file=sys.stderr)
            return 2
        finally:
            await link.close()


async def cmd_setup(state: dict, addrs: list[int]) -> None:
    """AppKey Add + OnOff bind (phase0's configure) and Lightness bind, per bulb."""
    link = MeshLink(state)
    await link.connect()
    try:
        for a in addrs:
            print(f"--- bulb 0x{a:04x}")
            await p0.configure(link.node, a, bytes.fromhex(state["appkey"]), state, link.pump)
            for model in (MODEL_LIGHT_LIGHTNESS_SERVER, MODEL_GENERIC_LEVEL_SERVER, MODEL_LIGHTNESS_SETUP_SERVER,
                          MODEL_HEALTH_SERVER):
                await link.bind_model(a, model)
                print(f"  model 0x{model:04x} bound")
            state["devices"][f"0x{a:04x}"]["models"] = [
                MODEL_GENERIC_ONOFF_SERVER, MODEL_LIGHT_LIGHTNESS_SERVER, MODEL_GENERIC_LEVEL_SERVER,
            ]
            p0.save_state(STATE_PATH, state)
    finally:
        await link.close()


async def cmd_adopt(args) -> int:
    p0.setup_logging(args.verbose)
    p0.SCAN_UNPROVISIONED_S = args.scan
    state = p0.load_or_create_state(STATE_PATH)
    with link_lock():
        try:
            addr = await p0.provision(
                SimpleNamespace(device_uuid=None, static_oob=None), state, STATE_PATH, p0.LocalTransport()
            )
            # The new bulb advertises Node Identity for about a minute; any
            # bulb already in the mesh can be the proxy meanwhile, and the
            # very first one simply needs the wait.
            for attempt in range(4):
                try:
                    await cmd_setup(state, [addr])
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


# ------------------------------------------------------------------ sync


def find_backlight() -> Path:
    devices = sorted(BACKLIGHT_DIR.iterdir()) if BACKLIGHT_DIR.exists() else []
    if not devices:
        sys.exit(_("no backlight device in /sys/class/backlight"))
    return devices[0]


def read_backlight(dev: Path) -> float:
    return int((dev / "brightness").read_text()) / int((dev / "max_brightness").read_text()) * 100


MODES = ("independent", "minimum", "maximum", "coupling", "off")  # what the keyboard slider moves
# The keyboard slider (k, 0-100) is a remote for one widget control. Brighter k
# always means brighter bulbs, so the window handles and the curve run the
# other way: a handle further left, or a lower curve, lights more.
KBD_BINDING = {"maximum": "range_hi", "minimum": "range_lo", "coupling": "curve", "independent": "level"}


def kbd_to_value(key: str, k: float) -> float:
    if key in ("range_lo", "range_hi"):
        return 200.0 - 3.0 * k
    if key == "curve":
        return 100.0 - 2.0 * k
    return float(k)


def value_to_kbd(key: str, value: float) -> int:
    if key in ("range_lo", "range_hi"):
        return max(0, min(100, round((200.0 - value) / 3.0)))
    if key == "curve":
        return max(0, min(100, round((100.0 - value) / 2.0)))
    return max(0, min(100, round(value)))


# Wall switch: a power cut resets the bulb at once, but the BLE link is only
# declared dead after its supervision timeout (~5 s). A quick flick is over by
# then: the bulb is advertising again the moment the drop is noticed. A reboot
# shows in the vendor status, a counter that restarts at 1 with each boot.
QUICK_GAP_S = 15.0  # back within this after the drop was noticed: a flick, at any speed
ADVERT_PERIOD_S = 10.0  # connected, the bulb adverts exactly every ~10 s
GRID_TOLERANCE_S = 0.8  # an advert further off that grid: the bulb rebooted
SETTLED_S = 12.0  # ...but only judge that once the connection has settled
VENDOR_GET = bytes.fromhex("c12e05")  # Telink VD_GROUP_G_GET, company 0x052e
VENDOR_STATUS = 0xC42E05
TRACKING = ("always", "night", "never")  # when the screen drives the bulbs
NIGHT_POLL_S = 30.0
RANGE_MIN, RANGE_MAX = -100.0, 200.0  # the screen window's scale
DEFAULTS = {"mode": "maximum", "slider": 100, "track": "always", "screen_state": True,
            "lid_state": True, "range_lo": 0.0, "range_hi": 100.0, "curve": 0.0, "level": 50.0}
PRESET_KEYS = ("mode", "track", "screen_state", "lid_state", "range_lo", "range_hi", "curve", "level")


def screen_window(screen: float, lo: float, hi: float) -> float:
    """Screen % mapped through the window [lo, hi] (scale -100..200) onto 0..1:
    lo gives an off bulb, hi a full one; 0..100 is 1:1."""
    if hi <= lo:
        return 1.0 if screen >= hi else 0.0
    return max(0.0, min(1.0, (screen - lo) / (hi - lo)))


def curved(x: float, curve: float) -> float:
    """curve -100..100: 0 linear, positive rises slowly from the dark end
    (x^4 at 100), negative rises fast (x^0.25 at -100)."""
    return x ** (4 ** (curve / 100))


def pipeline(st: dict, screen: float, tracking: bool) -> float:
    """Bulb % from the screen %: window, then curve; the level when not tracking."""
    if not tracking or st["mode"] == "independent":
        return max(0.0, min(100.0, float(st["level"])))
    return curved(screen_window(screen, st["range_lo"], st["range_hi"]), st["curve"]) * 100


def lid_closed() -> bool:
    try:
        return any("closed" in f.read_text() for f in Path("/proc/acpi/button/lid").glob("*/state"))
    except OSError:
        return False


class KbdSlider:
    """Shows up as the keyboard backlight in Plasma's brightness applet.

    The kernel's uleds driver creates an LED class device from user space;
    UPower takes any LED named *kbd_backlight as the keyboard backlight and
    PowerDevil draws its slider. Moving the slider writes the LED, which
    arrives here as a 4-byte int on /dev/uleds.
    """

    NAME = b"hauslicht::kbd_backlight"
    MAX = 100

    def __init__(self, on_change) -> None:
        self.on_change = on_change
        self.fd: int | None = None
        self.ready = False  # the new LED reads 0 until the saved value is restored

    def open(self) -> bool:
        try:
            self.fd = os.open("/dev/uleds", os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            log.warning("no keyboard slider (%s): run install/setup-root.sh once", exc)
            return False
        os.write(self.fd, self.NAME.ljust(64, b"\0") + struct.pack("i", self.MAX))
        asyncio.get_running_loop().add_reader(self.fd, self._readable)
        return True

    def _readable(self) -> None:
        with contextlib.suppress(BlockingIOError):
            data = os.read(self.fd, 4)
            if len(data) == 4 and self.ready:
                self.on_change(struct.unpack("i", data)[0])

    async def restore(self, value: int, timeout: float = 30.0) -> None:
        """Wait until UPower has picked the LED up, then set the saved value."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                "busctl", "--system", "call", "org.freedesktop.UPower",
                "/org/freedesktop/UPower/KbdBacklight", "org.freedesktop.UPower.KbdBacklight",
                "GetMaxBrightness", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if out.split()[-1:] == [str(self.MAX).encode()]:
                break
            await asyncio.sleep(1.0)
        else:
            log.warning("UPower did not report the slider within %.0f s", timeout)
        await self.push(value)
        await asyncio.sleep(0.5)  # let the echo of our own write pass
        self.ready = True
        await self.wake_powerdevil()

    @staticmethod
    async def wake_powerdevil() -> None:
        """PowerDevil looks for a keyboard backlight only when it starts; at login
        it usually wins the race against this daemon. Restart it once if so."""
        call = ("busctl", "--user", "call", "org.kde.Solid.PowerManagement",
                "/org/kde/Solid/PowerManagement/Actions/KeyboardBrightnessControl",
                "org.kde.Solid.PowerManagement.Actions.KeyboardBrightnessControl",
                "keyboardBrightnessMax")
        proc = await asyncio.create_subprocess_exec(
            *call, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            log.info("PowerDevil not reachable; no Plasma slider")
            return
        if out.split()[-1:] != [b"0"]:
            return  # the slider is already offered
        log.info("restarting PowerDevil so the brightness applet shows the slider")
        restart = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "plasma-powerdevil.service")
        await restart.wait()

    @staticmethod
    async def push(value: int) -> bool:
        """Set the value through UPower so Plasma's slider moves along."""
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call", "org.freedesktop.UPower",
            "/org/freedesktop/UPower/KbdBacklight", "org.freedesktop.UPower.KbdBacklight",
            "SetBrightness", "i", str(int(value)),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            log.warning("UPower SetBrightness failed: %s", err.decode().strip())
        return proc.returncode == 0

    def close(self) -> None:
        if self.fd is not None:
            asyncio.get_running_loop().remove_reader(self.fd)
            os.close(self.fd)  # the LED device disappears with the fd
            self.fd = None


LOWEST_LIT = 0.5  # the dimmest value the Volkslicht holds (Generic Level step)


def find_panel() -> Path | None:
    """The DRM connector of the built-in screen (eDP, LVDS or DSI)."""
    for pattern in ("card*-eDP-*", "card*-LVDS-*", "card*-DSI-*"):
        found = sorted(Path("/sys/class/drm").glob(pattern))
        if found:
            return found[0]
    return None


def panel_on(panel: Path | None) -> bool:
    """False while the panel is blanked (DPMS off) or switched off (lid closed)."""
    if panel is None:
        return True
    try:
        return ((panel / "dpms").read_text().strip() == "On"
                and (panel / "enabled").read_text().strip() == "enabled")
    except OSError:
        return True


class SleepGuard:
    """Holds a logind delay inhibitor so the bulbs go off before suspend."""

    def __init__(self, before_sleep, after_wake) -> None:
        self.before_sleep, self.after_wake = before_sleep, after_wake
        self.manager = None
        self.fd: int | None = None

    async def start(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
            path = "/org/freedesktop/login1"
            introspection = await bus.introspect("org.freedesktop.login1", path)
            self.manager = bus.get_proxy_object("org.freedesktop.login1", path, introspection) \
                .get_interface("org.freedesktop.login1.Manager")
        except Exception as exc:
            log.warning("no suspend handling (%s)", exc)
            return
        self.manager.on_prepare_for_sleep(self._prepare)
        await self._inhibit()

    async def _inhibit(self) -> None:
        if self.fd is None:
            self.fd = await self.manager.call_inhibit(
                "sleep", "Haus.Licht", "switch the bulbs off first", "delay")

    def _release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _prepare(self, sleeping: bool) -> None:
        asyncio.get_running_loop().create_task(self._on_prepare(sleeping))

    async def _on_prepare(self, sleeping: bool) -> None:
        if sleeping:
            try:
                await asyncio.wait_for(self.before_sleep(), timeout=4.0)
            except Exception as exc:
                log.warning("bulbs not switched off before sleep: %s", exc)
            finally:
                self._release()
        else:
            await self.after_wake()
            await self._inhibit()


CONN_UPDATE_HELPER = "/usr/local/lib/hauslicht/conn-update"
LINK_TIMEOUT_MS = 1000


async def shorten_link_timeout(address: str) -> None:
    """Ask for a 1 s link timeout (the bulb asks ~5 s), so a power cut frees
    the connection at once. Root helper via sudo, installed by setup-root.sh."""
    if not os.path.exists(CONN_UPDATE_HELPER):
        return
    await asyncio.sleep(1.0)  # after the bulb's own parameter request
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", CONN_UPDATE_HELPER, address, str(LINK_TIMEOUT_MS),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    log.info("link timeout: %s", out.decode().strip() or proc.returncode)


class SyncDaemon:
    def __init__(self, args) -> None:
        self.args = args
        self.state = load_state()
        sync = self.state.setdefault("sync", {})
        for key, value in DEFAULTS.items():
            sync.setdefault(key, value)
        self.state.setdefault("presets", {})
        self.panel = find_panel()
        self.screen_on = True
        self.lid_closed = False
        self.power = True  # middle click / on / off / wall switch; not saved
        self.online: bool | None = None  # None until the first contact
        self.lost_at = 0.0
        self.power_before_loss = True
        self.presence_task: asyncio.Task | None = None
        self.return_gap: float | None = None
        self.last_advert: float | None = None
        self.last_device = None
        self.bulb_seen = asyncio.Event()
        self.force_reconnect = False
        self.last_flick_at = 0.0  # when an off-beat advert toggled
        self.advert_period = ADVERT_PERIOD_S
        self.lost_link_since = 0.0
        self.wake = asyncio.Event()  # skip the loop's idle wait
        self.sleep_guard = SleepGuard(self.before_sleep, self.after_wake)
        self.night = False  # KWin Night Light currently active
        self.link = MeshLink(self.state)
        self.link.device_hint = self.fresh_device
        # Connecting to a freshly booted bulb while discovery runs took ~11 s;
        # stopping discovery first costs 0-2 s, then the connect takes 0.4-3 s.
        self.link.scan_pause = self.scan_paused
        self.link.after_connect = shorten_link_timeout
        self.backlight = find_backlight()
        self.slider = KbdSlider(self.slider_moved)
        self.paused = False
        self.sent: float | None = None  # last percent the bulbs confirmed
        self.lock = asyncio.Lock()  # one mesh exchange at a time

    @property
    def settings(self) -> dict:
        return self.state["sync"]

    def save_settings(self) -> None:
        p0.save_state(STATE_PATH, self.state)

    def slider_moved(self, value: int) -> None:
        """The keyboard slider moved: move the widget control the mode binds."""
        self.settings["slider"] = value
        key = KBD_BINDING.get(self.settings["mode"])
        if key is None or value == value_to_kbd(key, self.settings[key]):
            self.save_settings()  # unbound, or the echo of our own push
            return
        log.info("keyboard slider %d -> %s", value, key)
        self.set_setting(key, kbd_to_value(key, value), from_kbd=True)

    async def sync_kbd(self) -> None:
        """Put the keyboard slider where the bound control is."""
        key = KBD_BINDING.get(self.settings["mode"])
        if key is None or not self.slider.ready:
            return
        k = value_to_kbd(key, self.settings[key])
        if k != self.settings["slider"]:
            self.settings["slider"] = k
            await KbdSlider.push(k)

    @property
    def tracking(self) -> bool:
        track = self.settings["track"]
        return track == "always" or (track == "night" and self.night)

    def target(self, screen: float) -> float:
        return pipeline(self.settings, screen, self.tracking)

    def describe(self) -> dict:
        return {"bulb": "sync", "following": not self.paused, **self.settings,
                "night": self.night, "tracking": self.tracking, "screen_on": self.screen_on,
                "lid_closed": self.lid_closed, "power": self.power, "bulbs": self.sent,
                "connected": self.link.connected, "online": self.online,
                "advert_age": None if self.last_advert is None else round(time.monotonic() - self.last_advert, 1),
                "kbd_binds": KBD_BINDING.get(self.settings["mode"]),
                "screen": round(read_backlight(self.backlight), 1),
                "presets": sorted(self.state["presets"])}

    def want(self) -> float | None:
        """What the bulbs should show now; None while a manual command rules."""
        st = self.settings
        self.lid_closed = lid_closed()
        # With the lid closed the panel is off because of the lid: that is
        # lid_state's business, not screen_state's.
        self.screen_on = self.lid_closed or panel_on(self.panel)
        if (st["lid_state"] and self.lid_closed) or (st["screen_state"] and not self.screen_on):
            self.paused = False  # a manual setting ends with the screen
            return 0.0
        if not self.power:
            return 0.0
        if self.paused:
            return None
        want = round(self.target(read_backlight(self.backlight)), 1)
        keeps_glow = not (st["mode"] == "independent" and st["level"] <= 0)
        if st["screen_state"] and keeps_glow and want < LOWEST_LIT:
            want = LOWEST_LIT  # screen on, however dark: keep a glow
        return want

    def set_setting(self, key: str, value, from_kbd: bool = False) -> None:
        """Validate and store one setting (from the widget or the CLI)."""
        if key == "mode" and value not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if key == "track" and value not in TRACKING:
            raise ValueError(f"track must be one of {', '.join(TRACKING)}")
        if key in ("screen_state", "lid_state"):
            value = value in (True, "on", "true", "1", 1)
        elif key in ("range_lo", "range_hi"):
            value = max(RANGE_MIN, min(RANGE_MAX, float(value)))
        elif key == "curve":
            value = max(-100.0, min(100.0, float(value)))
        elif key == "level":
            value = max(0.0, min(100.0, float(value)))
        elif key not in ("mode", "track"):
            raise ValueError(f"unknown setting {key}")
        if key == "range_lo":  # the handles may meet but not cross
            value = min(value, self.settings["range_hi"])
        if key == "range_hi":
            value = max(value, self.settings["range_lo"])
        self.settings[key] = value
        self.save_settings()
        self.paused, self.sent = False, None
        if not from_kbd and (key == "mode" or key == KBD_BINDING.get(self.settings["mode"])):
            asyncio.get_running_loop().create_task(self.sync_kbd())

    async def preset(self, action: str, name: str) -> None:
        presets = self.state["presets"]
        if action == "save":
            presets[name] = {k: self.settings[k] for k in PRESET_KEYS}
        elif action == "delete":
            presets.pop(name, None)
        elif action == "apply":
            for key, value in presets[name].items():
                if key != "slider":
                    self.set_setting(key, value)
            slider = presets[name].get("slider")
            if slider is not None and slider != self.settings["slider"]:
                if not (self.slider.ready and await KbdSlider.push(slider)):
                    self.slider_moved(slider)
        else:
            raise ValueError("preset action must be save, apply or delete")
        self.save_settings()

    async def before_sleep(self) -> None:
        if not (self.settings["screen_state"] or self.settings["lid_state"]):
            return
        if self.online is False:
            return  # no power at the wall: nothing to switch off
        async with self.lock:
            await self.link.ensure()
            for a in device_addrs(self.state, None):
                await self.link.set_percent(a, 0)
        self.sent = 0.0
        log.info("suspend: bulbs off")

    async def after_wake(self) -> None:
        log.info("resumed from suspend")
        if self.online is False and self.presence_task is not None:
            # the Bluetooth scan does not survive suspend: watch again
            self.presence_task.cancel()
            self.presence_task = asyncio.get_running_loop().create_task(self.await_bulb())
        async with self.lock:
            await self.link.close()  # the BLE link did not survive the sleep
        self.sent = None

    async def watch_night_light(self) -> None:
        """KWin's Night Light is active when it runs outside daylight."""
        async def prop(name: str) -> bool | None:
            proc = await asyncio.create_subprocess_exec(
                "busctl", "--user", "get-property", "org.kde.KWin", "/org/kde/KWin/NightLight",
                "org.kde.KWin.NightLight", name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return None if proc.returncode else out.split()[-1:] == [b"true"]
        while True:
            running, daylight = await prop("running"), await prop("daylight")
            night = bool(running) and daylight is False
            if night != self.night:
                log.info("Night Light %s", "active" if night else "inactive")
                self.night = night
            await asyncio.sleep(NIGHT_POLL_S)

    async def handle_client(self, reader, writer) -> None:
        try:
            req = json.loads(await reader.readline())
            if req["cmd"] in ("pause", "resume"):
                self.paused = req["cmd"] == "pause"
                self.sent = None
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "mode":
                if req["arg"]:
                    self.set_setting("mode", req["arg"])
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "track":
                if req["arg"]:
                    self.set_setting("track", req["arg"])
                reply = {"results": [self.describe()]}
            elif req["cmd"] in ("screen-state", "lid-state"):
                if req["arg"]:
                    self.set_setting(req["cmd"].replace("-", "_"), req["arg"])
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "get":
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "set":
                key, _, value = req["arg"].partition("=")
                self.set_setting(key, value)
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "preset":
                action, _, name = req["arg"].partition(" ")
                await self.preset(action, name.strip())
                reply = {"results": [self.describe()]}
            elif req["cmd"] in ("on", "off", "toggle"):
                self.power = (not self.power) if req["cmd"] == "toggle" else req["cmd"] == "on"
                self.paused, self.sent = False, None
                log.info("power %s", "on" if self.power else "off")
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "bulb":
                # The widget's brightness slider: the level while it rules,
                # otherwise a manual setting until `resume`.
                if self.settings["mode"] == "independent" or not self.tracking:
                    self.set_setting("level", req["arg"])
                else:
                    async with self.lock:
                        await self.link.ensure()
                        await run_command(self.link, "dim", req["arg"], None)
                    self.paused, self.sent = True, float(req["arg"])
                reply = {"results": [self.describe()]}
            elif req["cmd"] == "slider":
                value = max(0, min(KbdSlider.MAX, round(float(req["arg"]))))
                if not (self.slider.ready and await KbdSlider.push(value)):
                    self.slider_moved(value)
                reply = {"results": [{**self.describe(), "slider": value}]}
            else:
                async with self.lock:
                    await self.link.ensure()
                    reply = {"results": await run_command(self.link, req["cmd"], req["arg"], req["which"])}
                if req["cmd"] == "dim":
                    self.paused = True  # a manual command wins until `resume`
                    log.info("manual %s: following paused", req["cmd"])
        except SystemExit as exc:
            reply = {"error": str(exc)}
        except Exception as exc:  # report, keep serving
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()

    def bulb_lost(self, long_ago: bool = False) -> None:
        """The proxy bulb vanished: most likely switched off at the wall."""
        if self.online is False:
            return
        self.online = False
        self.lost_at = time.monotonic() - (3600 if long_ago else 0)
        self.lost_link_since = self.link.connected_at  # which link died
        self.power_before_loss = self.power
        log.info("bulb unreachable%s", " (at start)" if long_ago else ": switched off?")
        self.presence_task = asyncio.get_running_loop().create_task(self.await_bulb())

    async def watch_adverts(self) -> None:
        """One scanner for the daemon's lifetime (two at once make BlueZ answer
        'InProgress'). It sees the bulb come back after a loss, notices an
        off-beat advert while connected (a reboot before the link times out),
        and keeps the device fresh for connecting without another scan."""
        from bleak import BleakScanner
        addrs = {d["address"].upper() for d in self.state["devices"].values()}

        def seen(dev, _adv) -> None:
            if dev.address.upper() not in addrs:
                return
            now = time.monotonic()
            prev, self.last_advert = self.last_advert, now
            self.last_device = dev
            if self.online is False:
                self.bulb_seen.set()
                return
            gap = None if prev is None else now - prev
            if gap is not None and 9.0 <= gap <= 11.0:  # learn the connected rhythm
                self.advert_period = 0.8 * self.advert_period + 0.2 * gap
            k = 0 if gap is None else round(gap / self.advert_period)
            off_grid = gap is not None and gap > 0.3 and (
                k == 0 or abs(gap - k * self.advert_period) > GRID_TOLERANCE_S)
            # Only on a link set up after the last flick and settled: right after a
            # reboot the bulb adverts every 2-3 s until it is connected again.
            if (self.link.connected and off_grid and self.link.connected_at > self.last_flick_at
                    and now - self.link.connected_at > SETTLED_S and not self.force_reconnect):
                self.power = not self.power  # toggle now; sent as soon as the link is back
                self.last_flick_at = now
                self.flash()
                log.info("off-beat advert %.1f s after the last one (period %.2f s): flick -> %s",
                         gap, self.advert_period, "on" if self.power else "off")
                self.paused, self.sent = False, None
                self.force_reconnect = True
                self.wake.set()

        # DuplicateData: report every advert, or BlueZ drops repeats and the
        # 10 s rhythm shows gaps of 20 s and more.
        self.scanner = BleakScanner(detection_callback=seen, bluez={"filters": {"DuplicateData": True}})
        await self.scanner.start()
        await asyncio.Event().wait()

    @contextlib.asynccontextmanager
    async def scan_paused(self):
        """BlueZ sets up connections slowly (10 s) while discovery runs."""
        scanner = getattr(self, "scanner", None)
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

    def flash(self) -> None:
        """Debug: show the detected flick on screen at once (HAUSLICHT_FLASH)."""
        if os.environ.get("HAUSLICHT_FLASH"):
            asyncio.get_running_loop().create_task(asyncio.create_subprocess_exec(
                "/usr/bin/python3", str(HERE / "dev" / "flash.py"), "on" if self.power else "off",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))

    def fresh_device(self):
        """The bulb as just seen advertising, for a connect without scanning."""
        if self.last_device is not None and time.monotonic() - self.last_advert < 5.0:
            return self.last_device
        return None

    async def await_bulb(self) -> None:
        """Wait for the watcher to see the bulbs again: the switch went on."""
        await self.link.close()
        # The old link of a flick already toggled by its off-beat advert dies
        # shortly after; a link set up after that flick dying is a new flick.
        recent_flick = self.lost_link_since < self.last_flick_at
        if not (recent_flick and self.fresh_device() is not None):
            # (after an off-beat advert the bulb is up already: no need to wait)
            self.bulb_seen.clear()
            await self.bulb_seen.wait()
        gap = time.monotonic() - self.lost_at
        if recent_flick:
            log.info("bulb back %.1f s after the drop: flick already handled", gap)
        elif gap < QUICK_GAP_S:
            self.power = not self.power_before_loss
            self.flash()
            log.info("bulb back %.1f s after the drop: flick -> %s", gap, "on" if self.power else "off")
        else:
            self.power = True
            log.info("bulb back %.1f s after the drop: switched on", gap)
        self.online, self.paused, self.sent = True, False, None
        self.wake.set()

    async def follow(self) -> None:
        backoff = 5.0
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), self.args.interval)
            self.wake.clear()
            if self.online is False:
                continue  # await_bulb() is watching for the switch
            if self.link.client is not None and not self.link.connected:
                self.bulb_lost()
                continue
            if self.force_reconnect:  # the old link is dead, only not timed out yet
                self.force_reconnect = False
                async with self.lock:
                    await self.link.close()
            want = self.want()
            if want is None or (self.sent is not None and abs(want - self.sent) < 0.25):
                continue
            try:
                async with self.lock:
                    await self.link.ensure()
                    answers = [await self.link.set_percent(a, want) for a in device_addrs(self.state, None)]
                if all(x is None for x in answers):
                    raise ConnectionError("no bulb answered")
                self.sent = want
                backoff = 5.0
                log.info("screen %s -> bulbs %.1f %%", "on" if self.screen_on else "off", want)
            except ConnectionError as exc:
                if "no bulb of our mesh" in str(exc):  # none advertising: switched off
                    self.bulb_lost(long_ago=self.online is None)
                    continue
                log.warning("%s; retrying in %.0f s", exc, backoff)
                await self.link.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
            except Exception as exc:
                log.warning("%s; retrying in %.0f s", exc, backoff)
                await self.link.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
            else:
                self.online = True

    async def run(self) -> int:
        with link_lock():
            with contextlib.suppress(FileNotFoundError):
                SOCK_PATH.unlink()
            server = await asyncio.start_unix_server(self.handle_client, path=str(SOCK_PATH))
            os.chmod(SOCK_PATH, 0o600)
            log.info("following %s, mode %s, slider %s",
                     self.backlight.name, self.settings["mode"], self.settings["slider"])
            key = KBD_BINDING.get(self.settings["mode"])
            if key is not None:  # start where the bound control stands
                self.settings["slider"] = value_to_kbd(key, self.settings[key])
            if self.slider.open():
                asyncio.create_task(self.slider.restore(self.settings["slider"]))
            night_task = asyncio.create_task(self.watch_night_light())
            advert_task = asyncio.create_task(self.watch_adverts())
            await self.sleep_guard.start()
            try:
                async with server:
                    await self.follow()
            finally:
                night_task.cancel()
                advert_task.cancel()
                self.slider.close()
                await self.link.close()
                with contextlib.suppress(FileNotFoundError):
                    SOCK_PATH.unlink()
        return 0


async def cmd_adverts(seconds: float) -> int:
    """Debug aid: every advert of an adopted bulb, with its service data."""
    from bleak import BleakScanner
    addrs = {d["address"].upper() for d in load_state()["devices"].values()}
    start = time.monotonic()

    def seen(dev, adv) -> None:
        if dev.address.upper() in addrs:
            data = {k[4:8]: v.hex() for k, v in adv.service_data.items()}
            print(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 10) % 10} +{time.monotonic() - start:5.1f}s "
                  f"{dev.address} rssi {adv.rssi} {data}", flush=True)

    async with BleakScanner(detection_callback=seen, bluez={"filters": {"DuplicateData": True}}):
        await asyncio.sleep(seconds)
    return 0


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hauslicht", description=_(__doc__.split("\n\n")[0].strip()))
    sub = parser.add_subparsers(dest="cmd", required=True)
    helps = {"on": _("switch bulbs on"), "off": _("switch bulbs off"), "status": _("show bulb state")}
    for name in ("on", "off", "status"):
        p = sub.add_parser(name, help=helps[name])
        p.add_argument("bulb", nargs="?", help=_("unicast address like 0x0002 (default: all)"))
    p = sub.add_parser("raw", help=_("send one hex access payload, print replies"))
    p.add_argument("percent", metavar="hex")
    p.add_argument("bulb", nargs="?")
    for name in ("dim",):
        p = sub.add_parser(name, help=_("set brightness in percent, 0 switches off"))
        p.add_argument("percent", type=float)
        p.add_argument("bulb", nargs="?")
    for name in ("pause", "resume"):
        sub.add_parser(name, help=_("stop following the screen") if name == "pause"
                       else _("follow the screen again")).set_defaults(bulb=None)
    sub.add_parser("list", help=_("list adopted bulbs"))
    p = sub.add_parser("adopt", help=_("take a factory-fresh bulb into the mesh"))
    p.add_argument("--scan", type=float, default=60.0, help=_("seconds to wait for a fresh bulb"))
    p.add_argument("--verbose", action="store_true")
    p = sub.add_parser("setup", help=_("bind keys and models on adopted bulbs again"))
    p.add_argument("bulb", nargs="?")
    p = sub.add_parser("mode", help=_("how the slider shapes the bulbs (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=MODES)
    p.set_defaults(bulb=None)
    p = sub.add_parser("track", help=_("when the screen drives the bulbs (daemon running)"))
    p.add_argument("percent", metavar="when", nargs="?", choices=TRACKING)
    p.set_defaults(bulb=None)
    p = sub.add_parser("screen-state", help=_("bulbs off while the screen is off, never below 0.5 %% while on"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=("on", "off"))
    p.set_defaults(bulb=None)
    p = sub.add_parser("lid-state", help=_("bulbs off while the lid is closed"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=("on", "off"))
    p.set_defaults(bulb=None)
    sub.add_parser("toggle", help=_("switch the bulbs on or off (daemon running)")).set_defaults(bulb=None)
    p = sub.add_parser("slider", help=_("set the slider, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="value", type=float)
    p.set_defaults(bulb=None)
    p = sub.add_parser("adverts", help=_("debug: print the bulbs' adverts with timestamps"))
    p.add_argument("seconds", type=float, nargs="?", default=30.0)
    p = sub.add_parser("sync", help=_("daemon: follow the screen, provide the Plasma slider"))
    p.add_argument("--interval", type=float, default=0.3, help=_("seconds between backlight reads"))
    args = parser.parse_args(argv)

    if args.cmd == "adverts":
        return asyncio.run(cmd_adverts(args.seconds))
    if args.cmd == "list":
        for addr, dev in load_state()["devices"].items():
            print(f"{addr}  {dev['address']}  uuid {dev['uuid']}")
        return 0
    if args.cmd == "adopt":
        return asyncio.run(cmd_adopt(args))
    logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    if args.cmd == "setup":
        state = load_state()
        with link_lock():
            asyncio.run(cmd_setup(state, device_addrs(state, args.bulb)))
        return 0
    if args.cmd == "sync":
        if os.environ.get("HAUSLICHT_DEBUG"):
            logging.getLogger("bleak_retry_connector").setLevel(logging.DEBUG)
            log.setLevel(logging.DEBUG)
        return asyncio.run(SyncDaemon(args).run())
    logging.getLogger("btmesh").setLevel(logging.WARNING)
    log.setLevel(logging.WARNING)
    return asyncio.run(cmd_control(args))


if __name__ == "__main__":
    sys.exit(main())
