"""Colour on mesh bulbs: the composition read once per device, the AppKey bound
to the colour models on their own elements, caps and range from what is bound,
and the Light CTL Temperature / HSL Hue / Saturation Sets on both bearers (a
fake bulb answering as real MeshNodes, one per element)."""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("btmesh")

import test_exclusive as ex  # noqa: E402  (its fakes; a module import collects none of its tests)
from btmesh import network as btnet  # noqa: E402
from btmesh.access import encode_opcode  # noqa: E402
from btmesh.node import MeshNode  # noqa: E402

from haus.backends import mesh  # noqa: E402
from haus.backends.mesh import MeshBackend  # noqa: E402
from haus.profiles import migrate  # noqa: E402

OLD_MODELS = [0x1000, 0x1300, 0x1002]  # what the user's bulb got bound at adoption
CT_COMP = [[0x0000, 0x0002, 0x1000, 0x1002, 0x1300, 0x1301, 0x1303, 0x1304, 0x1006, 0x1007],
           [0x1002, 0x1306]]
HSL_COMP = [[0x0000, 0x0002, 0x1000, 0x1002, 0x1300, 0x1307, 0x1308],
            [0x1002, 0x130A], [0x1002, 0x130B]]
BASE_CAPS = {"onoff", "dim", "flick", "powerup"}


def composition_status(elements: list[list[int]]) -> bytes:
    """Config Composition Data Status, page 0 (Ledvance's CID)."""
    out = bytes([0]) + b"".join(v.to_bytes(2, "little") for v in (mesh.LEDVANCE_CID, 0x0001, 0x0102, 10, 0x0003))
    for models in elements:
        out += (0).to_bytes(2, "little") + bytes([len(models), 0]) + b"".join(m.to_bytes(2, "little") for m in models)
    return encode_opcode(mesh.OP_CONFIG_COMPOSITION_DATA_STATUS) + out


class ColourBulb(ex.MeshBulb):
    """ex.MeshBulb plus a composition, binds, the CTL temperature range and
    the colour Sets; replies come from the element addressed (its own node).
    `back` set: the GATT proxy (replies to that callable), else adverts."""

    def __init__(self, elements=CT_COMP, ct_range=(2200, 6500)) -> None:
        super().__init__()
        self.node.add_device(0x0002, ex.DEVKEY)  # device-key messages to us
        self.elements = elements
        self.range = ct_range
        self.comp_answers = True
        self.comp_gets = 0
        self.binds: list[tuple[int, int, int]] = []  # (dst, element, model)
        self.sets: list[tuple[int, int, bytes]] = []  # (dst, opcode, params) of the colour messages
        self.dst = 0x0002
        self.back = None
        self.element_nodes: dict[int, MeshNode] = {0x0002: self.node}

    def hear(self, pdu: bytes, ctrl) -> None:
        self.dst = btnet.decode(self.node.ctx, pdu).dst
        super().hear(pdu, ctrl)

    def element_node(self, addr: int) -> MeshNode:
        if addr not in self.element_nodes:
            self.element_nodes[addr] = MeshNode(netkey=ex.NETKEY, appkey=ex.APPKEY, iv_index=0, src_addr=addr,
                                                send_network_pdu=self._out, seq=1000 * addr)
        return self.element_nodes[addr]

    def _message(self, msg) -> None:
        params = msg.params
        if msg.opcode == 0x8008:  # Config Composition Data Get
            self.comp_gets += 1
            if self.comp_answers:
                self.node.send_access(msg.src, composition_status(self.elements), dev_key=True)
            return
        if msg.opcode == 0x803D:  # Config Model App Bind
            element, model = int.from_bytes(params[0:2], "little"), int.from_bytes(params[4:6], "little")
            self.binds.append((self.dst, element, model))
            self.node.send_access(msg.src, encode_opcode(mesh.OP_CONFIG_MODEL_APP_STATUS) + b"\x00" + params[:6],
                                  dev_key=True)
            return
        if msg.opcode == mesh.OP_LIGHT_CTL_TEMPERATURE_RANGE_GET:
            self.sets.append((self.dst, msg.opcode, params))
            reply = (encode_opcode(mesh.OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS) + b"\x00"
                     + self.range[0].to_bytes(2, "little") + self.range[1].to_bytes(2, "little"))
        elif msg.opcode == mesh.OP_LIGHT_CTL_TEMPERATURE_SET:
            self.sets.append((self.dst, msg.opcode, params))
            reply = encode_opcode(mesh.OP_LIGHT_CTL_TEMPERATURE_STATUS) + params[0:4]
        elif msg.opcode in (mesh.OP_LIGHT_HSL_HUE_SET, mesh.OP_LIGHT_HSL_SATURATION_SET):
            self.sets.append((self.dst, msg.opcode, params))
            status = {mesh.OP_LIGHT_HSL_HUE_SET: mesh.OP_LIGHT_HSL_HUE_STATUS,
                      mesh.OP_LIGHT_HSL_SATURATION_SET: mesh.OP_LIGHT_HSL_SATURATION_STATUS}[msg.opcode]
            reply = encode_opcode(status) + params[0:2]
        else:
            super()._message(msg)
            return
        self.element_node(self.dst).send_access(msg.src, reply)

    def _out(self, pdu: bytes) -> None:
        if self.back is None:
            super()._out(pdu)
        else:
            asyncio.get_running_loop().call_soon(self.back, pdu)

    def colour_sets(self) -> list[tuple[int, int, bytes]]:
        return [s for s in self.sets if s[1] != mesh.OP_LIGHT_CTL_TEMPERATURE_RANGE_GET]


