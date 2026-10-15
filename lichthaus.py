#!/usr/bin/env python3
"""lichthaus: control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux.

    lichthaus on | off               switch every bulb (or one: on 0x0002)
    lichthaus dim 40                 brightness in percent (0 switches off)
    lichthaus status                 present state of every bulb
    lichthaus list                   bulbs in lichthaus.json
    lichthaus adopt                  provision a factory-fresh bulb, then set it up
    lichthaus setup                  (re)bind AppKey, OnOff and Lightness on every bulb
    lichthaus sync                   daemon: follow the screen backlight; adds a
                                     "keyboard brightness" slider to Plasma's applet
    lichthaus mode [MODE]            what the slider does: independent | minimum |
                                     maximum (default) | coupling
    lichthaus track [WHEN]           follow the screen: always (default) | night
                                     (only while Night Light is on) | never
    lichthaus screen-state [on|off]  bulbs off while the screen is blanked or the
                                     laptop sleeps, never below 0.5 % while on
    lichthaus lid-state [on|off]     bulbs off while the lid is closed
    lichthaus toggle                 switch the bulbs on/off (the widget's middle click)
    lichthaus slider 60              move the slider from the command line
    lichthaus pause | resume         stop / restart following (daemon running)

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

STATE_PATH = HERE / "lichthaus.json"
LOCK_PATH = HERE / "lichthaus.lock"
SOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "lichthaus.sock"
BACKLIGHT_DIR = Path("/sys/class/backlight")

MODEL_GENERIC_LEVEL_SERVER = 0x1002
MODEL_LIGHTNESS_SETUP_SERVER = 0x1301  # range and default
OP_LIGHT_LIGHTNESS_SET = 0x824C
LIGHTNESS_TRANSITION = 0x02  # 2 steps of 100 ms, close to the app's 250 ms
OP_GENERIC_LEVEL_SET = 0x8206
OP_GENERIC_LEVEL_STATUS = 0x8208
LEVEL_MAX_PERCENT = 50  # Generic Level stops here on the Volkslicht
SEQ_RESERVE = 64  # SEQ numbers written ahead, so a crash never replays one
PROXY_SCAN_S = 20.0
REQUEST_TIMEOUT_S = 3.0

log = logging.getLogger("lichthaus")


# ------------------------------------------------------------- i18n

GERMAN = {
    "lichthaus: control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux.":
        "lichthaus: Ledvance-Volkslicht-Lampen (Bluetooth SIG Mesh) unter Linux steuern.",
    "no {name} yet: run `lichthaus adopt` first": "noch keine {name}: zuerst `lichthaus adopt` ausführen",
    "unknown bulb {which}; known: {known}": "unbekannte Lampe {which}; bekannt: {known}",
    "another lichthaus process holds the mesh link (for adopt/setup: systemctl --user stop lichthaus first)":
        "ein anderer lichthaus-Prozess hält die Mesh-Verbindung "
        "(für adopt/setup zuerst: systemctl --user stop lichthaus)",
    "the sync daemon is not running": "der Sync-Dienst läuft nicht",
    "(no answer: switched off at the wall?)": "(keine Antwort: am Wandschalter aus?)",
    "bulb {addr} adopted. Try: lichthaus dim 30 {addr}": "Lampe {addr} übernommen. Probieren: lichthaus dim 30 {addr}",
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
        sys.exit(_("no {name} yet: run `lichthaus adopt` first").format(name=STATE_PATH.name))
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
        sys.exit(_("another lichthaus process holds the mesh link "
                   "(for adopt/setup: systemctl --user stop lichthaus first)"))
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


class MeshLink:
    """One GATT proxy connection to the mesh; the proxy relays to every bulb."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.transport = p0.LocalTransport()
        self.client = None
        self.bearer = None
        self.pump: BearerPump | None = None
        self.node: MeshNode | None = None
        self.tid = int(time.time()) & 0xFF

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
        # disappeared"), so every attempt scans afresh right before connecting.
        for attempt in range(1, attempts + 1):
            cand, seen = await find_proxy_node(
                self.transport.scanner(), k3(netkey), timeout=PROXY_SCAN_S
            )
            if cand is None:
                raise ConnectionError(
                    f"no bulb of our mesh advertising ({len(seen)} other proxies seen): "
                    "bulbs switched off at the wall, or out of range"
                )
            log.info("connecting to proxy %s", cand.device.address)
            try:
                self.client, self.bearer = await p0.open_gatt_session(
                    self.transport, cand.device, provisioning=False,
                    on_message=dispatch, attempts=1,
                )
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
                    await asyncio.sleep(1.5)
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
            print(f"lichthaus: {exc}", file=sys.stderr)
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
    print("\n" + _("bulb {addr} adopted. Try: lichthaus dim 30 {addr}").format(addr=f"0x{addr:04x}"))
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


