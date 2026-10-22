"""Tuya backend against a fake protocol-3.3 device on a local socket (frames
packed and encrypted with tinytuya's own helpers) and fake UDP announcements
on loopback. No LAN traffic."""

import asyncio
import json
import socket
import threading

import pytest

pytest.importorskip("tinytuya")
import tinytuya  # noqa: E402
from tinytuya.core.crypto_helper import AESCipher  # noqa: E402
from tinytuya.core.message_helper import TuyaMessage, pack_message, parse_header, unpack_message  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends.tuya import TuyaBackend  # noqa: E402
from haus.common import UserError  # noqa: E402
from lan_fakes import FakeCtx, bulb  # noqa: E402

KEY = "0123456789abcdef"
DEV_ID = "bf0123456789abcdefgh"
HEADER = b"3.3" + b"\0" * 12


class FakeTuya:
    """A protocol 3.3 bulb: answers DP_QUERY (10), CONTROL (7), HEART_BEAT (9)."""

    def __init__(self, dps):
        self.dps = dict(dps)
        self.controls: list[dict] = []
        self.cipher = AESCipher(KEY.encode())
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes accept(); close() alone keeps listening
        except OSError:
            pass
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._conn, args=(conn,), daemon=True).start()

    def _reply(self, conn, seq, cmd, body: dict | None, header=False):
        payload = b""
        if body is not None:
            payload = self.cipher.encrypt(json.dumps(body).encode(), use_base64=False)
            if header:
                payload = HEADER + payload
        payload = (0).to_bytes(4, "big") + payload  # device frames carry a return code
        conn.sendall(pack_message(TuyaMessage(seq, cmd, 0, payload, 0, True, 0x55AA, None)))

    def _conn(self, conn):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while len(buf) >= 16:
                    head = parse_header(buf)
                    if len(buf) < head.total_length:
                        break
                    frame, buf = buf[:head.total_length], buf[head.total_length:]
                    msg = unpack_message(frame, no_retcode=True)
                    payload = msg.payload
                    if payload.startswith(b"3.3"):
                        payload = payload[15:]
                    if msg.cmd == 9:
                        self._reply(conn, msg.seqno, 9, None)
                        continue
                    try:
                        req = json.loads(self.cipher.decrypt(payload, use_base64=False))
                    except Exception:  # wrong key: a real device just stays silent
                        return
                    if msg.cmd == 10:
                        self._reply(conn, msg.seqno, 10, {"devId": DEV_ID, "dps": self.dps})
                    elif msg.cmd == 7:
                        self.controls.append(req["dps"])
                        self.dps.update(req["dps"])
                        self._reply(conn, msg.seqno, 7, {"devId": DEV_ID, "dps": req["dps"]}, header=True)


def config(dev, **extra):
    return {"host": "127.0.0.1", "port": dev.port, "device_id": DEV_ID, "local_key": KEY,
            "version": "3.3", "bulb_type": "auto", **extra}


def run(coro):
    return asyncio.run(coro)


def test_type_b_bulb():
    dev = FakeTuya({"20": False, "21": "colour", "22": 500, "23": 0})

    async def main():
        be = TuyaBackend(FakeCtx())
        cfg = await be.add(config(dev))
        assert cfg["bulb_type"] == "B" and cfg["id"] == DEV_ID
        b = bulb("tuya", cfg["id"], **cfg)
        assert be.min_lit(b) == 1.0
        await be.apply(b, 1)
        assert dev.dps["20"] is True and dev.dps["21"] == "white" and dev.dps["22"] == 10
        await be.apply(b, 55.5)
        assert dev.dps["22"] == 555
        await be.apply(b, 0)
        assert dev.dps["20"] is False
        assert await be.query(b) == {"on": False, "percent": 0.0}
        assert await be.set_powerup(b, "on") is False
        await be.stop()

    try:
        run(main())
    finally:
        dev.close()
    # one kept-open socket for the bulb (plus the one add used)
    assert dev.connections <= 2


def test_type_a_bulb_floor():
    dev = FakeTuya({"1": True, "2": "white", "3": 255})

    async def main():
        be = TuyaBackend(FakeCtx())
        cfg = await be.add(config(dev))
        assert cfg["bulb_type"] == "A"
        b = bulb("tuya", cfg["id"], **cfg)
        assert be.min_lit(b) == 9.8
        await be.apply(b, 2)  # below the hardware floor: clamped to 25
        assert dev.dps["3"] == 25
        await be.apply(b, 100)
        assert dev.dps["3"] == 255
        await be.stop()

    try:
        run(main())
    finally:
        dev.close()


def test_unknown_dps_and_unreachable():
    dev = FakeTuya({"101": True})

    async def main():
        be = TuyaBackend(FakeCtx())
        with pytest.raises(UserError, match="no known bulb data points"):
            await be.add(config(dev))
        dev.close()
        b = bulb("tuya", "x", **config(dev, bulb_type="B"))
        with pytest.raises(Offline):
            await be.apply(b, 50)
        assert be.status(b)["online"] is False

    run(main())


