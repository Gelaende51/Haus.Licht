"""A Bluetooth link drop is not a wall-switch flick: the mesh backend reads the
Volkslicht's vendor boot count (Telink VD_GROUP_G_GET) on every new link and
counts a return as a power cycle only when that count restarted."""

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("btmesh")

from haus.backends import mesh  # noqa: E402
from haus.backends.mesh import REBOOTED, SAME, UNKNOWN, MeshBackend, boot_verdict  # noqa: E402

MAC = "F0:D1:B8:53:33:2F"
BID = "mesh:0002"

# Recorded on the bulb (2026-10-16, light off, three separate connections, then
# 248 Gets in one): the count goes on across reconnects and wraps ff -> 00 -> 01.
RECORDED = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09]
RECORDED_WRAP = [0xFD, 0xFE, 0xFF, 0x00, 0x01, 0x02]


class FakeBulb:
    """The bulb's vendor model: counts Gets since boot in one byte."""

    def __init__(self, count: int = 0, answers: bool = True) -> None:
        self.count = count
        self.answers = answers

    def get(self) -> int | None:
        if not self.answers:
            return None
        self.count = (self.count + 1) & 0xFF
        return self.count

    def reboot(self) -> None:
        self.count = 0


class FakeLink:
    def __init__(self, bulb: FakeBulb) -> None:
        self.bulb = bulb
        self.client = None
        self.connected_at = 0.0
        self.address = None
        self.opens = 0

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def ensure(self) -> None:
        if not self.connected:
            self.client = object()
            self.opens += 1
            self.connected_at = time.monotonic()
            self.address = MAC

    async def close(self) -> None:
        self.client = None

    async def boot_count(self, addr: int) -> int | None:
        assert addr == 0x0002
        return self.bulb.get()


class FakeCtx:
    def __init__(self) -> None:
        self.state = {"devices": {"0x0002": {"address": MAC}}}
        self.bulb = SimpleNamespace(id=BID, config={"unicast": "0x0002"})
        self.events: list[tuple] = []

    def bulbs(self):
        return [self.bulb]

    def flick(self, bid):
        self.events.append(("flick", bid))

    def power_returned(self, bid, gap):
        self.events.append(("power", bid, gap))

    def changed(self):
        pass


def backend(device: FakeBulb) -> MeshBackend:
    b = MeshBackend(FakeCtx())
    b.link = FakeLink(device)
    return b


# ------------------------------------------------------------ the verdict


@pytest.mark.parametrize("last, value, want", [
    (None, 5, UNKNOWN),      # no reference yet (first contact)
    (10, None, UNKNOWN),     # no answer
    (10, 11, SAME),          # went on by one
    (10, 13, SAME),          # a few replies lost
    (251, 255, SAME),
    (10, 1, REBOOTED),       # restarted
    (10, 3, REBOOTED),       # restarted, a reply or two lost
    (10, 30, UNKNOWN),       # someone else asked many times
    (10, 10, UNKNOWN),       # no Get counted
    (0, 1, UNKNOWN),         # after the wrap: on by one and fresh alike
    (254, 2, UNKNOWN),
])
def test_boot_verdict(last, value, want):
    assert boot_verdict(last, value) == want


def test_recorded_counts_are_one_boot():
    for last, value in zip(RECORDED[mesh.BOOT_SLACK:], RECORDED[mesh.BOOT_SLACK + 1:]):
        assert boot_verdict(last, value) == SAME
    # through the wrap the reference has to be moved on before it is safe
    assert boot_verdict(RECORDED_WRAP[3], RECORDED_WRAP[4]) == UNKNOWN


# ------------------------------------------------------------ reading on each link


def test_reference_is_moved_clear_of_a_fresh_boot():
    async def run():
        b = backend(FakeBulb(count=0))  # just booted
        await b._link_up()
        assert b.boot_counts[2] == mesh.BOOT_SLACK + 1  # 1 read, then re-armed
        assert b.boot_verdicts[2][1] == UNKNOWN  # nothing to compare with yet
        await b.link.close()
        await b._link_up()
        assert b.boot_verdicts[2][1] == SAME
    asyncio.run(run())


def test_reference_is_moved_through_the_wrap():
    async def run():
        device = FakeBulb(count=0xFB)
        b = backend(device)
        await b._link_up()  # reads 0xfc: too close to the wrap
        assert b.boot_counts[2] == mesh.BOOT_SLACK + 1
        await b.link.close()
        await b._link_up()
        assert b.boot_verdicts[2][1] == SAME
    asyncio.run(run())


def test_reboot_is_seen():
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        await b._link_up()
        await b.link.close()
        device.reboot()
        await b._link_up()
        assert b.boot_verdicts[2][1] == REBOOTED
    asyncio.run(run())


def test_silent_bulb_is_asked_only_a_few_times():
    async def run():
        device = FakeBulb(answers=False)
        b = backend(device)
        for _ in range(4):
            await b.link.close()
            await b._link_up()
        assert b.boot_verdicts[2][1] == UNKNOWN
        assert b.boot_misses[2] == mesh.BOOT_MISSES
    asyncio.run(run())


# ------------------------------------------------------------ drop and return


async def drop_and_return(b: MeshBackend, between) -> list[tuple]:
    b.online = True
    await b._link_up()  # the link before the drop: reference count
    await asyncio.sleep(0.01)
    b.link.client = SimpleNamespace(is_connected=False)  # the link died
    b.bulb_lost()
    between()
    await asyncio.sleep(0.05)
    b.bulb_seen.set()  # the bulb adverts again
    await asyncio.wait_for(b.presence_task, 2)
    assert b.online is True
    return b.ctx.events


def test_link_drop_is_no_flick():
    b = backend(FakeBulb(count=40))
    assert asyncio.run(drop_and_return(b, lambda: None)) == []


def test_reboot_after_drop_is_a_power_return():
    device = FakeBulb(count=40)
    events = asyncio.run(drop_and_return(backend(device), device.reboot))
    assert [e[:2] for e in events] == [("power", BID)]


def test_unknown_keeps_todays_behaviour(caplog):
    device = FakeBulb(count=40)
    caplog.set_level("INFO", logger="hauslicht")

    def silence():
        device.answers = False
    events = asyncio.run(drop_and_return(backend(device), silence))
    assert [e[:2] for e in events] == [("power", BID)]
    assert "boot count unknown" in caplog.text


def test_first_contact_is_unchanged():
    async def run():
        b = backend(FakeBulb(count=40))
        b.online = None
        b.bulb_lost(long_ago=True)
        await asyncio.sleep(0.01)
        b.bulb_seen.set()
        await asyncio.wait_for(b.presence_task, 2)
        return b.ctx.events
    assert asyncio.run(run()) == [("power", BID, None)]


# ------------------------------------------------------------ off-beat advert


def offbeat(device: FakeBulb, reboot: bool) -> list[tuple]:
    async def run():
        b = backend(device)
        b.online = True
        await b._link_up()
        await asyncio.sleep(0.01)
        at = time.monotonic()
        b.force_reconnect = True
        if reboot:
            device.reboot()
        await b.verify_flick(b.ctx.bulb, at)
        assert b.link.opens == 2  # the check reconnected
        return b.ctx.events
    return asyncio.run(run())


def test_offbeat_advert_with_same_boot_is_no_flick():
    assert offbeat(FakeBulb(count=40), reboot=False) == []


def test_offbeat_advert_after_reboot_is_a_flick():
    assert offbeat(FakeBulb(count=40), reboot=True) == [("flick", BID)]
