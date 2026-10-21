"""A Bluetooth link drop is not a wall-switch flick: the mesh backend reads the
Volkslicht's vendor boot count (Telink VD_GROUP_G_GET) on every new link and
counts a return as a power cycle only when that count restarted."""

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("btmesh")

from haus.backends import Offline, mesh  # noqa: E402
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


class HangingNode:
    """A mesh node on a dead link: requests never get an answer."""

    async def request(self, *args, **kw):
        await asyncio.get_running_loop().create_future()


class FakeLink:
    abort = mesh.MeshLink.abort
    _node_request = mesh.MeshLink._node_request

    def __init__(self, bulb: FakeBulb) -> None:
        self.bulb = bulb
        self.client = None
        self.connected_at = 0.0
        self.address = None
        self.opens = 0
        self.inflight: set = set()
        self.sets: set = set()  # user Sets among them (MeshLink.abort may keep them)
        self.node = HangingNode()
        self.hang = False  # requests go unanswered until aborted (the link died)
        self.powerup = 1
        self.log: list[tuple] = []
        self.clock = time.monotonic
        self.dead = False  # no power: nothing adverts, connecting finds nothing
        self.on_connect = None  # called before a link comes up (a slow connect moves the clock)

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def ensure(self) -> None:
        if not self.connected:
            if self.dead:
                raise ConnectionError("no bulb of our mesh advertising: bulbs switched off at the wall")
            if self.on_connect:
                self.on_connect()
            self.client = object()
            self.opens += 1
            self.connected_at = self.clock()
            self.address = MAC

    async def close(self) -> None:
        self.client = None

    async def _maybe_hang(self) -> None:
        if self.hang:
            await self._node_request()

    async def boot_count(self, addr: int) -> int | None:
        assert addr == 0x0002
        await self._maybe_hang()
        value = self.bulb.get()
        self.log.append(("boot", value))
        return value

    async def set_level(self, addr: int, percent: float, scale: int = 100) -> float | None:
        await self._maybe_hang()
        self.log.append(("level", percent))
        return percent

    async def get_onpowerup(self, addr: int) -> int | None:
        await self._maybe_hang()
        self.log.append(("powerup_get", self.powerup))
        return self.powerup

    async def set_onpowerup(self, addr: int, value: int) -> int | None:
        await self._maybe_hang()
        self.powerup = value
        self.log.append(("powerup_set", value))
        return value


class FakeScanner:
    def __init__(self) -> None:
        self.starts = self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


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
    (10, 30, SAME),          # someone else asked many times: ahead, never a restart
    (10, 10, SAME),          # no Get counted, or a late copy of the last reply (13:39:56: "5 -> 5")
    (250, 20, SAME),         # ahead through the wrap
    (100, 50, UNKNOWN),      # behind, but no fresh count either
    (1, 1, REBOOTED),        # a reference still fresh, and fresh again
    (0, 1, UNKNOWN),         # after the wrap: on by one and fresh alike
    (254, 2, UNKNOWN),
])
def test_boot_verdict(last, value, want):
    assert boot_verdict(last, value) == want


@pytest.mark.parametrize("last, value, extra, want", [
    (6, 10, 0, SAME),        # 03:11:40: two unanswered links, still within the slack
    (6, 14, 0, SAME),        # more unanswered Gets than the slack: still ahead
    (6, 14, 4, SAME),        # ...counted: the same boot
    (6, 3, 4, REBOOTED),
    (5, 6, 2, UNKNOWN),      # fits both: cannot be told
])
def test_boot_verdict_counts_unanswered_gets(last, value, extra, want):
    assert boot_verdict(last, value, extra) == want


def test_unanswered_reads_widen_the_next_window():
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        await b._link_up()
        for _ in range(3):  # the real bulb counts Gets it cannot answer
            await b.link.close()
            device.answers = False
            await b._link_up()
            device.count += mesh.BOOT_GETS_PER_READ
        await b.link.close()
        device.answers = True
        await b._link_up()  # 41 -> 48: 7 on, beyond BOOT_SLACK alone
        return b.boot_verdicts[2][1]
    assert asyncio.run(run()) == SAME


