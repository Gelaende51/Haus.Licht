"""Flick mode `exclusive`: one Bluetooth adapter taken away from BlueZ.

The root helper install/hci-claim (sudo, set up by install/setup-root.sh)
powers the adapter off in BlueZ, opens its HCI user channel and passes that
socket to the daemon (SCM_RIGHTS over its stdin, a socketpair). The daemon
then drives the controller itself (HciRadio): passive scanning at 100 % with
no duplicate filter, so every advert of the bulbs is seen at once, and mesh
network PDUs sent as non-connectable adverts (AD type 0x2A, the mesh
advertising bearer): no GATT link at all. Closing the channel (mode change,
stop, crash: the kernel closes it with the last copy) hands the adapter back;
the helper then powers it on again in BlueZ.

Packet layout: Bluetooth Core spec Vol 4 Part E (HCI: H4 type byte, opcode
OGF<<10|OCF little endian, parameter length) and Mesh Profile 3.3.1 (the Mesh
Message AD structure). Pure builders and parsers first, then the radio, the
claim and the adapter list.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("hauslicht")

# H4 packet types
CMD_PKT, ACL_PKT, EVT_PKT = 0x01, 0x02, 0x04
# opcodes (OGF << 10 | OCF)
OP_SET_EVENT_MASK = 0x0C01
OP_RESET = 0x0C03
OP_READ_BD_ADDR = 0x1009
OP_LE_SET_EVENT_MASK = 0x2001
OP_LE_SET_ADV_PARAMS = 0x2006
OP_LE_SET_ADV_DATA = 0x2008
OP_LE_SET_ADV_ENABLE = 0x200A
OP_LE_SET_SCAN_PARAMS = 0x200B
OP_LE_SET_SCAN_ENABLE = 0x200C
# events
EV_COMMAND_COMPLETE, EV_COMMAND_STATUS, EV_HARDWARE_ERROR, EV_LE_META = 0x0E, 0x0F, 0x10, 0x3E
LE_ADV_REPORT = 0x02
# the default event mask (0x1FFFFFFFFFFF) lacks LE Meta (bit 61): without it no advert reaches us
EVENT_MASK = 0x00001FFFFFFFFFFF | (1 << 61)
LE_EVENT_MASK = 0x1F  # the default, with LE Advertising Report (bit 1)
# advertising
ADV_IND, ADV_DIRECT_IND, ADV_SCAN_IND, ADV_NONCONN_IND, SCAN_RSP = 0, 1, 2, 3, 4
AD_FLAGS, AD_UUID16_SOME, AD_UUID16_ALL, AD_NAME_SHORT, AD_NAME_FULL, AD_SERVICE_DATA16 = 1, 2, 3, 8, 9, 0x16
AD_MESH_PB_ADV, AD_MESH_MESSAGE, AD_MESH_BEACON = 0x29, 0x2A, 0x2B
ADV_DATA_MAX = 31
MESH_PDU_MAX = ADV_DATA_MAX - 2  # one AD structure: length, type, the network PDU (<= 29 bytes)
# 0.625 ms units. 20 ms is the Core 5.0 minimum for non-connectable adverts;
# 4.x controllers (this RTL8821CE reports HCI 8 = 4.2) refuse it below 100 ms.
ADV_INTERVAL_FAST, ADV_INTERVAL_LEGACY = 0x0020, 0x00A0
# how long each PDU is advertised: three or four events at 20 ms, two at 100 ms
# (each event sends on all three channels); the bulb scans continuously
TX_S = {ADV_INTERVAL_FAST: 0.065, ADV_INTERVAL_LEGACY: 0.115}
SCAN_INTERVAL = SCAN_WINDOW = 0x0030  # 30 ms both: scanning 100 % of the time
COMMAND_TIMEOUT_S = 2.0
STATUS_UNKNOWN_COMMAND, STATUS_UNSUPPORTED, STATUS_INVALID_PARAMS = 0x01, 0x11, 0x12

HELPER = "/usr/local/lib/hauslicht/hci-claim"
CLAIM_TIMEOUT_S = 12.0  # sudo, mgmt power-off and the bind take well under 1 s here
RELEASE_TIMEOUT_S = 10.0  # the helper powers the adapter on again (retries up to 8 s)
SYSFS = Path("/sys/class/bluetooth")
HCIGETDEVINFO = 0x800448D3  # _IOR('H', 211, int)
HCI_UP = 1 << 0
BUS_NAMES = {0: "virtual", 1: "usb", 2: "pccard", 3: "uart", 4: "rs232", 5: "pci", 6: "sdio", 7: "spi",
             8: "i2c", 9: "smd", 10: "virtio"}
MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
NAME_RE = re.compile(r"hci\d{1,3}")


class HciError(OSError):
    """A command failed (status != 0), timed out, or the channel is gone."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ClaimError(RuntimeError):
    """The adapter could not be taken from BlueZ (no helper, sudo refused, busy)."""


