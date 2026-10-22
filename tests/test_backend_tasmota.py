"""Tasmota backend against a local aiohttp mock of `/cm`."""

import asyncio
import time

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from haus.backends import Offline  # noqa: E402
from haus.backends import tasmota as tasmota_mod  # noqa: E402
from haus.backends.tasmota import TasmotaBackend  # noqa: E402
from haus.common import UserError  # noqa: E402
from lan_fakes import FakeCtx, bulb, serve  # noqa: E402


class FakeTasmota:
    def __init__(self, password=None, dimmer=True, rgb=False, ct=False):
        self.password = password
        self.dimmer = dimmer
        self.rgb, self.ct = rgb, ct  # light channels: RGB (HSBColor), white temperature (CT)
        self.hsb, self.mired = "0,0,100", 300
        self.power = False
        self.level = 100
        self.power_on_state = 3
        self.uptime = 3600
        self.log: list[tuple[str, float]] = []

    def app(self):
        async def cm(request):
            q = request.query
            if self.password and (q.get("user") != "admin" or q.get("password") != self.password):
                return web.json_response({"WARNING": "Need user=<username>&password=<password>"}, status=401)
            cmnd = q["cmnd"]
            self.log.append((cmnd, time.monotonic()))
            word, _, arg = cmnd.partition(" ")
            if word == "Dimmer":
                self.level = int(arg)
                self.power = self.level > 0
                return web.json_response({"POWER": "ON" if self.power else "OFF", "Dimmer": self.level})
            if word == "Power":
                self.power = arg.upper() == "ON"
                return web.json_response({"POWER": "ON" if self.power else "OFF"})
            if word == "HSBColor" and self.rgb:
                h, s_, b = arg.split(",")
                self.hsb, self.level, self.power = arg, int(b), True  # SetOption20 0: a colour lights it
                return web.json_response({"POWER": "ON", "Dimmer": self.level, "HSBColor": self.hsb})
            if word == "CT" and self.ct:
                self.mired, self.power = int(arg), True
                return web.json_response({"POWER": "ON", "Dimmer": self.level, "CT": self.mired})
            if word == "PowerOnState":
                self.power_on_state = int(arg)
                return web.json_response({"PowerOnState": self.power_on_state})
            if cmnd == "Status 11":
                sts = {"UptimeSec": self.uptime, "POWER": "ON" if self.power else "OFF"}
                if self.dimmer:
                    sts["Dimmer"] = self.level
                if self.rgb:
                    sts["HSBColor"] = self.hsb
                if self.ct:
                    sts["CT"] = self.mired
                return web.json_response({"StatusSTS": sts})
            if cmnd == "Status 5":
                return web.json_response({"StatusNET": {"Mac": "DE:AD:BE:EF:00:01", "IPAddress": "127.0.0.1"}})
            return web.json_response({"Command": "Unknown"})

        app = web.Application()
        app.router.add_get("/cm", cm)
        return app


def run(coro):
    return asyncio.run(coro)


def test_add_apply_powerup_query():
    async def main():
        dev = FakeTasmota(password="pw")
        runner, host = await serve(dev.app())
        be = TasmotaBackend(FakeCtx())
        try:
            with pytest.raises(UserError, match="password"):
                await be.add({"host": host})
            config = await be.add({"host": host, "username": "admin", "password": "pw"})
            assert config["id"] == "deadbeef0001"
            b = bulb("tasmota", config["id"], **config)
            await be.apply(b, 12.6)
            assert dev.power and dev.level == 13
            await be.apply(b, 0)
            assert not dev.power and dev.log[-1][0] == "Power OFF"
            for value, code in (("off", 0), ("on", 1), ("restore", 3)):
                assert await be.set_powerup(b, value) and dev.power_on_state == code
            assert await be.query(b) == {"on": False, "percent": 0.0}
        finally:
            await be.stop()
            await runner.cleanup()
    run(main())


def test_relay_without_dimmer_is_refused():
    async def main():
        runner, host = await serve(FakeTasmota(dimmer=False).app())
        try:
            with pytest.raises(UserError, match="no dimmable"):
                await TasmotaBackend(FakeCtx()).add({"host": host})
        finally:
            await runner.cleanup()
    run(main())