def test_recorded_counts_are_one_boot():
    for last, value in zip(RECORDED[mesh.BOOT_SLACK:], RECORDED[mesh.BOOT_SLACK + 1:]):
        assert boot_verdict(last, value) == SAME
    # through the wrap the reference has to be moved on before it is safe
    assert boot_verdict(RECORDED_WRAP[3], RECORDED_WRAP[4]) == UNKNOWN


# ------------------------------------------------------------ reading on each link


def test_reference_is_moved_clear_of_a_fresh_boot():
    async def run():
        b = backend(FakeBulb(count=0))  # just booted
        b.online = True
        await b._link_up()
        assert b.boot_counts[2] == 1  # the verdict's one read; clearing waits for the arm
        assert b.boot_verdicts[2][1] == UNKNOWN  # nothing to compare with yet
        assert await b.set_powerup(b.ctx.bulb, "on")
        assert b.boot_counts[2] == mesh.BOOT_SLACK + 1  # moved clear after the arm
        await b.link.close()
        await b._link_up()
        assert b.boot_verdicts[2][1] == SAME
    asyncio.run(run())


def test_reference_is_moved_through_the_wrap_without_an_arm(monkeypatch):
    monkeypatch.setattr(mesh, "ARM_WAIT_S", 0.01)

    async def run():
        device = FakeBulb(count=0xFB)
        b = backend(device)
        await b._link_up()  # reads 0xfc: too close to the wrap
        assert b.boot_counts[2] == 0xFC
        await asyncio.sleep(0.05)  # no arm came: the wait ran out, clearing ran anyway
        assert b.boot_counts[2] == mesh.BOOT_SLACK + 1
        await b.link.close()
        await b._link_up()
        assert b.boot_verdicts[2][1] == SAME
    asyncio.run(run())


def test_verdict_from_the_first_get_clearing_after_the_arm():
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        b.online = True
        await b._link_up()
        assert await b.set_powerup(b.ctx.bulb, "on")
        await b.link.close()
        device.reboot()
        b.link.log.clear()
        await b._link_up()
        assert b.link.log == [("boot", 1)]  # one Get decides
        assert b.boot_verdicts[2][1] == REBOOTED
        assert await b.set_powerup(b.ctx.bulb, "off")
        # the Set goes out without a Get first (a Set follows anyway), then the clearing Gets
        assert b.link.log == [("boot", 1), ("powerup_set", 0)] + [("boot", n) for n in range(2, 6)]
        assert b.boot_counts[2] == mesh.BOOT_SLACK + 1
    asyncio.run(run())


# ------------------------------------------------------------ re-arm first


def test_after_a_reboot_onpowerup_goes_before_the_level():
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        b.online = True
        await b._link_up()
        assert await b.set_powerup(b.ctx.bulb, "on")
        await b.apply(b.ctx.bulb, 20)  # an ordinary link: sent at once
        assert b.link.log[-1] == ("level", 20)
        await b.link.close()
        device.reboot()
        b.link.log.clear()
        await b.apply(b.ctx.bulb, 40)  # the core sends the level first...
        assert ("level", 40) not in b.link.log  # ...it waits for the Set
        assert await b.set_powerup(b.ctx.bulb, "off")
        sent = [op for op in b.link.log if op[0] in ("level", "powerup_set")]
        assert sent == [("powerup_set", 0), ("level", 40)]
    asyncio.run(run())


def test_level_asked_before_the_flick_is_not_sent():
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        b.online = True
        await b._link_up()
        assert await b.set_powerup(b.ctx.bulb, "off")
        b.suspects[2] = time.monotonic()  # an off-beat advert, verdict pending
        b.force_reconnect = True
        device.reboot()
        b.link.log.clear()
        await b.apply(b.ctx.bulb, 40)  # reconnects, judges: a flick, so 40 % is stale
        assert b.ctx.events == [("flick", BID)]
        assert not [op for op in b.link.log if op[0] == "level"]
        assert await b.set_powerup(b.ctx.bulb, "on")  # the core's new target, at once
        assert b.link.log[1] == ("powerup_set", 1)  # right after the verdict's Get
    asyncio.run(run())


