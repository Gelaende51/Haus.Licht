"""Flick mode exclusive: HCI packets, the adapter claim and choice, the mesh
over the advertising bearer of a fake controller (a SOCK_SEQPACKET pair keeps
HCI's packet boundaries), the fallback to fast, and the SEQ across a switch
from the GATT proxy to the advertising bearer."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from haus import hci, rootsetup
from haus.hci import (
    ADV_IND, ADV_INTERVAL_FAST, ADV_INTERVAL_LEGACY, ADV_NONCONN_IND, OP_LE_SET_ADV_DATA, OP_LE_SET_ADV_ENABLE,
    OP_LE_SET_ADV_PARAMS, OP_LE_SET_EVENT_MASK, OP_LE_SET_SCAN_ENABLE, OP_LE_SET_SCAN_PARAMS, OP_READ_BD_ADDR,
    OP_RESET, OP_SET_EVENT_MASK,
)

ROOT = Path(__file__).resolve().parent.parent
MAC = "F0:D1:B8:53:33:2F"
BID = "mesh:0002"
CONTROLLER = "80:D2:1D:F5:AD:78"
# flags, 16-bit UUID 0x1828 (Mesh Proxy), its service data: Network ID (type 0, 8 bytes)
PROXY_AD = bytes.fromhex("020106" "03032818" "0c16281800") + bytes(8)


# ------------------------------------------------------------------ packets


def test_le_set_advertising_parameters():
    params = hci.le_set_adv_params(ADV_INTERVAL_LEGACY)
    assert params.hex() == "a000" "a000" "03" "00" "00" "000000000000" "07" "00"
    assert hci.command_packet(OP_LE_SET_ADV_PARAMS, params)[:4].hex() == "01" "0620" "0f"
    assert hci.le_set_adv_params(ADV_INTERVAL_FAST)[:4] == bytes.fromhex("20002000")


def test_le_set_advertising_data_carries_one_mesh_message():
    pdu = bytes(range(29))  # the largest network PDU (unsegmented, 32-bit NetMIC)
    ad = hci.mesh_ad(pdu)
    assert ad[:2] == bytes([30, 0x2A]) and ad[2:] == pdu and len(ad) == 31
    params = hci.le_set_adv_data(ad)
    assert len(params) == 32 and params[0] == 31 and params[1:] == ad
    short = hci.le_set_adv_data(hci.mesh_ad(b"\x01\x02\x03"))
    assert short.hex() == "05" "042a010203" + "00" * 26
    with pytest.raises(ValueError):
        hci.mesh_ad(bytes(30))
    with pytest.raises(ValueError):
        hci.mesh_ad(b"")
    assert hci.command_packet(OP_LE_SET_ADV_DATA, params)[:4].hex() == "01" "0820" "20"


def test_le_advertise_and_scan_enable():
    assert hci.command_packet(OP_LE_SET_ADV_ENABLE, hci.le_set_adv_enable(True)).hex() == "010a200101"
    assert hci.command_packet(OP_LE_SET_ADV_ENABLE, hci.le_set_adv_enable(False)).hex() == "010a200100"
    # passive, 30 ms window in a 30 ms interval: 100 %; own address public, no filter
    assert hci.command_packet(OP_LE_SET_SCAN_PARAMS, hci.le_set_scan_params()).hex() == "010b2007" "00" "3000" "3000" "00" "00"
    # enabled, duplicate filter OFF: every advert is reported
    assert hci.command_packet(OP_LE_SET_SCAN_ENABLE, hci.le_set_scan_enable(True)).hex() == "010c20020100"
    with pytest.raises(ValueError):
        hci.le_set_scan_params(interval=0x10, window=0x20)


def test_event_mask_lets_le_meta_events_through():
    assert hci.EVENT_MASK & (1 << 61)
    assert hci.EVENT_MASK & 0x00001FFFFFFFFFFF == 0x00001FFFFFFFFFFF  # the defaults stay


def test_advertising_report_parsing():
    # Core spec layout, one report: subevent 02, 1 report, ADV_NONCONN_IND, public
    # address (little endian), data length, a Mesh Message AD, RSSI -60 (0xc4)
    event = bytes.fromhex("043e1302" "01" "03" "00" "2f3353b8d1f0" "07" "062a0102030405" "c4")
    code, params = hci.parse_event(event)
    assert code == hci.EV_LE_META and params[0] == hci.LE_ADV_REPORT
    (r,) = hci.parse_adv_reports(params[1:])
    assert (r.event_type, r.addr_type, r.address, r.rssi) == (ADV_NONCONN_IND, 0, MAC, -60)
    assert r.ads() == [(0x2A, bytes.fromhex("0102030405"))]
    assert hci.adv_report_event(MAC, r.data, -60) == event


def test_advertising_reports_several_unknown_rssi_and_truncated():
    one = bytes([ADV_IND, 1]) + hci.address_bytes(MAC) + bytes([3]) + b"\x02\x01\x06" + bytes([127])
    two = bytes([ADV_NONCONN_IND, 0]) + hci.address_bytes(CONTROLLER) + bytes([0]) + bytes([0xB0])
    reports = hci.parse_adv_reports(bytes([2]) + one + two)
    assert [(r.address, r.rssi, r.data) for r in reports] == [(MAC, None, b"\x02\x01\x06"),
                                                               (CONTROLLER, -80, b"")]
    assert hci.parse_adv_reports(bytes([2]) + one + two[:5]) == reports[:1]  # cut short: what is whole
    assert hci.parse_adv_reports(b"") == []


def test_ad_structures_and_the_view():
    data = PROXY_AD + bytes.fromhex("0509" "48657921") + bytes([0, 9, 9])
    assert hci.parse_ad(data) == [(1, b"\x06"), (3, b"\x28\x18"), (0x16, b"\x28\x18\x00" + bytes(8)),
                                  (9, b"Hey!")]  # stops at the zero length
    view = hci.adv_view(hci.AdvReport(ADV_IND, 0, MAC, data, -50))
    assert view.service_uuids == [hci.uuid16(0x1828)] and view.local_name == "Hey!"
    assert view.service_data == {hci.uuid16(0x1828): bytes(9)} and view.rssi == -50
    assert hci.parse_ad(b"\x05\x2a\x01") == []  # truncated


def test_command_complete_round_trip():
    pkt = hci.command_complete(OP_READ_BD_ADDR, 0, hci.address_bytes(CONTROLLER))
    code, params = hci.parse_event(pkt)
    assert code == hci.EV_COMMAND_COMPLETE and params[1:3] == bytes([0x09, 0x10]) and params[3] == 0
    assert hci.format_address(params[4:10]) == CONTROLLER
    assert hci.parse_command(hci.command_packet(OP_RESET)) == (OP_RESET, b"")
    assert hci.parse_event(b"\x02\x00") is None


# ------------------------------------------------------------------ the fake controller


class FakeController:
    """The far end of an HCI user channel: answers every command with a
    Command Complete (status from `status_of`), records them, and hands the
    Mesh Message of each advertised PDU to `bulb`."""

    def __init__(self, sock: socket.socket, bulb=None, status_of=None) -> None:
        self.sock = sock
        sock.setblocking(False)
        self.bulb = bulb
        self.status_of = status_of or (lambda opcode, params: 0)
        self.commands: list[tuple[int, bytes]] = []
        self.advertised: list[bytes] = []
        self.closed = False
        asyncio.get_running_loop().add_reader(sock.fileno(), self._readable)

    def _readable(self) -> None:
        try:
            pkt = self.sock.recv(1024)
        except BlockingIOError:
            return
        if not pkt:
            asyncio.get_running_loop().remove_reader(self.sock.fileno())
            self.closed = True
            return
        opcode, params = hci.parse_command(pkt)
        self.commands.append((opcode, params))
        status = self.status_of(opcode, params)
        ret = hci.address_bytes(CONTROLLER) if opcode == OP_READ_BD_ADDR else b""
        self.sock.send(hci.command_complete(opcode, status, ret))
        if opcode == OP_LE_SET_ADV_DATA and status == 0:
            for t, v in hci.parse_ad(params[1:1 + params[0]]):
                if t == hci.AD_MESH_MESSAGE:
                    self.advertised.append(v)
                    if self.bulb is not None:
                        self.bulb.hear(v, self)

    def inject(self, packet: bytes) -> None:
        self.sock.send(packet)

    def advert(self, mac: str = MAC) -> None:
        self.inject(hci.adv_report_event(mac, PROXY_AD, -58, ADV_IND))

    def opcodes(self) -> list[int]:
        return [op for op, _p in self.commands]

    def close(self) -> None:
        if not self.closed:
            asyncio.get_running_loop().remove_reader(self.sock.fileno())
        self.sock.close()


def refuse_20ms(opcode: int, params: bytes) -> int:
    """A Bluetooth 4.2 controller: non-connectable adverts not below 100 ms."""
    return hci.STATUS_INVALID_PARAMS if opcode == OP_LE_SET_ADV_PARAMS and params[:2] == b"\x20\x00" else 0


def test_radio_sets_the_controller_up_and_advertises_mesh_pdus():
    async def body():
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ctrl = FakeController(theirs, status_of=refuse_20ms)
        radio = hci.HciRadio(ours.detach())
        reports, meshes = [], []
        radio.on_report = reports.append
        radio.on_mesh = lambda pdu, report: meshes.append((pdu, report.address))
        await radio.start()
        assert ctrl.opcodes() == [OP_RESET, OP_SET_EVENT_MASK, OP_LE_SET_EVENT_MASK, OP_READ_BD_ADDR,
                                  OP_LE_SET_ADV_PARAMS, OP_LE_SET_ADV_PARAMS, OP_LE_SET_SCAN_PARAMS,
                                  OP_LE_SET_SCAN_ENABLE]
        assert ctrl.commands[1][1] == hci.EVENT_MASK.to_bytes(8, "little")
        assert ctrl.commands[-1][1] == b"\x01\x00"  # scanning, no duplicate filter
        assert radio.adv_interval == ADV_INTERVAL_LEGACY and radio.address == CONTROLLER
        del ctrl.commands[:]
        radio.send_mesh(b"\x11" * 20)
        radio.send_mesh(b"\x22" * 20)
        for _i in range(100):
            if radio.sent == 2:
                break
            await asyncio.sleep(0.02)
        assert ctrl.opcodes() == [OP_LE_SET_ADV_DATA, OP_LE_SET_ADV_ENABLE, OP_LE_SET_ADV_ENABLE] * 2
        assert [p for op, p in ctrl.commands if op == OP_LE_SET_ADV_ENABLE] == [b"\x01", b"\x00"] * 2
        assert ctrl.advertised == [b"\x11" * 20, b"\x22" * 20]
        ctrl.inject(hci.adv_report_event(MAC, hci.mesh_ad(b"\x33" * 18)))
        ctrl.advert()
        await asyncio.sleep(0.05)
        assert meshes == [(b"\x33" * 18, MAC)] and [r.address for r in reports] == [MAC, MAC]
        dead = []
        radio.on_dead = dead.append
        ctrl.close()  # the controller (USB) went away
        await asyncio.sleep(0.05)
        assert radio.dead and dead
        with pytest.raises(hci.HciError):
            await radio.command(OP_RESET)
        await radio.close()
    asyncio.run(body())


def test_radio_takes_20_ms_where_allowed_and_reports_a_failed_command():
    async def body():
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ctrl = FakeController(theirs, status_of=lambda op, p: 0x0C if op == OP_LE_SET_SCAN_ENABLE else 0)
        radio = hci.HciRadio(ours.detach())
        with pytest.raises(hci.HciError) as err:
            await radio.start()
        assert err.value.status == 0x0C and radio.adv_interval == ADV_INTERVAL_FAST
        await radio.close()
        await asyncio.sleep(0.05)
        assert ctrl.closed
        ctrl.close()
    asyncio.run(body())


# ------------------------------------------------------------------ the claim (SCM_RIGHTS)

FAKE_HELPER = r'''
import json, os, socket, sys
ctl = socket.socket(fileno=os.dup(0))
mode, name = sys.argv[1], sys.argv[2]
if mode == "ok":
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    b.send(b"hello from " + name.encode())
    socket.send_fds(ctl, [json.dumps({"ok": True, "adapter": name}).encode() + b"\n"], [a.fileno()])
    a.close()
    while ctl.recv(64):
        pass
    print("released " + name, file=sys.stderr)
elif mode == "refuse":
    socket.send_fds(ctl, [json.dumps({"ok": False, "error": name + ": busy"}).encode() + b"\n"], [])
else:
    print("sudo: a password is required", file=sys.stderr)
    sys.exit(1)
'''


def fake_helper(tmp_path: Path, mode: str) -> list[str]:
    script = tmp_path / "helper.py"
    script.write_text(FAKE_HELPER)
    return [sys.executable, "-I", str(script), mode]


def test_claim_takes_the_channel_passed_over_stdin(tmp_path):
    async def body():
        claim = await hci.claim("hci0", fake_helper(tmp_path, "ok") + ["hci0"])
        assert claim.info["adapter"] == "hci0"
        assert os.read(claim.fd, 64) == b"hello from hci0"  # the very socket the helper opened
        os.close(claim.fd)
        assert await claim.release() == "released hci0"  # our end closed: the helper restores
    asyncio.run(body())


@pytest.mark.parametrize("mode, why", [("refuse", "hci1: busy"), ("sudo", "sudo: a password is required")])
def test_claim_failures_say_why(tmp_path, mode, why):
    async def body():
        with pytest.raises(hci.ClaimError, match=why):
            await hci.claim("hci1", fake_helper(tmp_path, mode) + ["hci1"])
    asyncio.run(body())


def test_claim_refuses_odd_names_and_a_missing_helper(monkeypatch):
    async def body():
        with pytest.raises(hci.ClaimError, match="not an adapter"):
            await hci.claim("hci0; rm -rf /")
        monkeypatch.setattr(hci, "HELPER", "/nonexistent/hci-claim")
        with pytest.raises(hci.ClaimError, match="setup v4"):
            await hci.claim("hci0")
    asyncio.run(body())


def test_the_root_helper_checks_its_input_without_root():
    helper = [sys.executable, "-I", str(ROOT / "install" / "hci-claim")]
    assert subprocess.run(helper + ["../hci0"], capture_output=True).returncode == 2
    assert subprocess.run(helper + ["hci0"], stdin=subprocess.DEVNULL, capture_output=True).returncode == 2
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with ours, theirs:
        proc = subprocess.run(helper + ["hci987"], stdin=theirs, capture_output=True, timeout=20)
        assert proc.returncode == 1
        assert json.loads(ours.recv(4096)) == {"ok": False, "error": "no adapter hci987"}


def test_setup_v4_installs_the_helper_for_sudo():
    script = rootsetup.SCRIPT.read_text()
    assert rootsetup.required() >= 4  # 5: the device udev rule (test_localdevices)
    assert '"$here/hci-claim" /usr/local/lib/hauslicht/hci-claim' in script
    assert "NOPASSWD: /usr/local/lib/hauslicht/conn-update, /usr/local/lib/hauslicht/hci-claim" in script
    assert hci.HELPER == "/usr/local/lib/hauslicht/hci-claim"
    assert os.access(ROOT / "install" / "hci-claim", os.X_OK)


# ------------------------------------------------------------------ adapters

BUILTIN = {"name": "hci0", "address": CONTROLLER, "bus": "usb", "builtin": True}
DONGLE = {"name": "hci1", "address": "00:1A:7D:DA:71:13", "bus": "usb", "builtin": False}


@pytest.mark.parametrize("adapters, setting, want", [
    ([BUILTIN, DONGLE], "", "hci1"),           # automatic: a USB adapter first
    ([BUILTIN, DONGLE], "auto", "hci1"),
    ([BUILTIN], "", "hci0"),                   # else the built-in one
    ([BUILTIN, DONGLE], "hci0", "hci0"),       # by name
    ([BUILTIN, DONGLE], "00:1a:7d:da:71:13", "hci1"),  # by address, any case
    ([BUILTIN, DONGLE], "hci7", None),         # not there
    ([], "", None),
])
def test_adapter_choice(adapters, setting, want):
    got = hci.choose_adapter(adapters, setting)
    assert (got["name"] if got else None) == want


def test_adapter_setting_check_and_the_warning():
    assert hci.check_adapter_setting(" Auto ") == "" and hci.check_adapter_setting(None) == ""
    assert hci.check_adapter_setting("hci2") == "hci2"
    assert hci.check_adapter_setting("00:1a:7d:da:71:13") == "00:1A:7D:DA:71:13"
    for bad in ("hci", "eth0", "00:1a:7d", "hci0 hci1"):
        with pytest.raises(ValueError):
            hci.check_adapter_setting(bad)
    assert hci.sole_or_builtin([BUILTIN, DONGLE], BUILTIN)
    assert not hci.sole_or_builtin([BUILTIN, DONGLE], DONGLE)
    assert hci.sole_or_builtin([DONGLE], DONGLE)  # the only one
    assert not hci.sole_or_builtin([BUILTIN], None)


def test_adapters_from_sysfs(tmp_path):
    devices = tmp_path / "devices"
    internal = devices / "usb1" / "1-4" / "1-4:1.0"
    dongle = devices / "usb3" / "3-2" / "3-2:1.0"
    uart = devices / "serial0" / "serial0-0"
    for d, removable in ((internal, "fixed"), (dongle, "removable"), (uart, None)):
        d.mkdir(parents=True)
        if removable:
            (d.parent / "removable").write_text(removable + "\n")
    cls = tmp_path / "class"
    for name, target in (("hci0", internal), ("hci1", dongle), ("hci2", uart)):
        (cls / name).mkdir(parents=True)
        (cls / name / "device").symlink_to(target)
    (cls / "hci0:16").mkdir()  # a connection, not a controller
    info = {0: (CONTROLLER, 1, True), 1: ("00:1A:7D:DA:71:13", 1, False), 2: (None, None, None)}
    got = hci.scan_sysfs(cls, devinfo=info.get)
    assert [(a["name"], a["bus"], a["builtin"]) for a in got] == [
        ("hci0", "usb", True), ("hci1", "usb", False), ("hci2", "other", True)]
    merged = hci.merge_adapters(got, {"hci0": {"powered": True, "devices": ["Headphones"]}}, claimed="hci1")
    assert merged[0]["devices"] == ["Headphones"] and merged[0]["powered"] is True and merged[0]["in_bluez"]
    assert merged[1]["claimed"] and not merged[1]["in_bluez"] and merged[1]["powered"] is None


# ------------------------------------------------------------------ the mesh over the advertising bearer

btmesh = pytest.importorskip("btmesh")
from btmesh import network as btnet  # noqa: E402
from btmesh.access import encode_opcode  # noqa: E402
from btmesh.node import MeshNode  # noqa: E402

from haus.backends import mesh  # noqa: E402
from haus.backends.mesh import AdvLink, MeshBackend, MeshLink  # noqa: E402

NETKEY, APPKEY, DEVKEY = bytes(range(16)), bytes(range(16, 32)), bytes(range(32, 48))


class MeshBulb:
    """A Volkslicht on the advertising bearer: its vendor boot count, OnPowerUp
    and Default Transition Time answer, each reply advertised twice (as the
    real network transmit and relays repeat it)."""

    def __init__(self) -> None:
        self.count = 7
        self.powerup = 1
        self.heard: list = []  # (src, seq) of what reached it
        self.ops: list[int] = []  # opcodes it answered
        self.node = MeshNode(netkey=NETKEY, appkey=APPKEY, iv_index=0, src_addr=0x0002,
                             send_network_pdu=self._out, seq=5000)
        self.node.on_message = self._message
        self.ctrl = None
        self.answers = True
        self.lose = 0  # the next this many PDUs never reach it (lost on the air)
        self.level: float | None = None  # percent, from a Lightness or Level Set
        self.delay = 0.01  # first copy of a reply
        self.repeats = (0.02,)  # the same PDU again after these (network transmit, relays)

    def reboot(self) -> None:
        self.count = 0

    def hear(self, pdu: bytes, ctrl: FakeController) -> None:
        self.ctrl = ctrl
        if self.lose:
            self.lose -= 1
            return
        decoded = btnet.decode(self.node.ctx, pdu)
        self.heard.append((decoded.src, decoded.seq))
        if self.answers:
            self.node.handle_network_pdu(pdu)

    def _message(self, msg) -> None:
        if msg.opcode == mesh.OP_VD_GROUP_G_GET:
            self.count = (self.count + 1) & 0xFF
            reply = encode_opcode(mesh.OP_VD_GROUP_G_STATUS) + bytes([self.count])
        elif msg.opcode == mesh.OP_ONPOWERUP_SET:
            self.powerup = msg.params[0]
            reply = encode_opcode(mesh.OP_ONPOWERUP_STATUS) + bytes([self.powerup])
        elif msg.opcode == mesh.OP_ONPOWERUP_GET:
            reply = encode_opcode(mesh.OP_ONPOWERUP_STATUS) + bytes([self.powerup])
        elif msg.opcode == mesh.OP_DTT_GET:
            reply = encode_opcode(mesh.OP_DTT_STATUS) + b"\x00"
        elif msg.opcode == mesh.OP_LIGHT_LIGHTNESS_SET:  # the Ledvance scale: the value is the percentage
            self.level = float(int.from_bytes(msg.params[0:2], "little"))
            reply = encode_opcode(mesh.OP_LIGHT_LIGHTNESS_STATUS) + msg.params[0:2]
        elif msg.opcode == mesh.OP_GENERIC_LEVEL_SET:
            self.level = (int.from_bytes(msg.params[0:2], "little", signed=True) + 0x8000) / 2
            reply = encode_opcode(mesh.OP_GENERIC_LEVEL_STATUS) + msg.params[0:2]
        else:
            return
        self.ops.append(msg.opcode)
        self.node.send_access(msg.src, reply)

    def _out(self, pdu: bytes) -> None:
        event = hci.adv_report_event(MAC, hci.mesh_ad(pdu), -60)
        loop = asyncio.get_running_loop()
        loop.call_later(self.delay, self.ctrl.inject, event)
        for later in self.repeats:  # the same PDU again: the cache drops it
            loop.call_later(later, self.ctrl.inject, event)


class Ctx:
    def __init__(self, seq: int = 1000, flick: str = "exclusive") -> None:
        self.state = {"netkey": NETKEY.hex(), "appkey": APPKEY.hex(), "appkey_index": 0, "iv_index": 0,
                      "seq": seq, "provisioner_addr": 0x0001,
                      "devices": {"0x0002": {"address": MAC, "device_key": DEVKEY.hex()}},
                      "settings": {"flick": flick, "exclusive_adapter": ""}}
        self.bulb = SimpleNamespace(id=BID, config={"unicast": "0x0002"})
        self.events: list[tuple] = []
        self.saved: list[int] = []  # state["seq"] at each save: what a crash would restart from
        self.state_path = Path("hauslicht.json")

    def bulbs(self):
        return [self.bulb]

    def save(self):
        self.saved.append(self.state["seq"])

    def flick(self, bid):
        self.events.append(("flick", bid, time.monotonic()))

    def power_returned(self, bid, gap):
        self.events.append(("power", bid, gap))

    def changed(self):
        pass


class FakeScanner:
    def __init__(self) -> None:
        self.starts = self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


async def no_after_connect(address: str) -> None:
    return None


def exclusive_backend(monkeypatch, ctx: Ctx, bulb: MeshBulb | None = None):
    """A mesh backend whose claim hands it a fake controller; returns (backend, holder)."""
    b = MeshBackend(ctx)
    b.after_connect = no_after_connect  # no A9 check in the background
    b.link = b._make_link()
    b.scanner = FakeScanner()
    holder = {"claims": 0}

    async def list_adapters(claimed=None, ignore=()):
        return [dict(BUILTIN, powered=True, in_bluez=True, devices=[], claimed=False)]

    async def claim(name, command=None, timeout=None):
        holder["claims"] += 1
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        holder["ctrl"] = FakeController(theirs, bulb)
        sock_a, sock_b = socket.socketpair()
        holder["helper_end"] = sock_b
        return hci.Claim(ours.detach(), None, sock_a, {"ok": True, "adapter": name})

    monkeypatch.setattr(hci, "list_adapters", list_adapters)
    monkeypatch.setattr(hci, "claim", claim)
    return b, holder


async def wait_for(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def gatt_session(b: MeshBackend, sent: list) -> None:
    """A GATT proxy link as MeshLink.connect leaves it (no radio needed)."""
    b.link._make_node(sent.append)
    b.link.client = SimpleNamespace(is_connected=True)
    b.link.address = MAC
    b.link.connected_at = time.monotonic()


def test_seq_goes_on_from_the_gatt_proxy_to_the_advertising_bearer(monkeypatch):
    async def body():
        ctx, bulb = Ctx(seq=1000), MeshBulb()
        b, holder = exclusive_backend(monkeypatch, ctx, bulb)
        gatt: list[bytes] = []
        gatt_session(b, gatt)
        for _i in range(5):
            b.link.node.send_access(0x0002, encode_opcode(mesh.OP_ONPOWERUP_GET))
        gatt_seqs = [btnet.decode(b.link.node.ctx, p).seq for p in gatt]
        assert gatt_seqs == [1000, 1001, 1002, 1003, 1004]
        reserved = ctx.state["seq"]
        assert reserved > max(gatt_seqs) and ctx.saved[-1] == reserved  # written ahead before use

        assert await b._enter_exclusive("") is True
        assert isinstance(b.link, AdvLink) and b.radio is not None and b.scanner.stops == 1
        assert b.exclusive_status() == {"active": True, "adapter": "hci0", "note": ""}
        ctrl = holder["ctrl"]
        ctrl.advert()
        await wait_for(lambda: b.fresh_device() is not None)
        await b._link_up()  # a session: the boot count over the advertising bearer
        assert b.boot_counts[0x0002] == 8 and b.link.connected
        assert await b.link.set_onpowerup(0x0002, 0) == 0 and bulb.powerup == 0
        adv_seqs = [seq for src, seq in bulb.heard if src == 0x0001]
        assert adv_seqs and min(adv_seqs) >= reserved > max(gatt_seqs)  # no SEQ used twice
        assert adv_seqs == sorted(set(adv_seqs))
        assert ctx.state["seq"] > max(adv_seqs) and ctx.saved == sorted(ctx.saved)
        # each reply arrived twice; the network message cache passed it on once
        assert len(b.link.seen) == len({k for k in b.link.seen})

        await b._leave_exclusive("test")
        assert isinstance(b.link, MeshLink) and not isinstance(b.link, AdvLink) and b.radio is None
        await wait_for(lambda: ctrl.closed)  # the channel closed: the kernel hands the adapter back
        assert holder["helper_end"].recv(16) == b""  # and the helper sees us let go
        assert b.scanner.starts == 1  # BlueZ scanning again
        back: list[bytes] = []
        gatt_session(b, back)
        b.link.node.send_access(0x0002, encode_opcode(mesh.OP_ONPOWERUP_GET))
        assert btnet.decode(b.link.node.ctx, back[0]).seq > max(adv_seqs)  # and on, back on GATT
        for t in b.tasks:
            t.cancel()
    asyncio.run(body())


def test_without_the_helper_exclusive_works_like_fast(monkeypatch):
    async def body():
        ctx = Ctx()
        b, holder = exclusive_backend(monkeypatch, ctx)
        calls = []

        async def failing_claim(name, command=None, timeout=None):
            calls.append(name)
            raise hci.ClaimError("the helper is not installed (system setup v4)")

        monkeypatch.setattr(hci, "claim", failing_claim)
        assert await b._enter_exclusive("") is False
        st = b.exclusive_status()
        assert st["active"] is False and "setup v4" in st["note"]  # (translated around it)
        assert type(b.link) is MeshLink and b.scanner.starts == 1  # GATT and BlueZ scanning go on
        await b._sync_exclusive()
        assert calls == ["hci0"]  # not again right away
        ctx.state["settings"]["exclusive_adapter"] = "hci0"
        await b._sync_exclusive()
        assert calls == ["hci0", "hci0"]  # a changed setting tries again
        ctx.state["settings"]["flick"] = "fast"
        await b._sync_exclusive()
        assert b.exclusive_status()["note"] == ""  # not exclusive any more: no note
    asyncio.run(body())


def test_a_missing_adapter_is_a_note_too(monkeypatch):
    async def body():
        ctx = Ctx()
        ctx.state["settings"]["exclusive_adapter"] = "hci5"
        b, holder = exclusive_backend(monkeypatch, ctx)
        await b._sync_exclusive()
        assert holder["claims"] == 0 and "no Bluetooth adapter hci5" in b.exclusive_status()["note"]
    asyncio.run(body())


def test_idle_and_mode_changes_hand_the_adapter_back(monkeypatch):
    async def body():
        ctx = Ctx()
        b, holder = exclusive_backend(monkeypatch, ctx)
        await b._sync_exclusive()
        assert b.radio is not None and holder["claims"] == 1
        await b.idle(True)
        assert b.radio is None and holder["ctrl"].sock.fileno() >= 0
        await wait_for(lambda: holder["ctrl"].closed)
        await b.idle(False)  # driven again: taken again at once
        assert b.radio is not None and holder["claims"] == 2
        ctx.state["settings"]["flick"] = "on"
        await b._sync_exclusive()
        assert b.radio is None and type(b.link) is MeshLink
        ctx.state["settings"]["flick"] = "exclusive"
        await b._sync_exclusive()
        assert b.radio is not None and holder["claims"] == 3
        b.radio._die("controller hardware error 0x01")  # unplugged, reset...
        await b._sync_exclusive()  # let go and taken again
        assert b.radio is not None and not b.radio.dead and holder["claims"] == 4
        await b.stop()
        assert b.radio is None
        await wait_for(lambda: holder["ctrl"].closed)
    asyncio.run(body())


async def adverts_every(ctrl: FakeController, period: float, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctrl.advert()
        await asyncio.sleep(period)


def test_a_short_silence_with_a_fresh_boot_count_is_a_flick(monkeypatch):
    """The re-arm path without a connection: silence, the bulb heard again, a
    session, the boot count read over the advertising bearer: a flick."""
    async def body():
        ctx, bulb = Ctx(), MeshBulb()
        b, holder = exclusive_backend(monkeypatch, ctx, bulb)
        b.online = True
        await b._enter_exclusive("")
        ctrl = holder["ctrl"]
        await adverts_every(ctrl, 0.05, 0.3)
        await b._link_up()
        await adverts_every(ctrl, 0.05, 0.4)  # the rhythm learnt: suspect after 0.9 s
        assert abs(b.adv_interval() - 0.05) < 0.03 and b.adv_suspect_s() == mesh.ADV_SUSPECT_MIN_S
        bulb.reboot()
        await asyncio.sleep(0.9)  # off, dead, on again
        back = time.monotonic()
        ctrl.advert()
        await wait_for(lambda: ctx.events, 3.0)
        kind, bid, at = ctx.events[0]
        assert (kind, bid) == ("flick", BID) and b.boot_counts[0x0002] == 1
        assert at - back < 0.6  # advert -> verdict on the fake: TX windows and one reply
        for t in b.tasks:
            t.cancel()
        await b.stop()
    asyncio.run(body())


def test_a_short_silence_with_the_count_going_on_is_no_flick(monkeypatch):
    async def body():
        ctx, bulb = Ctx(), MeshBulb()
        b, holder = exclusive_backend(monkeypatch, ctx, bulb)
        b.online = True
        await b._enter_exclusive("")
        ctrl = holder["ctrl"]
        await adverts_every(ctrl, 0.05, 0.3)
        await b._link_up()
        await adverts_every(ctrl, 0.05, 0.3)
        await asyncio.sleep(1.0)  # adverts lost, the bulb kept power
        ctrl.advert()
        await wait_for(lambda: b.verdict_at.get(0x0002, 0) > time.monotonic() - 1 and not b.suspects, 3.0)
        assert ctx.events == [] and b.boot_counts[0x0002] == 9
        for t in b.tasks:
            t.cancel()
        await b.stop()
    asyncio.run(body())


def test_a_long_silence_is_a_lost_bulb_and_its_return_a_power_return(monkeypatch):
    async def body():
        monkeypatch.setattr(mesh, "ADV_LOST_S", 0.5)
        monkeypatch.setattr(mesh, "WATCH_S", 0.05)
        ctx, bulb = Ctx(), MeshBulb()
        b, holder = exclusive_backend(monkeypatch, ctx, bulb)
        b.online = True
        await b._enter_exclusive("")
        ctrl = holder["ctrl"]
        await adverts_every(ctrl, 0.05, 0.3)
        await b._link_up()
        b.tasks.append(asyncio.get_running_loop().create_task(b.watch()))
        last = time.monotonic()
        bulb.reboot()
        await wait_for(lambda: b.online is False, 2.0)  # the watch: silent too long
        assert b.lost_at <= last + 0.06  # the drop is the last advert heard, not the watch's tick
        await asyncio.sleep(0.3)
        ctrl.advert()
        await wait_for(lambda: ctx.events, 3.0)
        kind, bid, gap = ctx.events[0]
        assert (kind, bid) == ("power", BID) and 0.7 < gap < 2.0  # a quick one: the core toggles
        for t in b.tasks:
            t.cancel()
        if b.presence_task:
            b.presence_task.cancel()
        await b.stop()
    asyncio.run(body())


# ------------------------------------------------------------------ 2026-10-20 afternoon (hardware)


async def exclusive_session(monkeypatch, bulb: MeshBulb | None = None):
    """Exclusive taken, the rhythm heard, one session with the boot-count reference."""
    ctx, bulb = Ctx(), bulb or MeshBulb()
    b, holder = exclusive_backend(monkeypatch, ctx, bulb)
    b.online = True
    await b._enter_exclusive("")
    ctrl = holder["ctrl"]
    await adverts_every(ctrl, 0.05, 0.3)
    await b._link_up()
    return ctx, bulb, b, ctrl


async def finish(b: MeshBackend) -> None:
    for t in b.tasks:
        t.cancel()
    if b.presence_task:
        b.presence_task.cancel()
    await b.stop()


@pytest.mark.parametrize("interval", [0.06, 0.12, 0.18, 0.21])
def test_the_silence_threshold_stays_below_the_shortest_reboot_silence(interval):
    """12:00-16:05: the learnt rhythm was 0.06-0.21 s, real reboots were silent
    1.01-1.79 s. At 0.21 s the old 5 x rhythm (1.05 s) would have let the
    1.01 s reboot of 12:02:36 pass unchecked."""
    b = MeshBackend(Ctx())
    b.adv_gaps.extend([interval] * 32)
    assert b.adv_suspect_s() == mesh.ADV_SUSPECT_MIN_S < 1.01


def test_a_slow_advertiser_gets_a_longer_threshold():
    b = MeshBackend(Ctx())
    b.adv_gaps.extend([0.5] * 32)
    assert b.adv_suspect_s() == pytest.approx(mesh.ADV_SUSPECT_FACTOR * 0.5)


def test_a_silence_below_the_threshold_is_not_checked(monkeypatch):
    """1786 checks in 4 h, 576 of them for silences of 0.6-0.9 s, all the same boot."""
    async def body():
        ctx, bulb, b, ctrl = await exclusive_session(monkeypatch)
        await adverts_every(ctrl, 0.05, 0.3)
        heard = len(bulb.heard)
        await asyncio.sleep(0.7)  # ~0.75 s without an advert
        ctrl.advert()
        await asyncio.sleep(0.3)
        assert not b.suspects and len(bulb.heard) == heard and ctx.events == []
        await finish(b)
    asyncio.run(body())


def test_a_late_repeat_of_an_earlier_reply_is_not_the_next_count(monkeypatch):
    """13:39:56 and 15:18:52: the silence check right after the boot-count
    clearing read "5 -> 5": a late copy of the clearing's last Status (the
    bulb repeats its PDUs) answered the new session's Get, whose fresh node
    and empty message cache took it. "unknown", counted as a reboot: the
    light toggled a second time. A session now keeps the link's node and
    cache: the copy is dropped and the Get gets its own answer."""
    async def body():
        bulb = MeshBulb()
        bulb.delay, bulb.repeats = 0.15, (0.25,)  # the copy comes after the next Get is out
        ctx, bulb, b, ctrl = await exclusive_session(monkeypatch, bulb)
        assert b.boot_counts[2] == 8
        b.offbeat(ctx.bulb, time.monotonic())  # a silence at once (13:39:55.8)
        await wait_for(lambda: not b.suspects, 3.0)
        assert b.boot_counts[2] == 9 and b.boot_verdicts[2][1] == mesh.SAME
        assert ctx.events == []
        await finish(b)
    asyncio.run(body())


def test_sessions_share_the_node_and_reserve_no_seq(monkeypatch):
    """Each check opened a session with a fresh MeshNode: 64 SEQ reserved and
    hauslicht.json written each time (1786 times in 4 h: ~0.7 M of the 24-bit
    SEQ space a day, used up in under a month)."""
    async def body():
        ctx, bulb, b, ctrl = await exclusive_session(monkeypatch)
        saved, node = list(ctx.saved), b.link.node
        for _i in range(5):
            await b.link.close()
            await b._link_up()
            assert b.link.connected and b.link.node is node
        assert b.boot_verdicts[2][1] == mesh.SAME and ctx.events == []
        seqs = [seq for src, seq in bulb.heard if src == 0x0001]
        assert len(seqs) == 6 and seqs == list(range(seqs[0], seqs[0] + 6))  # one Get each, SEQ on by one
        assert ctx.saved == saved  # nothing reserved or written for a session
        await finish(b)
    asyncio.run(body())


def test_a_same_boot_session_keeps_the_onpowerup_read(monkeypatch):
    """Each same-boot check also read OnPowerUp again (the GATT rule: once per
    link). A session is no new connection: the read holds until a reboot."""
    async def body():
        ctx, bulb, b, ctrl = await exclusive_session(monkeypatch)
        assert await b.set_powerup(ctx.bulb, "on")
        reads = bulb.ops.count(mesh.OP_ONPOWERUP_GET)
        assert reads == 1
        await b.link.close()
        await b._link_up()  # same boot
        assert await b.set_powerup(ctx.bulb, "on")
        assert bulb.ops.count(mesh.OP_ONPOWERUP_GET) == reads
        await b.link.close()
        bulb.reboot()
        await b._link_up()  # a reboot: the core arms anew, read first
        assert b.boot_verdicts[2][1] == mesh.REBOOTED
        assert await b.set_powerup(ctx.bulb, "on")
        assert bulb.ops.count(mesh.OP_ONPOWERUP_GET) == reads + 1
        await finish(b)
    asyncio.run(body())


def test_an_unanswered_read_while_the_bulb_adverts_asks_again(monkeypatch):
    """15:18:36-41 (45 times that afternoon): the check's Get went unanswered
    while the bulb went on advertising; the GATT rule (notifications failed:
    lost, reconnect) marked it unreachable and decided only on a "return"
    5 s later. Exclusive: the bulb is heard, so a new session asks again."""
    monkeypatch.setattr(mesh, "BOOT_TIMEOUT_S", 0.2)

    async def body():
        ctx, bulb, b, ctrl = await exclusive_session(monkeypatch)
        adverts = asyncio.get_running_loop().create_task(adverts_every(ctrl, 0.05, 3.0))
        bulb.answers = False
        b.offbeat(ctx.bulb, time.monotonic())
        await asyncio.sleep(0.3)
        bulb.answers = True
        await wait_for(lambda: not b.suspects, 2.5)
        assert b.online is True and b.presence_task is None
        assert b.boot_verdicts[2][1] == mesh.SAME and ctx.events == []
        adverts.cancel()
        await finish(b)
    asyncio.run(body())


# ------------------------------------------------------------------ 2026-10-21: commands sent late


async def armed_session(monkeypatch, bulb: MeshBulb | None = None):
    """exclusive_session after the first arm: levels go out at once, none held."""
    ctx, bulb, b, ctrl = await exclusive_session(monkeypatch, bulb)
    assert await b.set_powerup(ctx.bulb, "on")
    return ctx, bulb, b, ctrl


def test_a_silence_check_never_cancels_a_set_in_flight(monkeypatch):
    """10:50:59 and 10:51:01: a clap's Set went out, the bulb was heard again
    after its quiet following the OnPowerUp Set ("silent 1.23 s ...
    rebooted?"), and the check cancelled the Set "on the old link": sent again
    after the boot-count read, 1.8 s late. The advertising bearer has no link
    to lose: the Set goes on, is answered, and the check runs after it."""
    async def body():
        bulb = MeshBulb()
        bulb.delay, bulb.repeats = 0.2, ()
        ctx, bulb, b, ctrl = await armed_session(monkeypatch, bulb)
        start = time.monotonic()
        task = asyncio.get_running_loop().create_task(b.apply(ctx.bulb, 100.0))
        await asyncio.sleep(0.1)  # the Set is out, its Status (0.2 s) not back yet
        assert bulb.ops.count(mesh.OP_LIGHT_LIGHTNESS_SET) == 1 and bulb.level == 100.0
        b.offbeat(ctx.bulb, time.monotonic())
        await task  # no Offline: nothing cancelled
        assert time.monotonic() - start < 0.8 and bulb.level == 100.0
        assert bulb.ops.count(mesh.OP_LIGHT_LIGHTNESS_SET) == 1
        await wait_for(lambda: not b.suspects, 3.0)  # the check, after the Set
        assert b.boot_verdicts[2][1] == mesh.SAME and ctx.events == []
        await finish(b)
    asyncio.run(body())


def test_a_lost_set_is_sent_again_within_half_a_second(monkeypatch):
    """10:51:15: the clap's Set was lost on the air and sent again only after
    REQUEST_TIMEOUT_S (3 s, the GATT timing), right before the next clap's
    off: on at 10:51:18.50, off at 18.75, the light never visibly changed.
    Adverts get lost; the advertising bearer asks again much sooner."""
    async def body():
        ctx, bulb, b, ctrl = await armed_session(monkeypatch)
        bulb.lose = 1
        start = time.monotonic()
        await b.apply(ctx.bulb, 100.0)
        assert time.monotonic() - start < mesh.ADV_REQUEST_RETRY_S + 0.4 and bulb.level == 100.0
        bulb.lose = 2
        start = time.monotonic()
        await b.apply(ctx.bulb, 0.0)  # off: a Generic Level Set
        assert time.monotonic() - start < 2 * mesh.ADV_REQUEST_RETRY_S + 0.4 and bulb.level == 0.0
        await finish(b)
    asyncio.run(body())


def test_a_silence_check_still_cancels_its_own_reads(monkeypatch):
    """Only the user's Sets are kept: a boot-count read in flight is cancelled
    as before (the verdict path reads anew at once), so a real flick during a
    clearing is never decided late."""
    monkeypatch.setattr(mesh, "BOOT_TIMEOUT_S", 1.0)

    async def body():
        ctx, bulb, b, ctrl = await armed_session(monkeypatch)
        bulb.answers = False
        read = asyncio.get_running_loop().create_task(b.link.boot_count(0x0002))
        await asyncio.sleep(0.1)
        assert b.link.abort(keep_sets=True) == 1
        with pytest.raises(mesh.LinkAborted):
            await read
        await finish(b)
    asyncio.run(body())


# ------------------------------------------------------------------ the daemon side


def test_daemon_setting_adapters_command_and_get(tmp_path, monkeypatch):
    from test_core import cmd, mock_state, run
    from haus.common import UserError

    async def list_adapters(claimed=None, ignore=()):
        return [dict(BUILTIN, powered=True, in_bluez=True, devices=["Headphones"], claimed=False),
                dict(DONGLE, powered=True, in_bluez=True, devices=[], claimed=False)]

    monkeypatch.setattr(hci, "list_adapters", list_adapters)

    async def body(d):
        g = await cmd(d, "get")
        assert g["exclusive_adapter"] == "" and g["exclusive"] == {"active": False, "adapter": None, "note": ""}
        (a,) = (await d.command({"cmd": "adapters", "args": []}))["results"]
        assert a["chosen"] == "hci1" and a["others_stop"] is False and len(a["adapters"]) == 2
        g = await cmd(d, "set", f"exclusive_adapter={CONTROLLER.lower()}")
        assert g["exclusive_adapter"] == CONTROLLER
        (a,) = (await d.command({"cmd": "adapters", "args": []}))["results"]
        assert a["chosen"] == "hci0" and a["others_stop"] is True and a["selected"] == CONTROLLER
        with pytest.raises(UserError):
            await cmd(d, "set", "exclusive_adapter=wlan0")
        g = await cmd(d, "set", "exclusive_adapter=auto")
        assert g["exclusive_adapter"] == ""
    run(tmp_path, mock_state(mock_1={}), body)
