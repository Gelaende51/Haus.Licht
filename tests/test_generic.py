"""Generic backend: every transport against local fakes (nothing leaves 127.0.0.1)."""

import asyncio
import json
import shlex
import socket
import sys
from types import SimpleNamespace

import pytest

from haus.backends import Info, Offline, check_fields
from haus.backends import generic
from haus.backends import serial as sbackend
from haus.backends.generic import GenericBackend
from haus.common import UserError


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "en")
    monkeypatch.setattr(generic, "tr", lambda en, de: en)  # the daemon's language is fixed at import


class FakeCtx:
    def __init__(self):
        self.list, self.changes = [], 0

    def bulbs(self):
        return self.list

    def changed(self):
        self.changes += 1

    def save(self):
        pass


def bulb(cfg, bid="generic:1"):
    return SimpleNamespace(id=bid, name="G", config=cfg)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------ templates

@pytest.mark.parametrize("template", ["{bogus}", "{percent", "{}", "{0}", "{percent.real}", "{level!r}",
                                      "{level:q}", "}"])
def test_bad_templates_rejected(template):
    with pytest.raises(UserError):
        generic.check_template(template, "On")


def test_good_templates():
    for t in ("{percent}", "{{literal}} {level:02x}", "{r},{g},{b} {r_dim} {on} {hue} {sat} {kelvin}", "plain"):
        generic.check_template(t, "On")
    assert "{{x}}".format_map(generic.values(50, (1, 1, 1), 0, 0, 4000)) == "{x}"


def test_values_are_numbers():
    v = generic.values(50, (1.0, 0.0, 0.5), 300, 100, 3000)
    assert all(isinstance(x, int) for x in v.values())
    assert v == {"percent": 50, "level": 128, "hue": 300, "sat": 100, "kelvin": 3000, "r": 255, "g": 0,
                 "b": 128, "r_dim": 128, "g_dim": 0, "b_dim": 64, "on": 1}
    assert generic.values(0, (1, 1, 1), 0, 0, 3000)["on"] == 0


def test_unescape():
    assert generic.unescape("A\\x00\\xff{\\n\\r\\t\\\\é") == b"A\x00\xff{\n\r\t\\" + "é".encode()
    with pytest.raises(UserError):
        generic.check_escapes("\\xZ1", "On")


def test_caps_follow_templates():
    be = GenericBackend(FakeCtx())
    assert be.caps(bulb({"on": "x", "off": "y"})) == {"onoff"}
    assert be.caps(bulb({"brightness": "x"})) == {"onoff", "dim"}
    assert be.caps(bulb({"brightness": "x", "colour": "c", "white": "w"})) == {"onoff", "dim", "hs", "ct"}
    assert be.step(bulb({"brightness": "B{level}"})) == pytest.approx(100 / 255)
    assert be.step(bulb({"brightness": "B{percent}"})) == 1.0
    assert be.ct_range(bulb({"min_kelvin": 2000, "max_kelvin": 5000})) == (2000, 5000)


def test_add_validation():
    be = GenericBackend(FakeCtx())
    py = shlex.quote(sys.executable)
    with pytest.raises(UserError, match="on or a brightness"):
        run(be.add({"transport": "command"}))
    with pytest.raises(UserError, match="off template"):
        run(be.add({"transport": "command", "on": f"{py} -c pass"}))
    with pytest.raises(UserError, match="unknown placeholder"):
        run(be.add({"transport": "command", "brightness": f"{py} -c pass {{watts}}"}))
    with pytest.raises(UserError, match="no program"):
        run(be.add({"transport": "command", "brightness": "/no/such/prog {percent}"}))
    with pytest.raises(UserError):
        run(be.add({"transport": "command", "brightness": f"{py} 'unclosed"}))
    with pytest.raises(UserError, match="http"):
        run(be.add({"transport": "http", "url": "ftp://x", "brightness": "{percent}"}))
    with pytest.raises(UserError):
        run(be.add({"transport": "http", "url": "http://127.0.0.1/", "headers": "nocolon",
                    "brightness": "{percent}"}))
    with pytest.raises(UserError):
        run(be.add({"transport": "udp", "host": "127.0.0.1", "udp_port": 0, "brightness": "{percent}"}))
    with pytest.raises(UserError):
        run(be.add({"transport": "smoke", "brightness": "{percent}"}))
    with pytest.raises(UserError, match="White"):
        run(be.add({"transport": "udp", "host": "127.0.0.1", "udp_port": 9, "brightness": "{percent}",
                    "white": "{kelvin}", "min_kelvin": 6000, "max_kelvin": 3000}))
    cfg = run(be.add({"transport": "udp", "host": "127.0.0.1", "udp_port": 9, "brightness": "{percent}"}))
    assert cfg["off"] == "" and "id" not in cfg


