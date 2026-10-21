"""Identify and Test: one bulb taken out of following for a while, then given back.

Identify blinks a bulb slowly (IDENTIFY_HALF_S at full brightness, as long
off) until it is switched off, flicked, or IDENTIFY_MAX_S is up: every mesh
message burns a SEQ (24 bits, no IV update), so a forgotten toggle must not
run for hours. Test shows what a bulb and its link manage: brightest, a fade
to the dimmest lit level (the backend's own transition where it has one, one
message; else TEST_FADE_STEPS steps), its colours (red, green, blue, white;
white-only: warmest and coldest), then on/off from fast to slow, counting the
switches the backend took (apply returned: acknowledged where the backend
waits for an answer, sent otherwise) per second.

While either runs, the core's worker skips the bulb (Daemon.serve: no level,
colour or OnPowerUp); every message here goes out under the kind's lock, so
never two calls at once per backend. Power, clap, lid and profile changes
meanwhile only move the intended state. At the end the bulb gets its intended
state again (level, power, colour) and the worker takes over. A flick, the
bulb going offline, `remove`, `use=off`, suspend and daemon stop end it.

Notify (a desktop notification matched a rule, haus/notifywatch.py) runs the
rule's pattern (profiles.notify_steps) the same way: never two light changes
less than NOTIFY_MIN_STEP_S apart, the rule's colour on colour bulbs first and
the colour back last. A lit bulb dims to its lowest lit level between
flashes, never off. Identify and Test take a bulb from it (they come first);
a new notification for a bulb that is busy is dropped, never queued.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .backends import Offline
from .profiles import NOTIFY_MIN_STEP_S, hex_hs, notify_steps

log = logging.getLogger("hauslicht")

IDENTIFY_HALF_S = 1.0    # on this long, then off this long
IDENTIFY_MAX_S = 120.0   # stops by itself after this
TEST_BRIGHT_S = 1.5      # 100 % this long
TEST_FADE_S = 4.0        # 100 % to the dimmest lit level
TEST_FADE_STEPS = 8      # without a backend transition
TEST_COLOR_S = 1.0       # per colour
TEST_STROBE = (0.1, 0.15, 0.25, 0.4, 0.7, 1.0)  # half-periods, fast to slow
TEST_STROBE_CYCLES = 3   # on/off cycles per half-period
END_WAIT_S = 3.0         # suspend / daemon stop: how long a bulb may take to be given back

FAILED = object()  # _call: the backend refused or failed

COLORS_HS = ({"mode": "hs", "hue": 0.0, "sat": 100.0},    # red
             {"mode": "hs", "hue": 120.0, "sat": 100.0},  # green
             {"mode": "hs", "hue": 240.0, "sat": 100.0},  # blue
             {"mode": "hs", "hue": 0.0, "sat": 0.0})      # white


class Action:
    """One Identify or Test on one bulb (Daemon.actions holds the running ones)."""

    def __init__(self, daemon, bulb, kind: str, rule: dict | None = None, lit: bool = True) -> None:
        self.d, self.bulb, self.kind = daemon, bulb, kind
        self.rule = rule         # notify: the matched rule
        self.lit = lit           # notify: the bulb was lit (dims between flashes instead of going dark)
        self.stop = asyncio.Event()
        self.why = ""            # why it ended early: stop, flick, offline, suspend, daemon stop, limit, …
        self.restore = True      # False: handed over to a Test, or the bulb is gone
        self.step = "starting"   # test: bright, fade, colour, strobe; identify: on, off
        self.rate: float | None = None  # test: the most switches per second a strobe phase reached
        self.fade = ""           # test: "transition" (the backend's) or "steps"
        self.colored = False     # the test changed the colour
        self.before_sent = bulb.sent
        self.before_color = bulb.sent_color
        self.task: asyncio.Task | None = None

    # ------------------------------------------------------------ control

    def end(self, why: str, restore: bool = True) -> None:
        if not self.stop.is_set():
            self.why = why
        self.restore = self.restore and restore
        self.stop.set()

    def describe(self) -> dict:
        return {"step": self.step, "rate": self.rate, "running": True}

    async def wait(self, seconds: float) -> bool:
        """Sleep; True when told to stop meanwhile."""
        if seconds > 0 and not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), seconds)
        return self.stop.is_set()

    async def _call(self, what: str, *args):
        """One backend call under the kind's lock; FAILED on failure (Offline ends the action)."""
        b = self.bulb
        async with self.d.locks[b.kind]:
            try:
                return await getattr(b.backend, what)(b, *args)
            except Offline:
                self.end("offline")
            except Exception as exc:
                log.warning("%s: %s %s: %s", b.id, self.kind, what, exc)
        return FAILED

    async def send(self, percent: float) -> bool:
        if self.stop.is_set():
            return False
        return await self._call("apply", percent) is not FAILED

    # ------------------------------------------------------------ run

    async def run(self) -> None:
        b = self.bulb
        log.info("%s: %s started", b.id, self.kind)
        try:
            try:
                await {"identify": self.identify, "test": self.test, "notify": self.notify}[self.kind]()
            except Exception as exc:  # never fatal: the bulb is given back all the same
                log.warning("%s: %s failed: %s", b.id, self.kind, exc)
                self.why = self.why or "error"
            if self.restore and b.id in self.d.bulbs:
                await self.give_back()
        finally:
            self.d.action_done(self)
        log.info("%s: %s ended%s", b.id, self.kind, f" ({self.why})" if self.why else "")

    async def identify(self) -> None:
        deadline = time.monotonic() + IDENTIFY_MAX_S
        while True:
            for self.step, percent in (("on", 100.0), ("off", 0.0)):
                await self.send(percent)
                if await self.wait(min(IDENTIFY_HALF_S, deadline - time.monotonic())):
                    return
                if time.monotonic() >= deadline:
                    self.why = "limit"
                    return

    async def test(self) -> None:
        d, b = self.d, self.bulb
        caps = d.caps(b)
        if ("hs" in caps or "ct" in caps) and d.color(b) is None and self.before_color is None:
            got = await self._call("query")  # the colour left alone: what the bulb shows, to put it back
            self.before_color = (got or {}).get("color") if isinstance(got, dict) else None
        self.step = "bright"
        await self.send(100.0)
        if await self.wait(TEST_BRIGHT_S):
            return
        self.step = "fade"
        low = d.min_lit(b)
        if await self._call("fade", low, TEST_FADE_S) is True:
            self.fade = "transition"
            if await self.wait(TEST_FADE_S):
                return
        else:
            self.fade = "steps"
            for i in range(1, TEST_FADE_STEPS + 1):  # even steps to the eye: geometric
                await self.send(round(100.0 * (low / 100.0) ** (i / TEST_FADE_STEPS), 2))
                if await self.wait(TEST_FADE_S / TEST_FADE_STEPS):
                    return
        if "hs" in caps:
            colors = list(COLORS_HS)
        elif "ct" in caps:
            warm, cold = b.backend.ct_range(b)
            colors = [{"mode": "ct", "kelvin": int(warm)}, {"mode": "ct", "kelvin": int(cold)}]
        else:
            colors = []
        if colors:
            self.step = "colour"
            self.colored = True
            await self.send(100.0)
            for color in colors:
                if not self.stop.is_set():
                    await self._call("apply_color", color)
                if await self.wait(TEST_COLOR_S):
                    return
        self.step = "strobe"
        for half in TEST_STROBE:
            sent, t0 = 0, time.monotonic()
            for k in range(2 * TEST_STROBE_CYCLES):
                if await self.send(100.0 if k % 2 == 0 else 0.0):
                    sent += 1
                if await self.wait(t0 + (k + 1) * half - time.monotonic()):
                    return
            rate = sent / max(time.monotonic() - t0, 1e-6)
            self.rate = round(max(self.rate or 0.0, rate), 1)
            log.info("%s: test on/off every %.2f s: %.1f switches per second", b.id, half, rate)
        self.step = "done"

    async def notify(self) -> None:
        """The rule's pattern; its colour first and the colour back last, all
        at most one change per NOTIFY_MIN_STEP_S."""
        d, b, rule = self.d, self.bulb, self.rule
        high = float(rule["intensity"])
        low = d.min_lit(b) if self.lit else 0.0
        back = None  # the colour to put back after the rule's
        if rule["color"] and "hs" in d.caps(b):
            back = d.color(b) or b.sent_color
            if back is None:
                got = await self._call("query")
                back = (got or {}).get("color") if isinstance(got, dict) else None
            if back is not None:  # a colour that cannot be put back is never changed
                self.step = "colour"
                if await self._call("apply_color", hex_hs(rule["color"])) is not FAILED:
                    b.sent_color = None
                    if await self.wait(NOTIFY_MIN_STEP_S):
                        await self._color_back(back)
                        return
                else:
                    back = None
        try:
            for i, (level, hold) in enumerate(notify_steps(rule["pattern"], rule["count"], high, low)):
                self.step = f"{rule['pattern']} {i + 1}"
                await self.send(level)
                if await self.wait(hold):
                    return
            self.step = "done"
        finally:
            if back is not None:
                await self._color_back(back)

    async def _color_back(self, color: dict) -> None:
        """The colour as it was (an end by stop or offline: as far as possible), then a pause."""
        if self.why == "offline":
            return
        if await self._call("apply_color", color) is not FAILED:
            self.bulb.sent_color = color
        if not self.stop.is_set():
            await self.wait(NOTIFY_MIN_STEP_S)  # the level given back next keeps the pace

    async def give_back(self) -> None:
        """The intended state again: the level now (its want; paused without
        one: the last sent), the colour (the worker sends the intended one; a
        colour left alone is put back as it was)."""
        d, b = self.d, self.bulb
        want = d.want(b)
        level = want if want is not None else b.sent if b.sent is not None else self.before_sent
        intended_color = d.color(b)
        async with d.locks[b.kind]:
            try:
                if level is not None:
                    await b.backend.apply(b, level)
                b.sent = level
                if self.colored:
                    b.sent_color = None  # the worker sends the intended colour while lit
                    if intended_color is None and self.before_color and b.power and (level or 0) > 0:
                        if await b.backend.apply_color(b, self.before_color):
                            b.sent_color = self.before_color
            except Offline:
                b.sent, b.sent_color = None, None  # sent again when it is back
            except Exception as exc:
                b.sent, b.sent_color = None, None  # the worker retries with its back-off
                log.warning("%s: giving the bulb back after %s: %s", b.id, self.kind, exc)
        d.wake.set()