def test_link_timeout_update_waits_for_the_arm(monkeypatch):
    calls = []

    async def fake_shorten(address, delay=1.0):
        calls.append(address)
    monkeypatch.setattr(mesh, "CONN_UPDATE_HELPER", __file__)
    monkeypatch.setattr(mesh, "shorten_link_timeout", fake_shorten)

    async def run():
        b = backend(FakeBulb(count=40))
        b.online = True
        await b._link_up()
        task = asyncio.get_running_loop().create_task(b.after_connect(MAC))
        await asyncio.sleep(0.2)
        assert calls == []
        assert await b.set_powerup(b.ctx.bulb, "on")
        await asyncio.wait_for(task, 1)
        assert calls == [MAC]
    asyncio.run(run())


def test_offbeat_advert_cancels_the_request_in_flight():
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        b.online = True
        await b._link_up()
        assert await b.set_powerup(b.ctx.bulb, "on")
        b.link.hang = True  # the bulb rebooted: the old link answers nothing
        device.reboot()
        apply = asyncio.get_running_loop().create_task(b.apply(b.ctx.bulb, 30))
        await asyncio.sleep(0.05)
        assert b.lock.locked()  # held by the request on the dead link
        b.link.hang = False  # the new link will answer
        t0 = time.monotonic()
        b.offbeat(b.ctx.bulb, t0)
        with pytest.raises(Offline):  # no ConnectionError: no back-off in the core
            await asyncio.wait_for(apply, 0.5)
        while not b.ctx.events and time.monotonic() - t0 < 1.0:
            await asyncio.sleep(0.01)
        assert b.ctx.events == [("flick", BID)]
        assert time.monotonic() - t0 < 0.5  # far below a request timeout
        assert b.link.opens == 2
        assert b.rearm.get(2) == b.link.connected_at  # the next level waits for the Set
    asyncio.run(run())


# ------------------------------------------------------------ idle


def test_idle_drops_the_link_and_resumes():
    async def run():
        b = backend(FakeBulb(count=40))
        b.online = True
        b.scanner = FakeScanner()
        await b._link_up()
        assert await b.set_powerup(b.ctx.bulb, "on")
        await b.idle(True)
        assert not b.link.connected
        assert b.scanner.stops == 1
        assert b.status(b.ctx.bulb)["connected"] is False
        with pytest.raises(Offline):  # no reconnecting while idle
            await b.apply(b.ctx.bulb, 30)
        assert (await b.judge_boot([b.ctx.bulb], 0.0)) == {BID: UNKNOWN}
        assert b.link.opens == 1
        await b.idle(True)  # repeated: nothing more
        assert b.scanner.stops == 1
        await b.idle(False)
        assert b.scanner.starts == 1
        await b.apply(b.ctx.bulb, 30)
        assert b.link.opens == 2 and b.link.connected
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
    """A bulb that never answered the vendor Get (no counter): the old rules."""
    device = FakeBulb(count=40, answers=False)
    caplog.set_level("INFO", logger="hauslicht")
    events = asyncio.run(drop_and_return(backend(device), lambda: None))
    assert [e[:2] for e in events] == [("power", BID)]
    assert "boot count unknown" in caplog.text


def unread_return(device: FakeBulb, between) -> list[tuple]:
    """Drop and return of a bulb whose counter answered before, but whose
    count goes unread on the return's links (connects failing, notifications
    refused): nothing is decided until a link reads it."""
    async def run():
        b = backend(device)
        b.online = True
        await b._link_up()
        b.link.client = SimpleNamespace(is_connected=False)
        b.bulb_lost()
        await asyncio.sleep(0.05)
        device.answers = False
        b.bulb_seen.set()
        for _ in range(3):  # adverts, each a link whose count goes unanswered
            await asyncio.sleep(0.05)
            assert b.ctx.events == [] and b.online is False and not b.presence_task.done()
            b.bulb_seen.set()
        between()
        device.answers = True
        await asyncio.sleep(0.05)
        b.bulb_seen.set()
        await asyncio.wait_for(b.presence_task, 2)
        assert b.online is True
        return b.ctx.events
    return asyncio.run(run())