# ---------------------------------------------------------------- packets


def command_packet(opcode: int, params: bytes = b"") -> bytes:
    """An H4 command packet: type 0x01, opcode, parameter length, parameters."""
    if len(params) > 255:
        raise ValueError("HCI command parameters are at most 255 bytes")
    return struct.pack("<BHB", CMD_PKT, opcode, len(params)) + params


def parse_command(packet: bytes) -> tuple[int, bytes]:
    """(opcode, parameters) of an H4 command packet (the fake controller's side)."""
    if len(packet) < 4 or packet[0] != CMD_PKT:
        raise ValueError(f"not an HCI command: {packet.hex()}")
    _t, opcode, plen = struct.unpack_from("<BHB", packet)
    return opcode, packet[4:4 + plen]


def event_packet(code: int, params: bytes) -> bytes:
    return bytes([EVT_PKT, code, len(params)]) + params


def command_complete(opcode: int, status: int = 0, ret: bytes = b"") -> bytes:
    """An H4 Command Complete event (Num_HCI_Command_Packets 1)."""
    return event_packet(EV_COMMAND_COMPLETE, struct.pack("<BHB", 1, opcode, status) + ret)


def parse_event(packet: bytes) -> tuple[int, bytes] | None:
    """(event code, parameters) of an H4 event packet; None for anything else."""
    if len(packet) < 3 or packet[0] != EVT_PKT:
        return None
    return packet[1], packet[3:3 + packet[2]]


def le_set_adv_params(interval: int, adv_type: int = ADV_NONCONN_IND, own_addr_type: int = 0,
                      channel_map: int = 0x07) -> bytes:
    """LE Set Advertising Parameters: min = max interval (0.625 ms units), no peer, all channels."""
    return struct.pack("<HHBBB6sBB", interval, interval, adv_type, own_addr_type, 0, bytes(6), channel_map, 0)


def le_set_adv_data(ad: bytes) -> bytes:
    """LE Set Advertising Data: the significant length, then 31 bytes zero-padded."""
    if len(ad) > ADV_DATA_MAX:
        raise ValueError(f"advertising data is at most {ADV_DATA_MAX} bytes, got {len(ad)}")
    return bytes([len(ad)]) + ad.ljust(ADV_DATA_MAX, b"\0")


def le_set_adv_enable(on: bool) -> bytes:
    return bytes([1 if on else 0])


def le_set_scan_params(interval: int = SCAN_INTERVAL, window: int = SCAN_WINDOW, active: bool = False,
                       own_addr_type: int = 0, filter_policy: int = 0) -> bytes:
    if window > interval:
        raise ValueError("the scan window cannot exceed the interval")
    return struct.pack("<BHHBB", 1 if active else 0, interval, window, own_addr_type, filter_policy)


def le_set_scan_enable(on: bool, filter_duplicates: bool = False) -> bytes:
    """Scanning on with the controller's duplicate filter off: every advert is reported."""
    return bytes([1 if on else 0, 1 if filter_duplicates else 0])