def test_discovery_listens_on_loopback():
    be = TuyaBackend(FakeCtx())
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    be.udp_ports = (port,)
    be.udp_bind = "127.0.0.1"
    announce = json.dumps({"ip": "192.0.2.20", "gwId": DEV_ID, "active": 2, "ability": 0,
                           "mode": 0, "encrypt": True, "productKey": "key", "version": "3.3"}).encode()
    packet = AESCipher(tinytuya.udpkey).encrypt(announce, use_base64=False)

    async def main():
        task = asyncio.create_task(be.discover(1.0))
        await asyncio.sleep(0.3)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(2):
            tx.sendto(packet, ("127.0.0.1", port))
        tx.sendto(b"noise", ("127.0.0.1", port))
        tx.close()
        return await task

    found = run(main())
    assert found == [{"kind": "tuya", "address": "192.0.2.20", "name": f"Tuya {DEV_ID}",
                      "config": {"host": "192.0.2.20", "device_id": DEV_ID, "version": "3.3"}}]


def test_colour_caps_per_device():
    cases = [({"20": True, "21": "white", "22": 500}, []),
             ({"20": True, "21": "white", "22": 500, "23": 0}, ["ct"]),
             ({"20": True, "21": "colour", "22": 500, "23": 0, "24": "000003e803e8"}, ["ct", "hs"]),
             ({"1": True, "2": "white", "3": 255, "5": "ff00000000ffff"}, ["hs"])]
    for dps, caps in cases:
        dev = FakeTuya(dps)

        async def main():
            be = TuyaBackend(FakeCtx())
            cfg = await be.add(config(dev))
            assert cfg["color"] == caps, dps
            b = bulb("tuya", cfg["id"], **cfg)
            assert be.caps(b) == {"onoff", "dim"} | set(caps)
            assert be.ct_range(b) == (2700, 6500)  # Tuya reports no Kelvin range
            await be.stop()

        try:
            run(main())
        finally:
            dev.close()


def test_colour_type_b_messages_and_dark_light():
    dev = FakeTuya({"20": False, "21": "white", "22": 500, "23": 0, "24": "000003e803e8"})

    async def main():
        ctx = FakeCtx()
        be = TuyaBackend(ctx)
        cfg = await be.add(config(dev))
        cfg.pop("color")  # an older config
        b = bulb("tuya", cfg["id"], **cfg)
        hs = {"mode": "hs", "hue": 120, "sat": 50}
        assert await be.apply_color(b, hs) is False  # not driven yet
        await be.apply(b, 0)
        assert await be.apply_color(b, hs) is False  # dark: never switched on
        await be.query(b)  # a poll: the older config learns it
        assert b.config["color"] == ["ct", "hs"]
        n = len(dev.controls)
        await be.apply(b, 0)
        assert await be.apply_color(b, hs) is False and len(dev.controls) == n + 1
        assert dev.dps["20"] is False
        await be.apply(b, 40)
        assert await be.apply_color(b, hs) is True
        assert dev.controls[-1] == {"21": "colour", "24": "007801f40190"}  # no switch DP
        await be.apply(b, 60)  # colour mode: the level is the colour's value
        assert dev.controls[-1] == {"20": True, "21": "colour", "24": "007801f40258"}
        assert await be.apply_color(b, {"mode": "ct", "kelvin": 4600}) is True
        assert dev.controls[-1] == {"21": "white", "22": 600, "23": 500}
        await be.apply_color(b, {"mode": "ct", "kelvin": 9000})  # clamped to the range
        assert dev.controls[-1]["23"] == 1000
        await be.apply(b, 30)  # white again
        assert dev.controls[-1] == {"20": True, "21": "white", "22": 300}
        await be.stop()

    try:
        run(main())
    finally:
        dev.close()


def test_colour_type_a_message():
    dev = FakeTuya({"1": True, "2": "white", "3": 255, "4": 0, "5": "ff00000000ffff"})

    async def main():
        be = TuyaBackend(FakeCtx())
        cfg = await be.add(config(dev))
        b = bulb("tuya", cfg["id"], **cfg)
        await be.apply(b, 100)
        assert await be.apply_color(b, {"mode": "hs", "hue": 0, "sat": 100}) is True
        assert dev.controls[-1] == {"2": "colour", "5": "ff00000000ffff"}
        await be.apply(b, 50)
        assert dev.controls[-1] == {"1": True, "2": "colour", "5": "8000000000ff80"}  # rgb 128,0,0; h 0, s 255, v 128
        assert await be.apply_color(b, {"mode": "ct", "kelvin": 2700}) is True
        assert dev.controls[-1] == {"2": "white", "3": 128, "4": 0}
        await be.stop()

    try:
        run(main())
    finally:
        dev.close()