def test_unread_return_waits_and_a_link_drop_is_no_flick():
    """2026-10-20 02:31-06:07: connects failed for minutes, each return was
    "boot count unknown: counted as power back" (30 toggles); the count went
    on 5 -> 38 through all of them."""
    assert unread_return(FakeBulb(count=40), lambda: None) == []


def test_unread_return_then_a_fresh_count_is_a_power_return():
    device = FakeBulb(count=40)
    assert [e[:2] for e in unread_return(device, device.reboot)] == [("power", BID)]


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


def test_advert_right_after_a_connect_is_no_grid_reference(clock):
    """06:08: the bulb adverts once 0-2 s after a connect, then on its own
    rhythm; the gap from that advert looked off-beat (12.2-13.8 s)."""
    async def run():
        b = clocked(FakeBulb(count=40), clock)
        b.online = True
        seen = []
        b.offbeat = lambda bulb, now: seen.append(round(now - t0, 1))
        clock.advance(100)
        await b._link_up()
        t0 = clock()
        for at in (1.5, 13.7, 23.8, 33.9, 37.0):  # the last one is off the beat
            clock.t = t0 + at
            advert(b)
        return seen
    assert asyncio.run(run()) == [37.0]


def test_offbeat_advert_with_same_boot_is_no_flick():
    assert offbeat(FakeBulb(count=40), reboot=False) == []


def test_offbeat_advert_after_reboot_is_a_flick():
    assert offbeat(FakeBulb(count=40), reboot=True) == [("flick", BID)]


# ------------------------------------------------------------ flick or power-off (fake clock)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mesh, "time", SimpleNamespace(monotonic=c))
    return c


def clocked(device: FakeBulb, clock: Clock) -> MeshBackend:
    b = backend(device)
    b.link.clock = clock
    return b


def advert(b: MeshBackend) -> None:
    b.on_advert(SimpleNamespace(address=MAC), None)


async def offbeat_then_dead(b: MeshBackend, clock: Clock) -> float:
    """Connected and settled, then an off-beat advert after which the bulb is
    gone: no link to judge it on (the 19:48 / 20:52 / 22:19 pattern)."""
    b.online = True
    await b._link_up()  # the reference count
    clock.advance(60)
    at = clock()
    b.link.dead = True
    b.offbeat(b.ctx.bulb, at)
    await asyncio.wait_for(b.tasks[-1], 2)  # verify_flick: the boot check finds no bulb
    return at


def test_offbeat_advert_without_link_is_no_flick_and_comes_back_lit(clock):
    """Wall switch off for 4 h: the dying bulb adverts off the beat. Before:
    "counted as a flick" -> off, and its return "flick already handled": dark."""
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        at = await offbeat_then_dead(b, clock)
        assert b.ctx.events == []  # not decided without a link
        assert b.online is False and b.lost_at == at  # the drop is the off-beat advert
        clock.advance(2)
        advert(b)  # the dying bulb once more, within HOLDUP_S: not a return
        await asyncio.sleep(0.05)
        assert not b.presence_task.done() and b.ctx.events == []
        clock.advance(4 * 3600)  # the switch goes on again
        device.reboot()
        b.link.dead = False
        advert(b)
        await asyncio.wait_for(b.presence_task, 2)
        assert b.online is True
        return b.ctx.events
    assert asyncio.run(run()) == [("power", BID, pytest.approx(2 + 4 * 3600))]


def test_offbeat_advert_without_link_back_within_the_window_is_a_flick(clock):
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        await offbeat_then_dead(b, clock)
        clock.advance(8)  # off for a few seconds: a flick
        device.reboot()
        b.link.dead = False
        advert(b)
        await asyncio.wait_for(b.presence_task, 2)
        return b.ctx.events
    # power_returned with a gap inside the window: the core toggles the power before the loss
    events = asyncio.run(run())
    assert events == [("power", BID, pytest.approx(8))] and events[0][2] <= mesh.QUICK_GAP_S