def mesh_ad(pdu: bytes) -> bytes:
    """A network PDU as the Mesh Message AD structure (length, 0x2A, PDU)."""
    if not 0 < len(pdu) <= MESH_PDU_MAX:
        raise ValueError(f"a mesh network PDU over the advertising bearer is 1-{MESH_PDU_MAX} bytes")
    return bytes([len(pdu) + 1, AD_MESH_MESSAGE]) + pdu


def parse_ad(data: bytes) -> list[tuple[int, bytes]]:
    """AD structures as (type, value); stops at a zero length or a truncated one."""
    out, i = [], 0
    while i < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            break
        out.append((data[i + 1], bytes(data[i + 2:i + 1 + length])))
        i += 1 + length
    return out


def format_address(raw: bytes) -> str:
    """6 bytes little endian (as HCI carries them) -> "AA:BB:CC:DD:EE:FF"."""
    return ":".join(f"{b:02X}" for b in reversed(raw))


def address_bytes(address: str) -> bytes:
    return bytes(int(x, 16) for x in reversed(address.split(":")))


@dataclass
class AdvReport:
    event_type: int
    addr_type: int
    address: str
    data: bytes
    rssi: int | None

    def ads(self) -> list[tuple[int, bytes]]:
        return parse_ad(self.data)


def parse_adv_reports(params: bytes) -> list[AdvReport]:
    """LE Advertising Report (subevent 0x02, the parameters after the subevent
    byte): Num_Reports, then per report event type, address type, address,
    data length, data, RSSI (laid out report after report, as Linux reads it;
    controllers send one report per event). RSSI 127: not available."""
    if not params:
        return []
    out, i = [], 1
    for _n in range(params[0]):
        if i + 9 > len(params):
            break
        evt, atype = params[i], params[i + 1]
        address = format_address(params[i + 2:i + 8])
        dlen = params[i + 8]
        end = i + 9 + dlen
        if end + 1 > len(params) or dlen > ADV_DATA_MAX:
            break
        rssi = struct.unpack_from("b", params, end)[0]
        out.append(AdvReport(evt, atype, address, bytes(params[i + 9:end]), None if rssi == 127 else rssi))
        i = end + 1
    return out


def adv_report_event(address: str, data: bytes, rssi: int = -60, event_type: int = ADV_NONCONN_IND,
                     addr_type: int = 0) -> bytes:
    """An H4 LE Advertising Report event with one report (for fake controllers)."""
    report = bytes([event_type, addr_type]) + address_bytes(address) + bytes([len(data)]) + data
    return event_packet(EV_LE_META, bytes([LE_ADV_REPORT, 1]) + report + struct.pack("b", rssi))


@dataclass
class AdvView:
    """What the mesh backend's advert handler reads of a bleak advert, from a raw one."""

    rssi: int | None
    local_name: str | None = None
    service_uuids: list[str] = field(default_factory=list)
    service_data: dict[str, bytes] = field(default_factory=dict)


def uuid16(value: int) -> str:
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