def gatt_backend(bulb: ColourBulb, device: dict | None = None) -> tuple[MeshBackend, ex.Ctx]:
    """A backend on a GATT proxy link to `bulb`, its boot count already read
    on it (no re-arm pending), the device as adopted before colour."""
    ctx = ex.Ctx(flick="off")
    ctx.state["devices"]["0x0002"].update(device if device is not None else {"models": list(OLD_MODELS)})
    b = MeshBackend(ctx)
    b.link = b._make_link()
    b.online = True
    new_link(b, bulb)
    return b, ctx


def new_link(b: MeshBackend, bulb: ColourBulb) -> None:
    b.link._make_node(lambda pdu: bulb.hear(pdu, None))
    bulb.back = b.link.node.handle_network_pdu
    b.link.client = SimpleNamespace(is_connected=True)
    b.link.address = ex.MAC
    b.link.connected_at = time.monotonic()
    b.boot_link = b.link.connected_at  # the boot count was read on it: _link_up returns at once


async def setup_now(b: MeshBackend) -> None:
    async with b.lock:
        await b.colour_setup(0x0002)


def ct_bound() -> dict:
    return {"models": OLD_MODELS + [0x1303, 0x1306], "composition": CT_COMP, "ct_range": [2200, 6500]}


def test_the_composition_is_read_once_stored_and_the_models_bound_per_element(caplog):
    async def body():
        bulb = ColourBulb()
        b, ctx = gatt_backend(bulb)
        assert b.caps(ctx.bulb) == BASE_CAPS and b.ct_range(ctx.bulb) == mesh.CT_DEFAULT_RANGE
        assert b._colour_due(0x0002, b.link.connected_at)
        with caplog.at_level(logging.INFO, logger="hauslicht"):
            await setup_now(b)
        dev = ctx.state["devices"]["0x0002"]
        assert dev["composition"] == CT_COMP and bulb.comp_gets == 1
        assert dev["cid"] == mesh.LEDVANCE_CID
        # sent to the primary address, each for its own element
        assert bulb.binds == [(0x0002, 0x0002, 0x1303), (0x0002, 0x0003, 0x1306)]
        assert dev["models"] == OLD_MODELS + [0x1303, 0x1306]
        assert bulb.sets == [(0x0002, mesh.OP_LIGHT_CTL_TEMPERATURE_RANGE_GET, b"")]  # the CTL Server's element
        assert dev["ct_range"] == [2200, 6500] and b.ct_range(ctx.bulb) == (2200, 6500)
        assert b.caps(ctx.bulb) == BASE_CAPS | {"ct"}
        assert b.details(ctx.bulb)["ct_range"] == "2200–6500 K"
        assert ctx.saved  # kept in the state file
        text = caplog.text
        assert "0x0002: models element 0: 0x0000 0x0002 0x1000 0x1002 0x1300 0x1301 0x1303 0x1304 " \
               "0x1006 0x1007, element 1: 0x1002 0x1306" in text
        assert text.count("AppKey bound to model") == 2 and "on element 1 (0x0003)" in text

        # stored: never read or bound again
        assert not b._colour_pending(0x0002)
        new_link(b, bulb)
        b._schedule_colour_setup(b.link.connected_at)
        assert b.colour_task is None
        await setup_now(b)  # even asked directly: nothing but nothing to do
        assert bulb.comp_gets == 1 and len(bulb.binds) == 2
    asyncio.run(body())