# ------------------------------------------------------------ form attributes

def test_describe_carries_when_and_placeholder():
    fields = {f["key"]: f for f in Info("generic", GenericBackend, []).describe()["fields"]}
    assert fields["url"]["when"] == {"transport": ["http"]}
    assert "{percent}" in fields["brightness"]["placeholder"]
    assert all(f.get("label_de") for f in fields.values())


def test_check_fields_skips_hidden():
    cfg = check_fields(GenericBackend, {"transport": "udp", "host": "h", "udp_port": "5",
                                        "url": "http://leftover", "brightness": "{percent}"})
    assert cfg["udp_port"] == 5 and "url" not in cfg and "broker" not in cfg and "method" not in cfg
    with pytest.raises(UserError):  # shown and required
        check_fields(GenericBackend, {"transport": "http", "brightness": "{percent}"})
    cfg = check_fields(GenericBackend, {"brightness": "x"})  # transport by default: command
    assert cfg["transport"] == "command" and "port" not in cfg


# ------------------------------------------------------------ command

SCRIPT = "import sys, json\nwith open(sys.argv[1], 'a') as f:\n    f.write(json.dumps(sys.argv[2:]) + '\\n')\n"


def test_command_exact_args_no_shell(tmp_path):
    script, out, victim = tmp_path / "rec.py", tmp_path / "out.txt", tmp_path / "victim"
    script.write_text(SCRIPT)
    victim.write_text("keep")
    base = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(out))}"
    be = GenericBackend(FakeCtx())
    cfg = run(be.add({"transport": "command", "on": f"{base} on",
                      "brightness": f"{base} 'bri {{percent}}' L={{level}} '; rm {shlex.quote(str(victim))}'",
                      "colour": f"{base} rgb {{r}},{{g}},{{b}} {{r_dim}}", "off": f"{base} off {{on}}"}))
    b = bulb(cfg)

    async def main():
        await be.apply(b, 50)
        await be.apply(b, 60)
        await be.apply_color(b, {"mode": "hs", "hue": 120, "sat": 100})
        await be.apply(b, 0)
    run(main())
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert lines == [["on"], ["bri 50", "L=128", f"; rm {victim}"], ["bri 60", "L=153", f"; rm {victim}"],
                     ["rgb", "0,255,0", "0"], ["off", "0"]]  # r_dim: red 0 at 60 %
    assert victim.read_text() == "keep"  # "; rm" was an argument, never a shell command
    assert be.details(b)["transport"] == "command"


def test_command_failure_and_timeout(monkeypatch):
    py = shlex.quote(sys.executable)
    be = GenericBackend(FakeCtx())
    long_err = "x" * 500
    b = bulb({"transport": "command", "brightness": f"{py} -c \"import sys; sys.stderr.write('{long_err}'); sys.exit(3)\""})
    with pytest.raises(RuntimeError) as exc:
        run(be.apply(b, 10))
    assert "exit 3" in str(exc.value) and len(str(exc.value)) < 300
    monkeypatch.setattr(generic, "TIMEOUT_S", 0.3)
    b = bulb({"transport": "command", "brightness": f"{py} -c \"import time; time.sleep(5)\""})
    with pytest.raises(TimeoutError):
        run(be.apply(b, 10))


# ------------------------------------------------------------ http

def test_http_method_url_body_headers():
    from aiohttp import web
    seen = []

    async def handler(request):
        seen.append((request.method, request.path_qs, await request.text(), request.headers.get("X-Key"),
                     request.headers.get("Content-Type")))
        return web.Response(status=500 if "fail" in request.path_qs else 200)

    async def main():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        be = GenericBackend(FakeCtx())
        try:
            cfg = await be.add({"transport": "http", "method": "put", "url": f"http://127.0.0.1:{port}/light/{{on}}",
                                "headers": "X-Key: abc\\nContent-Type: application/json",
                                "brightness": '{{"bri": {level}}}'})
            assert cfg["method"] == "PUT"
            await be.apply(bulb(cfg), 100)
            get = await be.add({"transport": "http", "method": "GET", "url": f"http://127.0.0.1:{port}/cm?cmnd=",
                                "brightness": "Dimmer%20{percent}"})
            await be.apply(bulb(get), 40)
            fail = dict(get, url=f"http://127.0.0.1:{port}/fail?")
            with pytest.raises(RuntimeError, match="500"):
                await be.apply(bulb(fail), 40)
            assert be.details(bulb(cfg))["address"] == f"PUT http://127.0.0.1/light/{{on}}"
        finally:
            await be.stop()
            await runner.cleanup()
    run(main())
    assert seen[0] == ("PUT", "/light/1", '{"bri": 255}', "abc", "application/json")
    assert seen[1][:3] == ("GET", "/cm?cmnd=Dimmer%2040", "")