def adv_view(report: AdvReport) -> AdvView:
    view = AdvView(report.rssi)
    for t, v in report.ads():
        if t in (AD_UUID16_SOME, AD_UUID16_ALL):
            view.service_uuids += [uuid16(u) for (u,) in struct.iter_unpack("<H", v[:len(v) // 2 * 2])]
        elif t == AD_SERVICE_DATA16 and len(v) >= 2:
            view.service_data[uuid16(struct.unpack_from("<H", v)[0])] = v[2:]
        elif t in (AD_NAME_SHORT, AD_NAME_FULL):
            view.local_name = v.decode(errors="replace")
    return view


# ---------------------------------------------------------------- the radio


class HciRadio:
    """The controller behind an HCI user channel: scans, reports every advert
    (on_report) and every Mesh Message AD (on_mesh), and sends network PDUs
    as non-connectable adverts, one after the other (send_mesh)."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.on_report: Callable[[AdvReport], None] | None = None
        self.on_mesh: Callable[[bytes, AdvReport], None] | None = None
        self.on_dead: Callable[[str], None] | None = None
        self.address: str | None = None
        self.adv_interval = ADV_INTERVAL_FAST
        self.dead: str | None = None  # why the channel is unusable (None: fine)
        self.sent = 0  # PDUs advertised
        self._pending: tuple[int, asyncio.Future] | None = None
        self._cmd_lock = asyncio.Lock()
        self._txq: asyncio.Queue[bytes] = asyncio.Queue()
        self._tx_task: asyncio.Task | None = None
        self._reading = False

    @property
    def tx_s(self) -> float:
        return TX_S.get(self.adv_interval, 0.115)

    async def start(self) -> None:
        """Reset the controller and set it up: event masks, advertising
        parameters (20 ms, or 100 ms where refused), passive scan at 100 %
        with no duplicate filter."""
        loop = asyncio.get_running_loop()
        if not self._reading:
            os.set_blocking(self.fd, False)
            loop.add_reader(self.fd, self._readable)
            self._reading = True
        await self.command(OP_RESET)
        await self.command(OP_SET_EVENT_MASK, struct.pack("<Q", EVENT_MASK))
        await self.command(OP_LE_SET_EVENT_MASK, struct.pack("<Q", LE_EVENT_MASK))
        with contextlib.suppress(HciError):
            ret = await self.command(OP_READ_BD_ADDR)
            if len(ret) >= 6:
                self.address = format_address(ret[:6])
        try:
            await self.command(OP_LE_SET_ADV_PARAMS, le_set_adv_params(ADV_INTERVAL_FAST))
            self.adv_interval = ADV_INTERVAL_FAST
        except HciError as exc:
            if exc.status not in (STATUS_INVALID_PARAMS, STATUS_UNSUPPORTED):
                raise
            await self.command(OP_LE_SET_ADV_PARAMS, le_set_adv_params(ADV_INTERVAL_LEGACY))
            self.adv_interval = ADV_INTERVAL_LEGACY
        await self.command(OP_LE_SET_SCAN_PARAMS, le_set_scan_params())
        await self.command(OP_LE_SET_SCAN_ENABLE, le_set_scan_enable(True, filter_duplicates=False))
        if self._tx_task is None or self._tx_task.done():
            self._tx_task = loop.create_task(self._tx_loop())
        log.info("exclusive: controller %s ready: scanning without duplicate filter, advertising every %.1f ms",
                 self.address or "?", self.adv_interval * 0.625)

    async def command(self, opcode: int, params: bytes = b"", timeout: float = COMMAND_TIMEOUT_S) -> bytes:
        """Send one command and wait for its Command Complete; its return
        parameters after the status. HciError on a status or a timeout."""
        async with self._cmd_lock:
            if self.dead:
                raise HciError(f"channel gone: {self.dead}")
            fut = asyncio.get_running_loop().create_future()
            self._pending = (opcode, fut)
            try:
                os.write(self.fd, command_packet(opcode, params))
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                raise HciError(f"no answer to command 0x{opcode:04x}") from None
            except OSError as exc:
                if not isinstance(exc, HciError):
                    self._die(f"write failed: {exc}")
                raise
            finally:
                self._pending = None

    def send_mesh(self, pdu: bytes) -> None:
        """Queue one network PDU (sync, for MeshNode's send callback)."""
        mesh_ad(pdu)  # checks the size now, not in the TX task
        self._txq.put_nowait(pdu)

    async def _tx_loop(self) -> None:
        while True:
            pdu = await self._txq.get()
            try:
                await self.command(OP_LE_SET_ADV_DATA, le_set_adv_data(mesh_ad(pdu)))
                await self.command(OP_LE_SET_ADV_ENABLE, le_set_adv_enable(True))
                await asyncio.sleep(self.tx_s)
                await self.command(OP_LE_SET_ADV_ENABLE, le_set_adv_enable(False))
                self.sent += 1
            except HciError as exc:
                log.warning("exclusive: network PDU not sent: %s", exc)
                if self.dead:
                    return

    def _readable(self) -> None:
        while True:
            try:
                packet = os.read(self.fd, 1024)
            except BlockingIOError:
                return
            except OSError as exc:
                self._die(f"read failed: {exc}")
                return
            if not packet:
                self._die("channel closed")
                return
            try:
                self._packet(packet)
            except Exception:  # a parser or handler bug must not kill the reader
                log.exception("exclusive: HCI packet %s", packet[:40].hex())

    def _packet(self, packet: bytes) -> None:
        ev = parse_event(packet)
        if ev is None:
            return
        code, params = ev
        if code == EV_COMMAND_COMPLETE and len(params) >= 3:
            opcode = struct.unpack_from("<H", params, 1)[0]
            status, ret = (params[3], params[4:]) if len(params) >= 4 else (0, b"")
            self._resolve(opcode, status, ret)
        elif code == EV_COMMAND_STATUS and len(params) >= 4:
            status, opcode = params[0], struct.unpack_from("<H", params, 2)[0]
            if status:
                self._resolve(opcode, status, b"")
        elif code == EV_LE_META and params and params[0] == LE_ADV_REPORT:
            for report in parse_adv_reports(params[1:]):
                self._report(report)
        elif code == EV_HARDWARE_ERROR:
            self._die(f"controller hardware error 0x{params[0] if params else 0:02x}")

    def _resolve(self, opcode: int, status: int, ret: bytes) -> None:
        if self._pending is None or self._pending[0] != opcode or self._pending[1].done():
            return
        fut = self._pending[1]
        if status:
            fut.set_exception(HciError(f"command 0x{opcode:04x} failed: status 0x{status:02x}", status))
        else:
            fut.set_result(ret)

    def _report(self, report: AdvReport) -> None:
        if self.on_mesh is not None:
            for t, v in report.ads():
                if t == AD_MESH_MESSAGE and v:
                    self.on_mesh(v, report)
        if self.on_report is not None:
            self.on_report(report)

    def _die(self, why: str) -> None:
        if self.dead:
            return
        self.dead = why
        log.warning("exclusive: HCI channel unusable: %s", why)
        self._stop_reading()
        if self._pending is not None and not self._pending[1].done():
            self._pending[1].set_exception(HciError(f"channel gone: {why}"))
        if self.on_dead is not None:
            self.on_dead(why)

    def _stop_reading(self) -> None:
        if self._reading:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.fd)
            self._reading = False

    async def close(self) -> None:
        """Stop scanning and advertising (best effort) and close the channel:
        the kernel hands the adapter back to BlueZ with the last copy."""
        if self.fd < 0:
            return
        if not self.dead:
            with contextlib.suppress(Exception):
                await self.command(OP_LE_SET_SCAN_ENABLE, le_set_scan_enable(False), timeout=0.5)
            with contextlib.suppress(Exception):
                await self.command(OP_LE_SET_ADV_ENABLE, le_set_adv_enable(False), timeout=0.5)
        if self._tx_task is not None:
            self._tx_task.cancel()
            await asyncio.gather(self._tx_task, return_exceptions=True)
        self.dead = self.dead or "closed"
        self._stop_reading()
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = -1


# ---------------------------------------------------------------- the claim


@dataclass
class Claim:
    """An adapter lent by the root helper: its user channel and the helper
    waiting for us to let go (the control socket's end)."""

    fd: int
    proc: asyncio.subprocess.Process | None
    sock: socket.socket
    info: dict

    async def release(self, timeout: float = RELEASE_TIMEOUT_S) -> str:
        """Close our end: the helper powers the adapter on in BlueZ and exits.
        The channel fd belongs to the radio (closed there first)."""
        with contextlib.suppress(OSError):
            self.sock.close()
        if self.proc is None:
            return ""
        try:
            _out, err = await asyncio.wait_for(self.proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            return "helper did not finish"
        lines = [ln for ln in (err or b"").decode(errors="replace").splitlines() if ln.strip()]
        return lines[-1] if lines else ""


def helper_command(adapter: str, helper: str = HELPER) -> list[str]:
    return ["sudo", "-n", helper, adapter]


async def claim(adapter: str, command: list[str] | None = None, timeout: float = CLAIM_TIMEOUT_S) -> Claim:
    """Run the helper for `adapter` (hciN) and take the user channel it passes."""
    if not NAME_RE.fullmatch(adapter):
        raise ClaimError(f"not an adapter name: {adapter!r}")
    cmd = command or helper_command(adapter)
    if command is None and not os.path.exists(HELPER):
        raise ClaimError("the helper is not installed (system setup v4)")
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=theirs.fileno(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        ours.close()
        raise ClaimError(f"helper not started: {exc}") from exc
    finally:
        theirs.close()
    ours.settimeout(timeout)
    fds: list[int] = []
    try:
        try:
            msg, fds, _flags, _addr = await asyncio.to_thread(socket.recv_fds, ours, 4096, 1)
        except OSError as exc:
            msg = b""
            log.debug("exclusive: helper answer: %s", exc)
        info = {}
        if msg:
            with contextlib.suppress(ValueError):
                info = json.loads(msg.decode(errors="replace").splitlines()[0])
        if not info.get("ok") or not fds:
            for fd in fds:
                os.close(fd)
            why = info.get("error")
            if not why:  # sudo refused, or the helper died: its stderr says why
                with contextlib.suppress(TimeoutError, OSError):
                    _out, err = await asyncio.wait_for(proc.communicate(), 3.0)
                    lines = [ln for ln in (err or b"").decode(errors="replace").splitlines() if ln.strip()]
                    why = lines[-1] if lines else f"exit {proc.returncode}"
            raise ClaimError(why or "no answer from the helper")
    except BaseException:
        ours.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        raise
    ours.settimeout(None)
    return Claim(fds[0], proc, ours, info)


# ---------------------------------------------------------------- adapters


def _devinfo(index: int) -> tuple[str | None, int | None, bool | None]:
    """(address, bus type, up) of hciN via HCIGETDEVINFO (no root needed)."""
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except (OSError, AttributeError):
        return None, None, None
    with sock:
        buf = bytearray(struct.pack("<H", index) + bytes(100))
        try:
            fcntl.ioctl(sock.fileno(), HCIGETDEVINFO, buf)
        except OSError:
            return None, None, None
    # struct hci_dev_info: dev_id u16, name[8], bdaddr[6], flags u32 (offset 16), type u8 (20)
    flags = struct.unpack_from("<I", buf, 16)[0]
    return format_address(bytes(buf[10:16])), buf[20] & 0x0F, bool(flags & HCI_UP)


def _removable(device: Path) -> str | None:
    """The USB port's removable attribute (fixed | removable | unknown) above an interface."""
    for p in [device, *device.parents]:
        f = p / "removable"
        if f.is_file():
            with contextlib.suppress(OSError):
                return f.read_text().strip()
        if p.name.startswith("usb") or p == p.parent:
            break
    return None


def scan_sysfs(root: Path = SYSFS, devinfo=_devinfo) -> list[dict]:
    """Every controller (hciN): name, address, bus, built-in or not, up."""
    out = []
    for p in sorted(root.glob("hci*"), key=lambda p: int(p.name[3:]) if p.name[3:].isdigit() else 1 << 30):
        if not NAME_RE.fullmatch(p.name):
            continue  # hci0:16 and the like are connections
        device = Path(os.path.realpath(p / "device")) if (p / "device").exists() else None
        address, bus_type, up = devinfo(int(p.name[3:]))
        path = str(device or "")
        bus = BUS_NAMES.get(bus_type) if bus_type is not None else None
        if bus is None:
            bus = "usb" if "/usb" in path else "other"
        removable = _removable(device) if device is not None and bus == "usb" else None
        # A USB dongle sits on a removable (or unknown) port; a combo card's
        # Bluetooth is on an internal USB port marked "fixed" (here: hci0).
        # PCI, SDIO, UART: built in.
        builtin = removable == "fixed" if bus == "usb" else True
        out.append({"name": p.name, "address": address, "bus": bus, "builtin": builtin,
                    "removable": removable, "up": up})
    return out


async def bluez_adapters(ignore=(), timeout: float = 2.0) -> dict[str, dict]:
    """hciN -> {powered, devices: [names of connected devices]} from BlueZ
    over D-Bus (devices at the addresses in `ignore` left out: our own mesh
    link); {} when it cannot be asked."""
    try:
        from dbus_fast import BusType, Message
        from dbus_fast.aio import MessageBus
    except ImportError:
        return {}
    bus = None
    try:
        bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), timeout)
        reply = await asyncio.wait_for(bus.call(Message(
            destination="org.bluez", path="/", interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects")), timeout)
        objects = reply.body[0] if reply and reply.body else {}
    except Exception as exc:
        log.debug("BlueZ adapters: %s", exc)
        return {}
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()
    out: dict[str, dict] = {}
    for path, ifaces in objects.items():
        props = ifaces.get("org.bluez.Adapter1")
        if props is not None:
            entry = out.setdefault(path.rsplit("/", 1)[-1], {"devices": []})
            entry["powered"] = bool(getattr(props.get("Powered"), "value", False))
    for path, ifaces in objects.items():
        props = ifaces.get("org.bluez.Device1")
        if props is None or not getattr(props.get("Connected"), "value", False):
            continue
        if str(getattr(props.get("Address"), "value", "")).upper() in ignore:
            continue
        adapter = str(getattr(props.get("Adapter"), "value", "")).rsplit("/", 1)[-1]
        name = getattr(props.get("Alias"), "value", None) or getattr(props.get("Address"), "value", "?")
        out.setdefault(adapter, {"devices": []})["devices"].append(str(name))
    return out


def merge_adapters(sysfs: list[dict], bluez: dict[str, dict], claimed: str | None = None) -> list[dict]:
    """The `adapters` list: sysfs facts plus BlueZ's view; an adapter we hold
    is gone from BlueZ (claimed: true)."""
    out = []
    for a in sysfs:
        z = bluez.get(a["name"])
        mine = a["name"] == claimed
        out.append({"name": a["name"], "address": a["address"], "bus": a["bus"], "builtin": a["builtin"],
                    "powered": (z.get("powered") if z and "powered" in z else (None if mine else a.get("up"))),
                    "in_bluez": z is not None and "powered" in z, "devices": list(z["devices"]) if z else [],
                    "claimed": mine})
    return out


async def list_adapters(claimed: str | None = None, ignore=()) -> list[dict]:
    sysfs = await asyncio.to_thread(scan_sysfs)
    return merge_adapters(sysfs, await bluez_adapters({a.upper() for a in ignore}), claimed)


def check_adapter_setting(value) -> str:
    """settings.exclusive_adapter: "" (automatic), hciN or a MAC."""
    value = "" if value is None else str(value).strip()
    if value.lower() in ("", "auto", "default"):
        return ""
    if NAME_RE.fullmatch(value):
        return value
    if MAC_RE.fullmatch(value):
        return value.upper()
    raise ValueError("exclusive_adapter must be hciN, a Bluetooth address (AA:BB:CC:DD:EE:FF) or auto")


def choose_adapter(adapters: list[dict], setting: str = "") -> dict | None:
    """The adapter for exclusive mode: the one named (hciN or MAC), else a
    USB adapter if there is one, else the built-in one. None: not there."""
    setting = check_adapter_setting(setting)
    if not setting:
        external = [a for a in adapters if not a.get("builtin")]
        return (external or adapters or [None])[0]
    if MAC_RE.fullmatch(setting):
        return next((a for a in adapters if (a.get("address") or "").upper() == setting), None)
    return next((a for a in adapters if a.get("name") == setting), None)


def sole_or_builtin(adapters: list[dict], chosen: dict | None) -> bool:
    """Taking `chosen` leaves BlueZ without a radio for headphones, mice..."""
    return chosen is not None and (chosen.get("builtin") or len(adapters) <= 1)