def test_an_unanswered_composition_is_asked_again_on_a_later_link_only(monkeypatch):
    monkeypatch.setattr(mesh, "COMPOSITION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mesh, "COLOUR_SETUP_DELAY_S", 0.0)

    async def body():
        bulb = ColourBulb()
        bulb.comp_answers = False
        b, ctx = gatt_backend(bulb)
        first = b.link.connected_at
        b._schedule_colour_setup(first)
        await b.colour_task
        assert bulb.comp_gets == 2  # the Get and its one retransmission
        assert "composition" not in ctx.state["devices"]["0x0002"]
        # the same link: no second try
        assert not b._colour_due(0x0002, first)
        b.colour_task = None
        b._schedule_colour_setup(first)
        assert b.colour_task is None
        # a later link, but too soon: not yet
        new_link(b, bulb)
        assert not b._colour_due(0x0002, b.link.connected_at)
        # a later link after the spacing: again, at most COLOUR_TRIES in all
        monkeypatch.setattr(mesh, "COLOUR_RETRY_S", 0.0)
        tries = 1
        while True:
            await asyncio.sleep(0.001)
            new_link(b, bulb)
            b._schedule_colour_setup(b.link.connected_at)
            if b.colour_task is None or b.colour_task.done():
                break
            await b.colour_task
            tries += 1
            b.colour_task = None
        assert tries == mesh.COLOUR_TRIES and bulb.comp_gets == 2 * mesh.COLOUR_TRIES
        # answered on a later run (a restart): stored and bound
        b.colour_tries.clear()
        bulb.comp_answers = True
        new_link(b, bulb)
        b._schedule_colour_setup(b.link.connected_at)
        await b.colour_task
        assert ctx.state["devices"]["0x0002"]["composition"] == CT_COMP and "ct" in b.caps(ctx.bulb)
    asyncio.run(body())


def test_caps_follow_the_bound_models():
    ctx = ex.Ctx()
    b = MeshBackend(ctx)
    dev = ctx.state["devices"]["0x0002"]
    dev.update({"models": list(OLD_MODELS), "composition": CT_COMP})
    assert b.caps(ctx.bulb) == BASE_CAPS  # listed, not bound
    dev["models"] = OLD_MODELS + [0x1303, 0x1306]
    assert b.caps(ctx.bulb) == BASE_CAPS | {"ct"}
    assert b.ct_range(ctx.bulb) == mesh.CT_DEFAULT_RANGE and "ct_range" not in b.details(ctx.bulb)
    dev["ct_range"] = [100, 70000]  # nonsense: the default
    assert b.ct_range(ctx.bulb) == mesh.CT_DEFAULT_RANGE
    dev.update({"composition": HSL_COMP, "models": OLD_MODELS + [0x1307, 0x130A, 0x130B]})
    assert b.caps(ctx.bulb) == BASE_CAPS | {"hs"}
    dev["models"] = OLD_MODELS + [0x1307, 0x130A]  # saturation unbound
    assert b.caps(ctx.bulb) == BASE_CAPS
    assert mesh.colour_binds(HSL_COMP, dev["models"]) == [(2, 0x130B)]
    assert mesh.colour_binds(CT_COMP[:1], OLD_MODELS) == []  # no Temperature Server: nothing for ct


def test_the_temperature_set_goes_to_the_temperature_element_and_once_per_value():
    async def body():
        bulb = ColourBulb()
        b, ctx = gatt_backend(bulb, ct_bound())
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 3000})
        (dst, opcode, params), = bulb.colour_sets()
        assert (dst, opcode) == (0x0003, 0x8264)
        # temperature 3000 K, delta UV 0, TID, the lightness' transition (200 ms), no delay
        assert params[:4] == (3000).to_bytes(2, "little") + b"\x00\x00"
        assert params[5:] == bytes([mesh.LIGHTNESS_TRANSITION, 0]) and len(params) == 7
        seq = b.link.node.ctx.seq
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 3000.2})  # the same: nothing sent
        assert len(bulb.colour_sets()) == 1 and b.link.node.ctx.seq == seq
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 1000})  # below the range: its end
        assert bulb.colour_sets()[-1][2][:2] == (2200).to_bytes(2, "little")
        assert not await b.apply_color(ctx.bulb, {"mode": "hs", "hue": 10, "sat": 50})  # no hs cap
        assert len(bulb.colour_sets()) == 2

        # a reboot: what it shows is not known any more, the same value goes out again
        b._report(0x0002, ctx.flick, ctx.bulb.id)
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 2200})
        assert len(bulb.colour_sets()) == 3
        # unanswered: False (the core retries), and not taken for confirmed
        bulb.answers = False
        b.link._timing = lambda: {"timeout": 0.05, "retries": 0}
        assert not await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 4000})
        assert b.colour_now[2] == {"ct": 2200}
    asyncio.run(body())