def test_offline_power_return_and_rate_limit():
    async def main():
        dev = FakeTasmota()
        runner, host = await serve(dev.app())
        ctx = FakeCtx()
        be = TasmotaBackend(ctx)
        b = bulb("tasmota", "x", host=host)
        try:
            for p in (10, 20, 30):
                await be.apply(b, p)
            times = [t for (_, t) in dev.log]
            assert all(t2 - t1 >= 0.18 for t1, t2 in zip(times, times[1:]))
            await be._probe(b)
            dev.uptime = 3
            ctx.events.clear()
            await be._probe(b)
            assert [e[0] for e in ctx.events if e[0] == "returned"] == ["returned"]
            await runner.cleanup()
            with pytest.raises(Offline):
                await be.apply(b, 50)
            assert be.status(b)["online"] is False
        finally:
            await be.stop()
            await runner.cleanup()
    run(main())


def test_discover_filters_tasmota_names(monkeypatch):
    async def fake_browse(types, timeout):
        return [{"type": "_http._tcp.local.", "name": "tasmota-1A2B3C-1234", "addresses": ["192.0.2.7"],
                 "port": 80, "server": "", "properties": {}},
                {"type": "_http._tcp.local.", "name": "printer", "addresses": ["192.0.2.8"],
                 "port": 80, "server": "", "properties": {}}]

    monkeypatch.setattr(tasmota_mod, "mdns_browse", fake_browse)
    found = run(TasmotaBackend(FakeCtx()).discover(0.1))
    assert found == [{"kind": "tasmota", "address": "192.0.2.7", "name": "tasmota-1A2B3C-1234",
                      "config": {"host": "192.0.2.7"}}]


@pytest.mark.parametrize("rgb, ct, caps", [(False, False, set()), (False, True, {"ct"}),
                                           (True, False, {"hs"}), (True, True, {"ct", "hs"})])
def test_colour_caps_from_status(rgb, ct, caps):
    async def main():
        runner, host = await serve(FakeTasmota(rgb=rgb, ct=ct).app())
        be = TasmotaBackend(FakeCtx())
        try:
            config = await be.add({"host": host})
            b = bulb("tasmota", "x", **config)
            assert be.caps(b) == {"onoff", "dim", "powerup", "flick"} | caps
            assert be.ct_range(b) == (2000, 6536)
        finally:
            await be.stop()
            await runner.cleanup()
    run(main())


def test_colour_commands_keep_level_and_dark_light():
    async def main():
        dev = FakeTasmota(rgb=True, ct=True)
        runner, host = await serve(dev.app())
        be = TasmotaBackend(FakeCtx())
        b = bulb("tasmota", "x", host=host)  # an older config: the next poll learns the colour
        try:
            assert be.caps(b) == {"onoff", "dim", "powerup", "flick"}
            await be._probe(b)
            assert b.config["color"] == ["ct", "hs"]
            assert await be.apply_color(b, {"mode": "ct", "kelvin": 3000}) is False  # not lit: nothing
            assert not dev.power and all(c[0].split()[0] not in ("CT", "HSBColor") for c in dev.log)
            await be.apply(b, 37.4)
            assert await be.apply_color(b, {"mode": "hs", "hue": 359.6, "sat": 42.2})
            assert dev.log[-1][0] == "HSBColor 0,42,37" and dev.level == 37
            assert await be.apply_color(b, {"mode": "ct", "kelvin": 1000})  # clamped to 500 mired
            assert dev.log[-1][0] == "CT 500" and dev.level == 37
            assert await be.apply_color(b, {"mode": "ct", "kelvin": 4000})
            assert dev.log[-1][0] == "CT 250"
            await be.apply(b, 0)
            n = len(dev.log)
            assert await be.apply_color(b, {"mode": "hs", "hue": 10, "sat": 10}) is False
            assert len(dev.log) == n and not dev.power
        finally:
            await be.stop()
            await runner.cleanup()
    run(main())