def test_offbeat_advert_without_link_and_no_boot_count_waits_for_a_read(clock):
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        await offbeat_then_dead(b, clock)
        clock.advance(45)
        device.reboot()
        b.link.dead = False
        device.answers = False  # no boot count on the new link (02:23:33)
        advert(b)
        await asyncio.sleep(0.05)
        assert b.ctx.events == [] and not b.presence_task.done()  # not decided unread
        clock.advance(5)
        device.answers = True  # the next link reads 1: the reboot, gap from the advert back
        advert(b)
        await asyncio.wait_for(b.presence_task, 2)
        return b.ctx.events
    assert asyncio.run(run()) == [("power", BID, pytest.approx(45))]


def offbeat_slow_link(clock: Clock, delay: float) -> list[tuple]:
    """An off-beat advert whose new link only comes up `delay` s later (the
    dying bulb adverted; the pending connect caught it when power came back)."""
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        b.online = True
        await b._link_up()
        clock.advance(60)
        at = clock()
        device.reboot()
        b.link.on_connect = lambda: clock.advance(delay)
        b.offbeat(b.ctx.bulb, at)
        await asyncio.wait_for(b.tasks[-1], 2)
        assert b.online is True
        return b.ctx.events
    return asyncio.run(run())


def test_offbeat_advert_link_within_the_window_is_a_flick(clock):
    assert offbeat_slow_link(clock, 15.0) == [("flick", BID)]


def test_offbeat_advert_link_after_the_window_is_a_power_off(clock):
    assert offbeat_slow_link(clock, 25.0) == [("power", BID, pytest.approx(25))]


def test_link_loss_back_after_the_window_is_a_power_return(clock):
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        b.online = True
        await b._link_up()
        b.link.client = SimpleNamespace(is_connected=False)  # the link died
        b.bulb_lost()
        await asyncio.sleep(0.05)  # await_bulb closes the old link first
        clock.advance(40)
        device.reboot()
        advert(b)
        await asyncio.wait_for(b.presence_task, 2)
        return b.ctx.events
    assert asyncio.run(run()) == [("power", BID, pytest.approx(40))]


def test_loss_aborts_requests_in_flight():
    """A request on the dead link would hold the lock until it timed out
    (02:23:35: the OnPowerUp Set waited 4 s): the loss cancels it."""
    async def run():
        b = backend(FakeBulb(count=40))
        b.online = True
        await b._link_up()
        b.link.hang = True
        arm = asyncio.get_running_loop().create_task(b.set_powerup(b.ctx.bulb, "off"))
        await asyncio.sleep(0.05)
        assert b.lock.locked()
        b.link.client = SimpleNamespace(is_connected=False)
        b.bulb_lost()
        with pytest.raises(Offline):
            await asyncio.wait_for(arm, 0.5)
        b.presence_task.cancel()
    asyncio.run(run())


def test_fresh_device_after_a_failed_connect():
    b = backend(FakeBulb())
    b.last_device, b.last_seen = "dev", time.monotonic()
    assert b.fresh_device() == "dev"
    assert b.fresh_device(after=b.last_seen) is None  # the advert before the failure: its device may be gone


def test_connect_timeout_is_capped():
    pytest.importorskip("bleak_retry_connector")
    from bleak_retry_connector import BleakClientWithServiceCache
    seen = []

    async def fake_connect(self, **kwargs):
        seen.append(kwargs["timeout"])

    cls = mesh._quick_client()
    client = object.__new__(cls)
    original = BleakClientWithServiceCache.__dict__.get("connect")
    BleakClientWithServiceCache.connect = fake_connect
    try:
        asyncio.run(cls.connect(client, timeout=20.0, dangerous_use_bleak_cache=True))
        asyncio.run(cls.connect(client, timeout=4.0))
    finally:
        if original is None:
            del BleakClientWithServiceCache.connect
        else:
            BleakClientWithServiceCache.connect = original
    assert seen == [mesh.CONNECT_TIMEOUT_S, 4.0]