def test_a_colour_asked_before_the_onpowerup_set_waits_for_it():
    async def body():
        bulb = ColourBulb()
        b, ctx = gatt_backend(bulb, ct_bound())
        b.rearm[0x0002] = b.link.connected_at  # a link after a reboot
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 3000})
        assert bulb.colour_sets() == []
        assert await b.set_powerup(ctx.bulb, "on")
        assert [s[:2] for s in bulb.colour_sets()] == [(0x0003, 0x8264)]
        assert b.held_color == {}
    asyncio.run(body())


def test_hue_and_saturation_go_to_their_elements_only_when_changed():
    async def body():
        bulb = ColourBulb(HSL_COMP)
        b, ctx = gatt_backend(bulb)
        await setup_now(b)
        assert bulb.binds == [(2, 2, 0x1307), (2, 3, 0x130A), (2, 4, 0x130B)]
        assert b.caps(ctx.bulb) == BASE_CAPS | {"hs"} and bulb.sets == []  # no range Get without ct
        assert await b.apply_color(ctx.bulb, {"mode": "hs", "hue": 120, "sat": 50})
        assert [(d, op, p[:2]) for d, op, p in bulb.colour_sets()] == [
            (0x0003, 0x826F, (21845).to_bytes(2, "little")), (0x0004, 0x8273, (32768).to_bytes(2, "little"))]
        assert await b.apply_color(ctx.bulb, {"mode": "hs", "hue": 120, "sat": 100})
        assert [(d, op, p[:2]) for d, op, p in bulb.colour_sets()[2:]] == [
            (0x0004, 0x8273, (65535).to_bytes(2, "little"))]
        assert await b.apply_color(ctx.bulb, {"mode": "hs", "hue": 480, "sat": 100})  # 480 is 120
        assert len(bulb.colour_sets()) == 3
    asyncio.run(body())


def test_the_advertising_bearer_reads_binds_and_sets_the_same_way(monkeypatch):
    async def body():
        bulb = ColourBulb()
        ctx, bulb, b, ctrl = await ex.armed_session(monkeypatch, bulb)
        assert isinstance(b.link, mesh.AdvLink) and bulb.back is None
        ctx.state["devices"]["0x0002"]["models"] = list(OLD_MODELS)
        await ex.adverts_every(ctrl, 0.05, 0.2)  # heard: the session counts as up
        await setup_now(b)  # the composition comes segmented, as adverts
        dev = ctx.state["devices"]["0x0002"]
        assert dev["composition"] == CT_COMP and "ct" in b.caps(ctx.bulb)
        assert bulb.binds == [(0x0002, 0x0002, 0x1303), (0x0002, 0x0003, 0x1306)]
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 4000})
        (dst, opcode, params), = bulb.colour_sets()
        assert (dst, opcode, params[:4]) == (0x0003, 0x8264, (4000).to_bytes(2, "little") + b"\x00\x00")
        assert await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 4000})
        assert len(bulb.colour_sets()) == 1
        await ex.finish(b)
    asyncio.run(body())


def test_an_old_state_file_without_a_composition_works_as_before():
    async def body():
        bulb = ColourBulb()
        b, ctx = gatt_backend(bulb)
        migrate(ctx.state)
        dev = ctx.state["devices"]["0x0002"]
        assert dev["models"] == OLD_MODELS and "composition" not in dev
        assert b.caps(ctx.bulb) == BASE_CAPS and b.ct_range(ctx.bulb) == mesh.CT_DEFAULT_RANGE
        assert "ct_range" not in b.details(ctx.bulb)
        assert not await b.apply_color(ctx.bulb, {"mode": "ct", "kelvin": 3000})  # nothing sent
        assert bulb.sets == [] and bulb.comp_gets == 0
        assert b._colour_pending(0x0002)  # read on the next link
        await b.apply(ctx.bulb, 40.0)  # the level as ever
        assert bulb.level == 40.0
    asyncio.run(body())