# ------------------------------------------------------------ udp

def test_udp_bytes():
    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv.bind(("127.0.0.1", 0))
    recv.settimeout(3)
    port = recv.getsockname()[1]
    be = GenericBackend(FakeCtx())
    try:
        cfg = run(be.add({"transport": "udp", "host": "127.0.0.1", "udp_port": port,
                          "brightness": "\\x7e\\x01B{level}\\x00", "off": "\\xffOFF\\n"}))
        run(be.apply(bulb(cfg), 100))
        assert recv.recvfrom(64)[0] == b"\x7e\x01B255\x00"
        run(be.apply(bulb(cfg), 0))
        assert recv.recvfrom(64)[0] == b"\xffOFF\n"
    finally:
        recv.close()


# ------------------------------------------------------------ serial

class FakePort:
    def __init__(self):
        self.tx, self.closed = [], False

    def write(self, data):
        self.tx.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


def test_serial_lines_port_stays_open(monkeypatch, tmp_path):
    opened = []

    def fake_open(port, baud):
        opened.append((port, baud, FakePort()))
        return opened[-1][2]

    monkeypatch.setattr(sbackend, "_open_port", fake_open)
    be = GenericBackend(FakeCtx())

    async def main():
        assert await be.discover(1) == [] and not opened  # discover opens nothing
        cfg = await be.add({"transport": "serial", "port": "loop://", "baud": 9600,
                            "brightness": "B{level}\\n", "colour": "C\\x02{r}{g}{b}\\r\\n"})
        assert not opened  # add opens nothing either
        b = bulb(cfg)
        await be.apply(b, 100)
        await be.apply_color(b, {"mode": "hs", "hue": 0, "sat": 100})
        await be.apply(b, 0)
        assert be.status(b) == {"connected": True, "online": True}
        assert len(opened) == 1 and opened[0][:2] == ("loop://", 9600)
        assert opened[0][2].tx == [b"B255\n", b"C\x022550" + b"0\r\n", b"B0\n"]
        gone = bulb({"transport": "serial", "port": str(tmp_path / "ttyGone"), "brightness": "B{level}"}, "generic:2")
        with pytest.raises(Offline):
            await be.apply(gone, 50)
        assert be.status(gone)["online"] is False
        with pytest.raises(UserError):
            await be.add({"transport": "serial", "port": str(tmp_path / "ttyGone"), "brightness": "B"})
        await be.stop()
        assert opened[0][2].closed
    run(main())


# ------------------------------------------------------------ mqtt

def test_mqtt_publish_one_connection():
    aiomqtt = pytest.importorskip("aiomqtt")
    pytest.importorskip("amqtt")
    from amqtt.broker import Broker

    async def main():
        port = free_port()
        broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                         "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
        await broker.start()
        got, ready = [], asyncio.Event()

        async def listen():
            async with aiomqtt.Client("127.0.0.1", port) as c:
                await c.subscribe("lights/#")
                ready.set()
                async for msg in c.messages:
                    got.append((str(msg.topic), msg.payload.decode()))

        task = asyncio.create_task(listen())
        await asyncio.wait_for(ready.wait(), 5)
        be = GenericBackend(FakeCtx())
        try:
            base = {"transport": "mqtt", "broker": "127.0.0.1", "broker_port": port}
            a = bulb(await be.add({**base, "topic": "lights/a/{on}", "brightness": '{{"b":{percent}}}'}), "generic:a")
            c = bulb(await be.add({**base, "topic": "lights/c", "on": "ON", "off": "OFF"}), "generic:c")
            await be.apply(a, 30)
            await be.apply(c, 30)
            await be.apply(c, 0)
            for _ in range(60):
                if len(got) >= 3:
                    break
                await asyncio.sleep(0.05)
            assert got == [("lights/a/1", '{"b":30}'), ("lights/c", "ON"), ("lights/c", "OFF")]
            assert len(be.brokers) == 1 and be.status(a)["connected"]
            with pytest.raises(UserError, match="not reachable"):
                await be.add({**base, "broker_port": free_port(), "topic": "t", "brightness": "{percent}"})
        finally:
            await be.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await broker.shutdown()
    run(main())