def test_link_drop_after_an_unread_return_is_no_flick(clock):
    """02:23: back after 4 h, the boot count unanswered on that link, the link
    drops 8 s later and the next read says 1: the boot already counted, not a
    second one (a toggle would darken the bulb just switched on)."""
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        b.online = True
        await b._link_up()
        b.link.client = SimpleNamespace(is_connected=False)
        b.bulb_lost()
        await asyncio.sleep(0.05)
        clock.advance(4 * 3600)
        device.reboot()
        device.answers = False
        advert(b)
        await asyncio.sleep(0.05)
        assert b.ctx.events == []  # unread: waits (before: power at once, then the drop toggled)
        clock.advance(8)
        b.link.client = SimpleNamespace(is_connected=False)  # only the link dropped
        await asyncio.sleep(0.05)
        clock.advance(5.7)
        device.answers = True
        advert(b)
        await asyncio.wait_for(b.presence_task, 2)
        return b.ctx.events
    assert asyncio.run(run()) == [("power", BID, pytest.approx(4 * 3600))]


def test_a_read_reboot_still_counts_after_a_return():
    """With the count read on the return's link, a fresh count later is a new reboot."""
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        await drop_and_return(b, device.reboot)
        b.ctx.events.clear()
        events = await drop_and_return(b, device.reboot)
        return events
    assert [e[:2] for e in asyncio.run(run())] == [("power", BID)]


# ------------------------------------------------------------ a second decision right after a flick


def flick_then_check(clock: Clock, after: float, reboot_again: bool) -> tuple[list, str]:
    """A real flick (the count restarts at 1), then `after` s later, before
    the reference is moved clear of 1..BOOT_SLACK, another off-beat check."""
    async def run():
        device = FakeBulb(count=40)
        b = clocked(device, clock)
        b.link.on_connect = lambda: clock.advance(0.1)  # each new link a moment after its advert
        b.online = True
        await b._link_up()  # reference 41
        clock.advance(60)
        device.reboot()
        b.offbeat(b.ctx.bulb, clock())
        await asyncio.wait_for(b.tasks[-1], 2)
        assert b.ctx.events == [("flick", BID)] and b.boot_counts[2] == 1
        clock.advance(after)
        if reboot_again:
            device.reboot()
        b.offbeat(b.ctx.bulb, clock())
        await asyncio.wait_for(b.tasks[-1], 2)
        return b.ctx.events, b.boot_verdicts[2][1]
    return asyncio.run(run())


def test_unknown_right_after_a_flick_is_no_second_flick(clock):
    """13:39:54-56 and 15:18:42-52: a real flick, then the next silence check
    read a count that fits the same boot and a fresh one alike ("unknown");
    counted as a reboot it toggled the light back. Within FLICK_HOLDOFF_S
    only a count that restarted decides a second flick."""
    events, verdict = flick_then_check(clock, 2.7, reboot_again=False)  # reads 2 after 1
    assert verdict == UNKNOWN and events == [("flick", BID)]


def test_a_real_second_flick_inside_the_holdoff_still_toggles(clock):
    events, verdict = flick_then_check(clock, 2.7, reboot_again=True)  # reads 1 after 1
    assert verdict == REBOOTED and events == [("flick", BID)] * 2


def test_unknown_after_the_holdoff_counts_as_before(clock):
    events, verdict = flick_then_check(clock, mesh.FLICK_HOLDOFF_S + 1, reboot_again=False)
    assert verdict == UNKNOWN and events == [("flick", BID)] * 2


# ------------------------------------------------------------ the scanner after a failed connect