# Wall switch: power cut drops the BLE link, power back brings the adverts back.
TOGGLE_WINDOW_S = 2.0  # switched off for less than this: toggle
SWITCH_OVERHEAD_S = 1.5  # link timeout + bulb boot, added to the measured gap
GLITCH_S = 0.8  # adverts back faster than a reboot: the link only hiccupped
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

    NAME = b"lichthaus::kbd_backlight"
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
                "sleep", "Licht.Haus", "switch the bulbs off first", "delay")

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
        self.sleep_guard = SleepGuard(self.before_sleep, self.after_wake)
        self.night = False  # KWin Night Light currently active
        self.link = MeshLink(self.state)
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
        self.power_before_loss = self.power
        log.info("bulb unreachable%s", " (at start)" if long_ago else ": switched off?")
        self.presence_task = asyncio.get_running_loop().create_task(self.await_bulb())

    async def await_bulb(self) -> None:
        """Scan for the bulbs' adverts; their return is the switch going on."""
        from bleak import BleakScanner
        await self.link.close()
        addrs = {d["address"].upper() for d in self.state["devices"].values()}
        seen = asyncio.Event()
        scanner = BleakScanner(detection_callback=lambda dev, _adv: (
            dev.address.upper() in addrs and seen.set()))
        await scanner.start()
        try:
            await seen.wait()
        finally:
            await scanner.stop()
        gap = time.monotonic() - self.lost_at
        if gap < GLITCH_S:
            log.info("bulb back after %.1f s: link hiccup, no change", gap)
        elif gap < TOGGLE_WINDOW_S + SWITCH_OVERHEAD_S:
            self.power = not self.power_before_loss
            log.info("bulb back after %.1f s: quick flick -> %s", gap, "on" if self.power else "off")
        else:
            self.power = True
            log.info("bulb back after %.1f s: switched on", gap)
        self.online, self.paused, self.sent = True, False, None

    async def follow(self) -> None:
        backoff = 5.0
        while True:
            await asyncio.sleep(self.args.interval)
            if self.online is False:
                continue  # await_bulb() is watching for the switch
            if self.link.client is not None and not self.link.connected:
                self.bulb_lost()
                continue
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
            await self.sleep_guard.start()
            try:
                async with server:
                    await self.follow()
            finally:
                night_task.cancel()
                self.slider.close()
                await self.link.close()
                with contextlib.suppress(FileNotFoundError):
                    SOCK_PATH.unlink()
        return 0


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lichthaus", description=_(__doc__.split("\n\n")[0].strip()))
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
    p = sub.add_parser("sync", help=_("daemon: follow the screen, provide the Plasma slider"))
    p.add_argument("--interval", type=float, default=0.3, help=_("seconds between backlight reads"))
    args = parser.parse_args(argv)

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
        return asyncio.run(SyncDaemon(args).run())
    logging.getLogger("btmesh").setLevel(logging.WARNING)
    log.setLevel(logging.WARNING)
    return asyncio.run(cmd_control(args))


if __name__ == "__main__":
    sys.exit(main())