class BusyScanner:
    """BlueZ's discovery: tracks whether it runs; a start right after a
    failed connect fails `busy` times (2026-10-17 10:32: the scanner stayed
    off and the bulb's return was never seen)."""

    def __init__(self, busy: int = 0) -> None:
        self.running = False
        self.busy = busy
        self.starts = self.stops = 0

    async def start(self) -> None:
        if self.busy:
            self.busy -= 1
            raise RuntimeError("org.bluez.Error.InProgress")
        self.starts += 1
        self.running = True

    async def stop(self) -> None:
        self.stops += 1
        self.running = False


class PausingLink(FakeLink):
    """Connects like MeshLink.connect: the scanner paused while connecting."""

    def __init__(self, bulb: FakeBulb, pause) -> None:
        super().__init__(bulb)
        self.pause = pause
        self.block: asyncio.Event | None = None  # a connect that hangs until cancelled

    async def ensure(self) -> None:
        if self.connected:
            return
        async with self.pause():
            if self.block is not None:
                await self.block.wait()
            await super().ensure()


def pausing(device: FakeBulb, clock: Clock, scanner: BusyScanner) -> MeshBackend:
    b = clocked(device, clock)
    b.link = PausingLink(device, b.scan_paused)
    b.link.clock = clock
    b.scanner = scanner
    b.scanning = True
    scanner.running = True
    return b


def test_failed_connect_after_an_offbeat_advert_leaves_the_scanner_running(clock):
    async def run():
        device = FakeBulb(count=40)
        scanner = BusyScanner()
        b = pausing(device, clock, scanner)
        b.online = True
        await b._link_up()  # the reference count
        assert scanner.running and scanner.stops == 1
        clock.advance(60)
        b.link.dead = True
        scanner.busy = 1  # the restart after the failed connect fails once
        b.offbeat(b.ctx.bulb, clock())
        await asyncio.wait_for(b.tasks[-1], 3)  # verify_flick: no link, the bulb is lost
        assert b.online is False
        assert scanner.running and b.scanning and b.scan_pauses == 0
        # the bulb comes back: its advert is seen and returns it
        clock.advance(4 * 3600)
        device.reboot()
        b.link.dead = False
        advert(b)
        await asyncio.wait_for(b.presence_task, 3)
        assert b.online is True
        assert b.ctx.events == [("power", BID, pytest.approx(4 * 3600))]
        assert scanner.running
    asyncio.run(run())


def test_cancelled_connect_leaves_the_scanner_running(clock):
    async def run():
        b = pausing(FakeBulb(count=40), clock, BusyScanner())
        b.link.block = asyncio.Event()
        task = asyncio.get_running_loop().create_task(b._link_up())
        await asyncio.sleep(0.05)
        assert not b.scanner.running and b.scan_pauses == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert b.scanner.running and b.scanning and b.scan_pauses == 0
    asyncio.run(run())


def test_watch_restarts_a_scanner_left_off_while_the_bulb_is_awaited(clock):
    async def run():
        scanner = BusyScanner(busy=SCAN_TRIES)
        b = pausing(FakeBulb(count=40), clock, scanner)
        b.link.dead = True
        b.online = True
        with pytest.raises(Offline):
            await b.apply(b.ctx.bulb, 30)  # no bulb: lost; every restart try fails
        assert b.online is False and not scanner.running and not b.scanning
        clock.advance(mesh.SCAN_HEAL_S)
        await b._heal_scanner()
        assert scanner.running and b.scanning
        b.link.dead = False
        advert(b)
        await asyncio.wait_for(b.presence_task, 2)
        assert b.online is True
    asyncio.run(run())


def test_watch_leaves_a_paused_or_idle_scanner_alone(clock):
    async def run():
        b = pausing(FakeBulb(count=40), clock, BusyScanner())
        b.scanning = b.scanner.running = False
        b.scan_pauses = 1  # a connect in progress
        clock.advance(mesh.SCAN_HEAL_S)
        await b._heal_scanner()
        assert not b.scanner.running
        b.scan_pauses, b.idle_mode = 0, True
        clock.advance(mesh.SCAN_HEAL_S)
        await b._heal_scanner()
        assert not b.scanner.running
    asyncio.run(run())


SCAN_TRIES = getattr(mesh, "SCAN_START_TRIES", 3)


@pytest.mark.parametrize("exc, logged", [
    (mesh.LinkAborted("request cancelled: the link is being replaced"), False),
    (asyncio.CancelledError(), False),
    (RuntimeError("boom"), True),
])
def test_after_connect_ends_quietly_when_the_link_is_replaced(exc, logged, monkeypatch, caplog):
    b = backend(FakeBulb())

    async def fail(address):
        raise exc

    monkeypatch.setattr(b, "_after_connect", fail)
    with caplog.at_level("DEBUG", logger="hauslicht"):
        asyncio.run(b.after_connect("AA:BB"))  # never raises
    assert any(r.levelname == "ERROR" for r in caplog.records) == logged


# ------------------------------------------------------------ exclusive: link epoch, stray cancels


def test_exclusive_epoch_moves_only_when_a_session_is_not_the_same_boot():
    """2026-10-21: every silence check opened a session and the core armed
    again each time (same boot, 80 times in 17 minutes). The epoch the core
    resets on moves for a reboot, never for a session of the same boot."""
    async def run():
        device = FakeBulb(count=40)
        b = backend(device)
        b.online = True
        changed = []
        b.ctx.changed = lambda: changed.append(b.link_epoch)
        b.radio = SimpleNamespace(dead=None)  # exclusive
        await b._link_up()  # first contact: unknown, a new epoch
        first = b.status(b.ctx.bulb)["link_epoch"]
        assert first == b.link.connected_at and changed == [first]
        for _ in range(3):  # silence checks: a new session each, the same boot
            await b.link.close()
            await asyncio.sleep(0.01)
            await b._link_up()
            assert b.boot_verdicts[2][1] == SAME
            assert b.status(b.ctx.bulb)["link_epoch"] == first
        assert changed == [first]
        device.reboot()  # a real reboot
        await b.link.close()
        await asyncio.sleep(0.01)
        await b._link_up()
        assert b.boot_verdicts[2][1] == REBOOTED
        moved = b.status(b.ctx.bulb)["link_epoch"]
        assert moved == b.link.connected_at != first and changed == [first, moved]
    asyncio.run(run())


def test_gatt_status_carries_no_epoch():
    """GATT: a new link may follow a reboot the count cannot tell; the core
    keeps resetting on every connect (no link_epoch in the status)."""
    async def run():
        b = backend(FakeBulb(count=40))
        b.online = True
        await b._link_up()
        assert "link_epoch" not in b.status(b.ctx.bulb)
    asyncio.run(run())


def _caught_own_cancel() -> None:
    """Leave the current task as Daemon.run leaves it for stop(): its cancel
    caught, never uncancelled (cancelling() stays 1)."""
    asyncio.current_task().cancel()


def test_abort_while_the_caller_is_stopping_is_link_aborted():
    """16:21:27: the stop's OnPowerUp Set was cancelled by a silence check's
    abort(); the caller had caught SIGTERM's cancel (cancelling() == 1), so a
    bare CancelledError went up and killed stop(). It is Offline now."""
    async def run():
        b = backend(FakeBulb(count=40))
        b.online = True
        await b._link_up()
        _caught_own_cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert asyncio.current_task().cancelling() == 1
        b.link.hang = True
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, b.link.abort)
        with pytest.raises(Offline):
            await b.set_powerup(b.ctx.bulb, "on")
        assert asyncio.current_task().cancelling() == 1  # nothing to uncancel
        # the caller's own cancel since still propagates as one
        b.link.hang = True
        b.device_powerup.pop(2, None)
        loop.call_later(0.05, asyncio.current_task().cancel)
        with pytest.raises(asyncio.CancelledError):
            await b.set_powerup(b.ctx.bulb, "off")
        assert asyncio.current_task().cancelling() == 2
        # and a timeout around it is a TimeoutError, not an abort
        b.link.hang = True
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await b.set_powerup(b.ctx.bulb, "off")
        assert asyncio.current_task().cancelling() == 2
    asyncio.run(run())
