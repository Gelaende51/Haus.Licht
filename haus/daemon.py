"""The core: bulbs, profiles, widgets and their selections; the screen loop; the ctl socket.

Every bulb's want comes from its own profile, power and pause state; one
worker per backend sends it (never two calls at once per backend) and keeps
OnPowerUp armed for wall-switch flicks. A bulb not in use is idle: not
driven at all, its OnPowerUp left at "on". Widgets only choose what their
controls act on; a used bulb in no widget is still driven by its profile. Backends call back through
Context; `watch` clients get every intended-state change and device event as
it happens. The protocol is in docs/ctl-protocol.md, the backend contract in
docs/backend-api.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import time
from pathlib import Path

from . import backends as registry
from . import rootsetup
from .backends import QUICK_GAP_S, Offline
from .common import UserError, _, link_lock, read_state, save_state
from .desktop import Desktop, KbdSlider
from .kde import Kde, panel_disabled
from .profiles import (
    DEFAULT, FAST_FLICK, KBD_BINDING, PROFILE_KEYS, SETTABLE_GLOBALS, check_global_value, check_icon,
    flick_toggles,
    SCHEMA, check_profile_value, check_widget_count, clean_selected, clean_widget_bulbs, color_moved,
    color_rgb, color_want, compute_want, is_tracking, kbd_to_value, migrate, new_state, parse_bool, powerup_target, value_to_kbd,
)

log = logging.getLogger("hauslicht")

# QUICK_GAP_S (haus/backends): back within this after the drop: a flick; longer: a power-off, back lit
ARM_RETRY_S = 60.0  # after an OnPowerUp Get/Set got no answer on a link that stays up (a new link: at once)
NUDGE_ECHO_S = 3.0  # a keyboard-slider echo of a nudge's own push arrives within this
SEND_STEP = 0.25  # resend only when the want moved this much
BACKOFF_S, BACKOFF_MAX_S = 5.0, 300.0
SHUTDOWN_ARM_S = 8.0
DISCOVER_S = 5.0
POWERUP_INT = {"off": 0, "on": 1, "restore": 2}  # get's `onpowerup` keeps the old numbers
WATCH_QUEUE = 1000  # lines a watcher may lag behind before it is dropped
MANUAL_SAVE_S = 2.0  # a manual level is written this long after its last change


class Bulb:
    """A bulb at runtime: its json entry plus what only the core tracks."""

    def __init__(self, bid: str, entry: dict) -> None:
        self.id = bid
        self.entry = entry
        self.backend = None
        self.paused = False  # a manual command rules until resume / a setting change
        self.manual: float | None = None  # the manual level while paused
        self.manual_exact = False  # `dim`: sent as it is, no lowest-lit floor
        self.sent: float | None = None  # last percent the backend took
        self.sent_color: dict | None = None  # last colour the backend took (None: send it again)
        self.color_retry_at = 0.0
        self.armed: str | None = None  # OnPowerUp as last confirmed (None: unknown)
        self.arm_retry_at = 0.0
        self.connected = False
        self.online: bool | None = None
        self.power_before_loss = self.power
        self.extra: dict = {}

    @property
    def kind(self) -> str:
        return self.entry["kind"]

    @property
    def name(self) -> str:
        return self.entry.get("name", "")

    @property
    def config(self) -> dict:
        return self.entry.setdefault("config", {})

    @property
    def profile(self) -> str:
        return self.entry.get("profile", DEFAULT)

    @property
    def power(self) -> bool:
        return bool(self.entry.get("power"))

    @power.setter
    def power(self, on: bool) -> None:
        self.entry["power"] = bool(on)

    @property
    def use(self) -> bool:
        return self.entry.get("use", True) is not False

    def reset(self) -> None:
        self.paused, self.manual, self.manual_exact, self.sent = False, None, False, None
        self.sent_color = None


class Context:
    """What a backend may use of the core."""

    def __init__(self, daemon: Daemon, kind: str) -> None:
        self._d, self.kind = daemon, kind

    @property
    def state(self) -> dict:
        return self._d.state

    @property
    def state_path(self) -> Path:
        return self._d.state_path

    def save(self) -> None:
        self._d.save()

    def bulbs(self) -> list[Bulb]:
        return self._d.bulbs_of(self.kind)

    def flick(self, bulb_id: str) -> None:
        self._d.on_flick(bulb_id)

    def power_returned(self, bulb_id: str, gap_s: float | None) -> None:
        self._d.on_power_returned(bulb_id, gap_s)

    def changed(self) -> None:
        self._d.scan_status()
        self._d.wake.set()


class Watcher:
    """One `watch` connection: its lines wait here until the socket takes them."""

    def __init__(self, writer) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(WATCH_QUEUE)


def _kv(items: list[str]) -> dict:
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise UserError(f"expected key=value, got {item!r}")
        out[key.strip()] = value
    return out


class Daemon:
    def __init__(self, state_path: Path, sock_path: Path, interval: float = 0.3,
                 desktop_writes: bool = True, desktop=None, kde=None) -> None:
        self.state_path, self.sock_path, self.interval = state_path, sock_path, interval
        self.state = read_state(state_path) or new_state()
        if migrate(self.state):
            log.info("%s migrated to schema %d", state_path.name, SCHEMA)
        self.bulbs: dict[str, Bulb] = {bid: Bulb(bid, e) for bid, e in self.state["bulbs"].items()}
        for b in self.bulbs.values():  # a manual level survives a restart (power as stored: dark stays dark)
            kept = b.entry.get("manual")
            if kept is not None and b.use:
                b.paused, b.manual, b.manual_exact = True, kept["level"], kept["exact"]
                log.info("%s: manual level %s kept from before the restart", b.id, kept["level"])
        self.manual_saver: asyncio.Task | None = None
        self.setup_task: asyncio.Task | None = None
        self.setup_note = ""  # the last system setup's failure, for the widget
        self.desktop = desktop or Desktop(self.slider_moved, self.before_sleep, self.after_wake, desktop_writes)
        self.backends: dict[str, registry.Backend] = {}  # instances, started or not
        self.started: set[str] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.wake = asyncio.Event()
        self.lid_closed = self.desktop.lid_closed()  # only changes are events; nothing at start
        self.sleeping = False  # between before_sleep and after_wake: hands off
        self.kbd_echo: dict[int, float] = {}  # keyboard-slider values a nudge pushed, and when
        self.kbd_want: int | None = None
        self.kbd_pusher: asyncio.Task | None = None
        self.idle_kinds: set[str] = set()  # kinds whose bulbs are all idle (told so)
        self.watchers: set[Watcher] = set()
        self.intended: dict[str, dict] = {}  # bulb id -> last reported intended state
        with contextlib.suppress(AttributeError):
            self.desktop.env.on_change = self.wake.set  # a Night Light step: at once
        # shortcuts, OSD, panel widgets: only with the real desktop (tests inject theirs)
        self.kde = kde if kde is not None else (
            Kde(self, panel=not panel_disabled(not desktop_writes)) if desktop is None else None)

    # ------------------------------------------------------------------ model

    @property
    def settings(self) -> dict:
        return self.state["settings"]

    @property
    def profiles(self) -> dict:
        return self.state["profiles"]

    def save(self) -> None:
        save_state(self.state_path, self.state)

    def bulbs_of(self, kind: str) -> list[Bulb]:
        return [b for b in self.bulbs.values() if b.kind == kind]

    def find(self, which: str) -> Bulb:
        """A bulb by id, or a mesh node by unicast (0x0002 / 0002)."""
        if which in self.bulbs:
            return self.bulbs[which]
        with contextlib.suppress(ValueError):
            addr = int(which.split(":")[-1], 16)
            for b in self.bulbs_of("mesh"):
                if int(b.config.get("unicast", "-1"), 16) == addr:
                    return b
        raise UserError(_("unknown bulb {which}; known: {known}").format(
            which=which, known=", ".join(self.bulbs) or "-"))

    @property
    def widget_count(self) -> int:
        return self.settings["widgets"]

    def widget(self, token: str) -> int:
        """Widget number of an `@N` token."""
        with contextlib.suppress(ValueError):
            w = int(token)
            if 1 <= w <= self.widget_count:
                return w
        raise UserError(_("no widget {w} (widgets: 1-{n})").format(w=token, n=self.widget_count))

    def active(self, bulb: Bulb) -> bool:
        """In use: driven. Otherwise idle."""
        return bulb.use

    def active_bulbs(self) -> list[Bulb]:
        return [b for b in self.bulbs.values() if self.active(b)]

    def widget_list(self, w: int) -> list[str]:
        """Widget w's stored bulb ids (unused ones too: they come back with use=on)."""
        return self.settings["widget_bulbs"].setdefault(str(w), [])

    def in_widget(self, w: int) -> list[Bulb]:
        """Widget w's "all": its used bulbs, in list order."""
        ids = set(self.widget_list(w))
        return [b for b in self.bulbs.values() if b.id in ids and self.active(b)]

    def selection(self, w: int = 1) -> str:
        sel = self.settings["selected"].get(str(w), "all")
        ok = sel == "all" or (sel in self.bulbs and sel in self.widget_list(w) and self.active(self.bulbs[sel]))
        return sel if ok else "all"

    def selected(self, w: int = 1) -> list[Bulb]:
        sel = self.selection(w)
        return self.in_widget(w) if sel == "all" else [self.bulbs[sel]]

    def targets(self, which: str | None, w: int = 1) -> list[Bulb]:
        return self.selected(w) if which in (None, "") else (
            self.in_widget(w) if which == "all" else [self.find(which)])

    def driven(self, bulbs: list[Bulb]) -> list[Bulb]:
        """Refuse power and level commands for an idle bulb named on its own."""
        for b in bulbs:
            if not self.active(b):
                raise UserError(_("{bid} is not in use").format(bid=b.id))
        return bulbs

    def selected_profile_name(self, w: int = 1) -> str:
        sel = self.selection(w)
        if sel == "all":
            return DEFAULT
        name = self.bulbs[sel].profile
        return name if name in self.profiles else DEFAULT

    def selected_profile(self, w: int = 1) -> dict:
        return self.profiles[self.selected_profile_name(w)]

    def clean_selection(self) -> None:
        """Widget lists in list order and known ids only; selections of bulbs
        that left their widget or went out of use fall back to "all"."""
        before = self.selection(1)
        self.settings["widget_bulbs"] = clean_widget_bulbs(self.settings["widget_bulbs"],
                                                           self.state["bulbs"], self.widget_count)
        self.settings["selected"] = clean_selected(self.settings["selected"], self.state["bulbs"],
                                                   self.settings["widget_bulbs"], self.widget_count)
        if self.selection(1) != before:
            self.later(self.sync_kbd())

    def profile_of(self, bulb: Bulb) -> dict:
        return self.profiles.get(bulb.profile) or self.profiles[DEFAULT]

    def using(self, name: str) -> list[Bulb]:
        return [b for b in self.bulbs.values()
                if b.profile == name or (name == DEFAULT and b.profile not in self.profiles)]

    def tracking(self, profile: dict) -> bool:
        env = self.desktop.env
        return is_tracking(profile["track"], env.night, env.day, env.scheme)

    def min_lit(self, bulb: Bulb) -> float:
        return bulb.backend.min_lit(bulb) if bulb.backend else 1.0

    def caps(self, bulb: Bulb) -> set[str]:
        return set(bulb.backend.caps(bulb)) if bulb.backend else set()

    def want(self, bulb: Bulb, power: bool | None = None, paused: bool | None = None) -> float | None:
        """What a bulb should show now; None while a manual command rules."""
        profile = self.profile_of(bulb)
        return compute_want(profile, power=bulb.power if power is None else power,
                            paused=bulb.paused if paused is None else paused,
                            manual=bulb.manual, manual_exact=bulb.manual_exact,
                            screen=self.desktop.screen_level(), tracking=self.tracking(profile),
                            mode=self.settings["mode"], min_lit=self.min_lit(bulb))

    def color(self, bulb: Bulb) -> dict | None:
        """The colour a bulb should show; None: leave it alone."""
        if bulb.backend is None:
            return None
        caps = self.caps(bulb)
        ct_range = bulb.backend.ct_range(bulb) if "ct" in caps else None
        return color_want(self.profile_of(bulb), caps, getattr(self.desktop.env, "screen_kelvin", None),
                          *([ct_range] if ct_range else []))

    def powerup_target(self, bulb: Bulb) -> str:
        """Flick mode: boot into what a toggle would show. Otherwise "on": the
        wall switch behaves as with any bulb."""
        if not flick_toggles(self.settings["flick"]):
            return "on"
        return powerup_target(self.want(bulb, power=not bulb.power, paused=False), self.min_lit(bulb))

    # ------------------------------------------------------------------ backends

    def instance(self, kind: str) -> registry.Backend:
        if kind not in self.backends:
            cls = registry.backend_class(kind)
            self.backends[kind] = cls(Context(self, kind))
        return self.backends[kind]

    async def start_backend(self, kind: str) -> None:
        """Instance, start and worker for a kind that has bulbs."""
        if kind in self.started:
            return
        try:
            backend = self.instance(kind)
        except UserError as exc:
            log.warning("bulbs of kind %s stay dark: %s", kind, exc)
            return
        self.started.add(kind)
        for b in self.bulbs_of(kind):
            b.backend = backend
        self.locks[kind] = asyncio.Lock()
        self.events[kind] = asyncio.Event()
        try:
            await backend.start()
        except Exception as exc:
            log.warning("backend %s did not start: %s", kind, exc)
        self.workers[kind] = asyncio.get_running_loop().create_task(self.worker(kind))

    async def worker(self, kind: str) -> None:
        backend, event, lock = self.backends[kind], self.events[kind], self.locks[kind]
        backoff = BACKOFF_S
        while True:
            await event.wait()
            event.clear()
            if self.sleeping:
                continue  # before_sleep() set the bulbs up for the night
            try:
                async with lock:
                    bulbs = self.bulbs_of(kind)
                    if self.needs_link(bulbs) and kind in self.idle_kinds:
                        await self.backend_idle(kind, False)  # before the first send
                    for bulb in bulbs:
                        with contextlib.suppress(Offline):  # the backend watches for it; status() tells
                            await self.serve(backend, bulb)
                    if bulbs and not self.needs_link(bulbs) and kind not in self.idle_kinds:
                        await self.backend_idle(kind, True)  # after the last arming
                backoff = BACKOFF_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: %s; retrying in %.0f s", kind, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    def needs_link(self, bulbs: list[Bulb]) -> bool:
        """A used bulb, or an idle one whose OnPowerUp "on" is still unconfirmed
        (and that is not known to be without power)."""
        return any(self.active(b) or ("powerup" in self.caps(b) and b.armed != "on" and b.online is not False)
                   for b in bulbs)

    async def backend_idle(self, kind: str, idle: bool) -> None:
        """Tell a backend that all its bulbs are idle (it may drop its link) or
        that one is driven again."""
        if idle:
            self.idle_kinds.add(kind)
        else:
            self.idle_kinds.discard(kind)
        log.info("%s: %s", kind, "every bulb idle" if idle else "driven again")
        try:
            await self.backends[kind].idle(idle)
        except Exception as exc:
            log.warning("%s: idle(%s): %s", kind, idle, exc)

    async def serve(self, backend, bulb: Bulb) -> None:
        """Send one bulb's want if it moved; keep its OnPowerUp armed. An idle
        bulb only gets OnPowerUp "on" (the wall switch as with any bulb),
        retried like any arming, and nothing else."""
        if bulb.online is False or bulb.id not in self.bulbs:
            return
        if not self.active(bulb):
            if "powerup" in self.caps(bulb) and bulb.armed != "on" and time.monotonic() >= bulb.arm_retry_at:
                await self.arm(backend, bulb, "on")
            return
        want = self.want(bulb)
        if want is not None and (bulb.sent is None or abs(want - bulb.sent) >= SEND_STEP):
            await backend.apply(bulb, want)
            bulb.sent = want
            log.info("%s: screen %s -> %.1f %%", bulb.id, "on" if self.desktop.screen_on else "off", want)
        await self.serve_color(backend, bulb, want)
        if "powerup" in self.caps(bulb):
            target = self.powerup_target(bulb)
            if bulb.armed != target and time.monotonic() >= bulb.arm_retry_at:
                await self.arm(backend, bulb, target)

    async def serve_color(self, backend, bulb: Bulb, want: float | None) -> None:
        """Send the colour when it moved, only while the bulb is lit; dark, it
        is forgotten so that switching on sends it again."""
        lit = bulb.power and (want if want is not None else bulb.sent or 0.0) > 0
        if not lit:
            bulb.sent_color = None
            return
        color = self.color(bulb)
        if not color_moved(color, bulb.sent_color) or time.monotonic() < bulb.color_retry_at:
            return
        if await backend.apply_color(bulb, color):
            bulb.sent_color = color
            log.info("%s: colour %s", bulb.id, color)
        else:
            bulb.color_retry_at = time.monotonic() + ARM_RETRY_S
            log.warning("%s: colour %s not confirmed; retrying in %.0f s", bulb.id, color, ARM_RETRY_S)

    async def arm(self, backend, bulb: Bulb, target: str) -> None:
        if await backend.set_powerup(bulb, target):
            bulb.armed = target
            log.info("%s: OnPowerUp %s (a power cut boots it %s)", bulb.id, target,
                     "lit" if target == "on" else "dark" if target == "off" else "as before")
        else:
            bulb.armed = None
            bulb.arm_retry_at = time.monotonic() + ARM_RETRY_S
            log.warning("%s: OnPowerUp %s not confirmed; retrying in %.0f s", bulb.id, target, ARM_RETRY_S)

    def scan_status(self) -> None:
        """Note connects (OnPowerUp is read again on a new link), losses (the
        power before them) and returns (send again)."""
        for bulb in list(self.bulbs.values()):
            st = bulb.backend.status(bulb) if bulb.backend else {"connected": False, "online": False}
            connected, online = bool(st.get("connected")), st.get("online")
            if connected != bulb.connected:
                self.emit_event("connected" if connected else "disconnected", bulb)
            if connected and not bulb.connected:
                bulb.armed = None
                bulb.sent_color = None  # a new link: the colour is sent again
                bulb.arm_retry_at = bulb.color_retry_at = 0.0  # a failure on the old link waits no longer
            if online is False and bulb.online is not False:
                bulb.power_before_loss = bulb.power
                self.emit_event("dropout", bulb, power_before=bulb.power)
            elif online is not False and bulb.online is False:
                bulb.reset()
                bulb.arm_retry_at = bulb.color_retry_at = 0.0
            bulb.connected, bulb.online, bulb.extra = connected, online, st

    # ------------------------------------------------------------------ backend events

    def on_flick(self, bid: str) -> None:
        bulb = self.bulbs.get(bid)
        if bulb is None:
            return
        if not self.active(bulb):
            self.emit_event("flick", bulb, idle=True)
            return  # idle: the bulb does what the wall switch says
        bulb.power = (not bulb.power) if flick_toggles(self.settings["flick"]) else True
        self.emit_event("flick", bulb, power=bulb.power)
        bulb.reset()
        self.save()
        log.info("%s: flick -> %s%s", bid, "on" if bulb.power else "off",
                 "" if flick_toggles(self.settings["flick"]) else " (flick mode off: a power cycle means on)")
        self.wake.set()

    def on_power_returned(self, bid: str, gap: float | None) -> None:
        bulb = self.bulbs.get(bid)
        if bulb is None:
            return
        if not self.active(bulb):
            self.emit_event("returned", bulb, gap=gap, idle=True)
            return
        if gap is None:
            log.info("%s: first contact, power stays %s", bid, "on" if bulb.power else "off")
        elif gap <= QUICK_GAP_S and flick_toggles(self.settings["flick"]):
            bulb.power = not bulb.power_before_loss
            log.info("%s: back %.1f s after the drop: flick -> %s", bid, gap, "on" if bulb.power else "off")
        else:  # a power-off (or flick mode off): back lit, whatever it was before
            bulb.power = True
            log.info("%s: back %.1f s after the drop: switched on", bid, gap)
        self.emit_event("returned", bulb, gap=gap, power=bulb.power)
        bulb.reset()
        self.save()
        self.wake.set()

    # ------------------------------------------------------------------ power, lid, sleep

    def set_power(self, bulbs: list[Bulb], on: bool, why: str) -> None:
        for b in bulbs:
            b.power = on
            b.reset()
        self.save()
        log.info("power %s: %s (%s)", "on" if on else "off", ", ".join(b.id for b in bulbs) or "-", why)
        self.wake.set()

    def check_lid(self) -> None:
        """Lid events switch every bulb like `off` / `on`; the state itself does not."""
        closed = self.desktop.lid_closed()
        if closed == self.lid_closed:
            return
        self.lid_closed = closed
        when = self.settings["lid"]
        if closed and when in ("close", "both"):
            self.set_power(self.active_bulbs(), False, "lid closed")
        elif not closed and when in ("open", "both"):
            self.set_power(self.active_bulbs(), True, "lid opened")
        else:
            log.info("lid %s (lid=%s: no switch)", "closed" if closed else "opened", when)

    async def before_sleep(self) -> None:
        """Dark while asleep if lid is close/both; OnPowerUp back to "on" always."""
        self.sleeping = True  # the workers must not relight or re-arm meanwhile
        self.check_lid()  # the lid close that sent the laptop to sleep
        dark = self.settings["lid"] in ("close", "both")
        for kind in list(self.started):
            backend = self.backends[kind]
            async with self.locks[kind]:
                for bulb in self.bulbs_of(kind):
                    if bulb.online is False:
                        continue  # no power at the wall: nothing to switch off
                    with contextlib.suppress(Offline):
                        if dark and self.active(bulb):
                            await backend.apply(bulb, 0.0)
                            bulb.sent, bulb.sent_color = 0.0, None
                        if "powerup" in self.caps(bulb) and bulb.armed != "on":
                            bulb.armed = "on" if await backend.set_powerup(bulb, "on") else None
            with contextlib.suppress(Exception):
                await backend.suspend()
        log.info("suspend: %s", "bulbs off" if dark else "bulbs left as they are")

    async def after_wake(self) -> None:
        log.info("resumed from suspend")
        self.sleeping = False
        for kind in list(self.started):
            with contextlib.suppress(Exception):
                await self.backends[kind].resume()
        for b in self.bulbs.values():
            b.sent, b.sent_color = None, None
        self.wake.set()

    async def shutdown_arm(self) -> None:
        """Daemon stops: OnPowerUp back to "on" (bounded wait; skipped for bulbs
        out of reach or known to be "on" already)."""
        try:
            async with asyncio.timeout(SHUTDOWN_ARM_S):
                for kind in list(self.started):
                    backend = self.backends[kind]
                    todo = [b for b in self.bulbs_of(kind)
                            if "powerup" in self.caps(b) and b.online is not False and b.armed != "on"]
                    if not todo:
                        continue
                    async with self.locks[kind]:
                        for b in todo:
                            with contextlib.suppress(Offline):
                                ok = await backend.set_powerup(b, "on")
                                b.armed = "on" if ok else None
                                log.info("shutdown: %s OnPowerUp on (%s)", b.id, "ok" if ok else "no answer")
        except Exception as exc:
            log.warning("shutdown: OnPowerUp not reset: %s", exc)

    # ------------------------------------------------------------------ slider, settings, profiles

    def slider_moved(self, value: int) -> None:
        """The keyboard slider moved: move the widget control the mode binds."""
        pushed = self.kbd_echo.pop(value, None)
        if pushed is not None and time.monotonic() - pushed < NUDGE_ECHO_S:
            return  # the echo of a nudge's push: applied already
        self.apply_slider(value)

    def apply_slider(self, value: int) -> None:
        self.settings["slider"] = value
        key = KBD_BINDING.get(self.settings["mode"])
        if key is None or value == value_to_kbd(key, self.selected_profile()[key]):
            self.save()  # unbound, or the echo of our own push
            return
        log.info("keyboard slider %d -> %s of profile %s", value, key, self.selected_profile_name())
        self.set_value(key, kbd_to_value(key, value), from_kbd=True)

    async def sync_kbd(self) -> None:
        """Put the keyboard slider where the bound control of the selection is."""
        key = KBD_BINDING.get(self.settings["mode"])
        if key is None:
            return
        k = value_to_kbd(key, self.selected_profile()[key])
        if k != self.settings["slider"]:
            self.settings["slider"] = k
            self.save()
            if self.desktop.slider_ready:
                await self.desktop.push_slider(k)

    def later(self, coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    def set_value(self, key: str, value, from_kbd: bool = False, w: int = 1) -> None:
        """Validate and store one setting: profile keys on the profile of
        widget w's selection, the rest globally."""
        if key == "widgets":
            self.set_widget_count(value)
            return
        if key in PROFILE_KEYS:
            name = self.selected_profile_name(w)
            profile = self.profiles[name]
            profile[key] = check_profile_value(profile, key, value)
            affected = self.using(name)
        elif key == "flick":
            self.set_flick(value)
            affected = self.active_bulbs()
        elif key in SETTABLE_GLOBALS:
            self.settings[key] = check_global_value(key, value)
            affected = self.active_bulbs()
        else:
            raise ValueError(f"unknown setting {key}")
        self.save()
        for b in affected:
            b.paused, b.sent = False, None
        if not from_kbd and (key == "mode" or key == KBD_BINDING.get(self.settings["mode"])):
            self.later(self.sync_kbd())  # moves the LED only if widget 1's bound value changed
        self.wake.set()

    def set_flick(self, value) -> None:
        """off | on | fast | exclusive. fast and exclusive need the root setup:
        missing or outdated, it runs now (pkexec), and should it fail or be
        cancelled the mode goes back to the one before."""
        try:
            mode = check_global_value("flick", value)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        before = self.settings["flick"]
        self.settings["flick"] = mode
        self.setup_note = ""
        log.info("flick mode %s", mode)
        if mode == "exclusive":
            log.info("flick mode exclusive is not implemented yet: it works like fast")
        if mode in FAST_FLICK and rootsetup.status()["status"] != "current":
            self.start_setup(revert_to=before, mode=mode)

    def setup_running(self) -> bool:
        return self.setup_task is not None and not self.setup_task.done()

    def start_setup(self, revert_to: str | None = None, mode: str | None = None) -> None:
        """Run install/setup-root.sh via pkexec (a graphical polkit prompt) in
        the background; Bluetooth restarts when its tuning changes."""
        if self.setup_running():
            return
        self.setup_note = ""
        self.setup_task = asyncio.get_running_loop().create_task(self.run_setup(revert_to, mode))

    async def run_setup(self, revert_to: str | None, mode: str | None) -> None:
        cmd = rootsetup.command(tty=False)
        log.info("system setup: %s (asks for the administrator password; Bluetooth restarts)", " ".join(cmd))
        try:
            rc, last = await rootsetup.run(cmd)
        except Exception as exc:
            rc, last = -1, str(exc)
        st = rootsetup.status()
        if rc == 0 and st["status"] == "current":
            log.info("system setup v%s installed", st["installed"])
        else:
            why = {126: _("cancelled"), 127: _("not allowed, or no password prompt available")}.get(
                rc, _("failed ({last})").format(last=last or rc))
            if mode is not None and revert_to is not None and self.settings["flick"] == mode:
                self.settings["flick"] = revert_to
                self.save()
                for b in self.active_bulbs():
                    b.sent = None
                self.setup_note = _("System setup {why}: wall-switch flick back to {mode}.").format(
                    why=why, mode=revert_to)
            else:
                self.setup_note = _("System setup {why}.").format(why=why)
            log.warning("system setup: exit %s, %s; %s", rc, last or "-", self.setup_note)
        self.wake.set()

    def setup_state(self) -> dict:
        try:
            st = rootsetup.status()
        except (OSError, ValueError) as exc:
            st = {"status": "missing", "installed": None, "required": None, "error": str(exc)}
        return {**st, "running": self.setup_running(), "note": self.setup_note}

    def note_manual(self) -> None:
        """Keep each bulb's manual level in its entry (written MANUAL_SAVE_S
        after the last change), so a restart does not fall back to the profile."""
        changed = False
        for b in self.bulbs.values():
            kept = {"level": b.manual, "exact": b.manual_exact} if b.paused else None
            if b.entry.get("manual") != kept:
                if kept is None:
                    b.entry.pop("manual", None)
                else:
                    b.entry["manual"] = kept
                changed = True
        if changed and (self.manual_saver is None or self.manual_saver.done()):
            self.manual_saver = asyncio.get_running_loop().create_task(self._save_manual())

    async def _save_manual(self) -> None:
        await asyncio.sleep(MANUAL_SAVE_S)
        self.save()

    def set_widget_count(self, value) -> None:
        """How many widgets; lowering it drops the higher widgets' bulb lists
        and selections (driving does not change: membership never decides it)."""
        try:
            n = check_widget_count(value)
        except (TypeError, ValueError):
            raise UserError(_("widgets must be a number from 1 to 8")) from None
        self.settings["widgets"] = n
        self.clean_selection()
        self.save()
        log.info("widgets: %d", n)
        if self.kde is not None:
            self.kde.widgets_changed()

    def set_widget_bulbs(self, w: int, ids: list[str]) -> None:
        """Widget w's bulbs ("" or none: no bulb), kept in list order. Unused
        bulbs the widget had stay in it (the widget does not show them)."""
        chosen = {self.find(i).id for i in ",".join(ids).replace(" ", "").split(",") if i}
        kept = {bid for bid in self.widget_list(w) if bid in self.bulbs and not self.active(self.bulbs[bid])}
        self.settings["widget_bulbs"][str(w)] = [bid for bid in self.bulbs if bid in chosen | kept]
        self.clean_selection()
        self.save()
        log.info("widget %d: %s", w, ", ".join(self.widget_list(w)) or "-")

    def nudge(self, step: float, w: int = 1) -> None:
        """Scroll on the tray icon: move the keyboard slider by step (or, in
        mode off, the brightness of the selection). No device traffic here:
        the workers send the latest value, and only the latest push runs.
        Widgets other than 1 move the same bound value of their own selection,
        in slider steps, without the slider."""
        if not math.isfinite(step):
            raise ValueError("nudge needs a number")
        if self.settings["mode"] != "off" and w != 1:
            key = KBD_BINDING[self.settings["mode"]]
            k = value_to_kbd(key, self.selected_profile(w)[key])
            value = max(0, min(KbdSlider.MAX, round(k + step)))
            if value != k:
                self.set_value(key, kbd_to_value(key, value), w=w)
            return
        if self.settings["mode"] != "off":
            value = max(0, min(KbdSlider.MAX, round(self.settings["slider"] + step)))
            if value != self.settings["slider"]:
                self.apply_slider(value)  # as a real slider move, minus the echo check
                if self.desktop.slider_ready:
                    self.push_kbd(value)
            return
        self.nudge_level(step, w)

    def nudge_level(self, step: float, w: int = 1) -> float | None:
        """The brightness of widget w's selection by step: the profile's level
        while it rules, else a manual level per bulb. Returns the new level
        (the highest of the selection), None without bulbs."""
        profile = self.selected_profile(w)
        if not self.tracking(profile):  # the level rules (mode off is never independent)
            self.set_value("level", float(profile["level"]) + step, w=w)
            return float(self.selected_profile(w)["level"])
        levels = []
        for b in self.selected(w):
            if b.paused and b.manual is not None:
                base = b.manual
            else:
                base = b.sent if b.sent is not None else (self.want(b) or 0.0)
            b.manual, b.manual_exact, b.paused = max(0.0, min(100.0, base + step)), False, True
            levels.append(b.manual)
        self.wake.set()
        return max(levels) if levels else None

    def push_kbd(self, value: int) -> None:
        """Show value on Plasma's keyboard slider; one push at a time, latest wins."""
        self.kbd_want = value
        if self.kbd_pusher is None or self.kbd_pusher.done():
            self.kbd_pusher = asyncio.get_running_loop().create_task(self._push_kbd())

    async def _push_kbd(self) -> None:
        while True:
            value = self.kbd_want
            self.kbd_echo[value] = time.monotonic()
            await self.desktop.push_slider(value)
            if self.kbd_want == value:
                return

    def manual(self, bulbs: list[Bulb], percent: float, exact: bool) -> None:
        for b in bulbs:
            b.paused, b.manual, b.manual_exact, b.sent = True, float(percent), exact, None
        self.wake.set()

    def select(self, target: str, w: int = 1) -> None:
        """What widget w's controls act on: `all` or one of its used bulbs."""
        target = "all" if target == "all" else self.find(target).id
        if target != "all" and self.bulbs[target] not in self.in_widget(w):
            raise UserError(_("{bid} is not in widget {w}").format(bid=target, w=w))
        self.settings["selected"][str(w)] = target
        self.save()
        if w == 1:  # the keyboard slider is bound to widget 1
            self.later(self.sync_kbd())

    def profile_command(self, action: str, names: list[str], w: int = 1) -> None:
        """save NAME | apply NAME | rename OLD NEW | delete NAME (also as `preset`),
        on widget w's selection."""
        if action == "rename":
            if len(names) != 2:
                raise UserError("profile rename OLD NEW")
            old, new = names
        else:
            old = new = " ".join(names).strip()
        if not new:
            raise UserError(f"profile {action} needs a name")
        if action == "save":
            self.profiles[new] = dict(self.selected_profile(w))
        elif action == "delete":
            if new == DEFAULT:
                raise UserError(_("the default profile cannot be deleted or renamed"))
            if self.profiles.pop(new, None) is not None:
                for b in [b for b in self.bulbs.values() if b.profile == new]:
                    b.entry["profile"] = DEFAULT
                    b.paused, b.sent = False, None
        elif action == "rename":
            if DEFAULT in (old, new):
                raise UserError(_("the default profile cannot be deleted or renamed"))
            if old not in self.profiles:
                raise UserError(_("unknown profile {name}").format(name=old))
            if new in self.profiles:
                raise UserError(_("a profile named {name} exists already").format(name=new))
            self.profiles[new] = self.profiles.pop(old)
            for b in self.bulbs.values():
                if b.profile == old:
                    b.entry["profile"] = new
        elif action in ("apply", "use"):
            if new not in self.profiles:
                raise UserError(_("unknown profile {name}").format(name=new))
            if self.selection(w) == "all":
                # "all" works on the default profile: it takes the values, every bulb of the widget uses it
                if new != DEFAULT:
                    self.profiles[DEFAULT] = dict(self.profiles[new])
                for b in self.in_widget(w):
                    b.entry["profile"] = DEFAULT
            else:
                self.bulbs[self.selection(w)].entry["profile"] = new
            for b in self.bulbs.values():
                b.paused, b.sent = False, None
        else:
            raise UserError("profile action must be save, apply, rename or delete")
        self.save()
        self.later(self.sync_kbd())
        self.wake.set()

    # ------------------------------------------------------------------ bulbs

    async def add_bulb(self, kind: str, given: dict) -> str:
        cls = registry.backend_class(kind)
        name = given.pop("name", "").strip()
        config = await self.instance(kind).add(registry.check_fields(cls, given))
        suffix = str(config.pop("id", "") or "")
        if not suffix:
            n = 1
            while f"{kind}:{n}" in self.bulbs:
                n += 1
            suffix = str(n)
        bid = f"{kind}:{suffix}"
        if bid in self.bulbs:
            raise UserError(_("{bid} is added already").format(bid=bid))
        entry = {"kind": kind, "name": name or f"{cls.title} {suffix}", "profile": DEFAULT,
                 "power": True, "use": True, "icon_on": "", "icon_off": "", "config": config}
        self.state["bulbs"][bid] = entry
        self.bulbs[bid] = Bulb(bid, entry)
        self.widget_list(1).append(bid)  # new bulbs join widget 1
        self.save()
        log.info("added %s (%s)", bid, entry["name"])
        if kind in self.started:
            self.bulbs[bid].backend = self.backends[kind]
        else:
            await self.start_backend(kind)
        self.scan_status()
        self.wake.set()
        return bid

    async def remove_bulb(self, bid: str) -> None:
        bulb = self.find(bid)
        self.bulbs.pop(bulb.id)
        self.state["bulbs"].pop(bulb.id, None)
        self.intended.pop(bulb.id, None)
        self.clean_selection()
        self.save()
        if bulb.backend is not None:
            with contextlib.suppress(Exception):
                await bulb.backend.remove(bulb)
        log.info("removed %s", bulb.id)

    def bulb_set(self, bid: str, values: dict) -> None:
        bulb = self.find(bid)
        was_active = self.active(bulb)
        for key, value in values.items():
            if key == "name":
                if not value.strip():
                    raise UserError("name must not be empty")
                bulb.entry["name"] = value.strip()
            elif key == "profile":
                if value not in self.profiles:
                    raise UserError(_("unknown profile {name}").format(name=value))
                bulb.entry["profile"] = value
                bulb.paused, bulb.sent = False, None
            elif key == "use":
                try:
                    bulb.entry["use"] = parse_bool(value)
                except ValueError:
                    raise UserError(_("use must be on or off")) from None
            elif key in ("icon_on", "icon_off"):
                try:
                    bulb.entry[key] = check_icon(value)
                except ValueError:
                    raise UserError(_("an icon is an icon name or an absolute file path")) from None
            else:
                raise UserError(f"bulb-set takes name=, profile=, use=, icon_on= and icon_off=, not {key}")
        if self.active(bulb) != was_active:
            bulb.reset()  # driven again: sent afresh
            bulb.arm_retry_at = 0.0
            log.info("%s: %s", bulb.id, "in use again" if self.active(bulb)
                     else "not in use: OnPowerUp on, then left alone")
        self.clean_selection()
        self.save()
        if self.selection(1) == bulb.id:
            self.later(self.sync_kbd())
        self.wake.set()

    def order(self, ids: list[str]) -> None:
        """The user's list order: a permutation of every bulb id."""
        new = [self.find(i).id for i in ids]
        if len(new) != len(self.bulbs) or set(new) != set(self.bulbs):
            raise UserError(_("order needs every bulb id once: {known}").format(known=" ".join(self.bulbs)))
        for d in (self.state["bulbs"], self.bulbs):  # in place: backends may hold the dicts
            items = {bid: d.pop(bid) for bid in new}
            d.update(items)
        self.clean_selection()  # the widget lists follow the order
        self.save()

    async def discover(self, kind: str | None) -> list[dict]:
        kinds = [kind] if kind else [k for k, info in registry.registry().items() if info.available]
        results = await asyncio.gather(*(self.instance(k).discover(DISCOVER_S) for k in kinds),
                                       return_exceptions=True)
        found = []
        for k, res in zip(kinds, results):
            if isinstance(res, Exception):
                log.warning("discover %s: %s", k, res)
                continue
            found += res
        return found

    # ------------------------------------------------------------------ get

    def describe_bulb(self, b: Bulb) -> dict:
        info = registry.registry().get(b.kind)
        return {"id": b.id, "name": b.name, "kind": b.kind, "profile": b.profile, "power": b.power,
                "output": b.sent, "connected": b.connected, "online": b.online,
                "caps": sorted(self.caps(b)), "experimental": info.experimental if info else True,
                "following": not b.paused, "powerup": b.armed, "use": b.use,
                "icon_on": b.entry.get("icon_on", ""),
                "icon_off": b.entry.get("icon_off", "")}

    def describe(self, w: int = 1) -> dict:
        """The get object: the top-level fields describe widget w's selection."""
        sel = self.selected(w)
        name = self.selected_profile_name(w)
        profile = self.profiles[name]
        outputs = [b.sent for b in sel if b.sent is not None]
        onlines = [b.online for b in sel]
        armed = {b.armed for b in sel if "powerup" in self.caps(b)}
        ages = [b.extra.get("advert_age") for b in sel if b.extra.get("advert_age") is not None]
        env = self.desktop.env
        return {
            "bulb": "sync", "following": not any(b.paused for b in sel),
            "mode": self.settings["mode"], "slider": self.settings["slider"],
            **{k: profile[k] for k in PROFILE_KEYS},
            "lid": self.settings["lid"],
            "night": env.night, "day": env.day, "color_scheme": env.scheme_name,
            "screen_kelvin": getattr(env, "screen_kelvin", None),
            "tracking": self.tracking(profile),
            "screen_on": self.desktop.screen_on, "lid_closed": self.lid_closed,
            "power": any(b.power for b in sel), "bulbs": max(outputs) if outputs else None,
            "connected": bool(sel) and all(b.connected for b in sel),
            "online": False if False in onlines else None if (None in onlines or not sel) else True,
            "onpowerup": POWERUP_INT.get(armed.pop()) if len(armed) == 1 else None,
            "advert_age": max(ages) if ages else None,
            "kbd_binds": KBD_BINDING.get(self.settings["mode"]),
            "screen": round(self.desktop.screen_level(), 1),
            "presets": sorted(n for n in self.profiles if n != DEFAULT),
            "selected": self.selection(w), "flick": self.settings["flick"], "setup": self.setup_state(),
            "profile": name,
            "widget": w, "widgets": self.widget_count,
            "in_widget": [b.id for b in self.in_widget(w)],
            "bulbs_list": [self.describe_bulb(b) for b in self.bulbs.values()],
            "profiles": sorted(self.profiles, key=lambda n: (n != DEFAULT, n.lower())),
            "backends": registry.describe(),
        }

    # ------------------------------------------------------------------ watch

    def intended_of(self, b: Bulb) -> dict:
        """What the core wants a bulb to show; percent None: not driven (idle,
        or paused with no level yet: then what it holds). color: the colour
        want plus "rgb" for display, None when the colour is left alone."""
        if not self.active(b):
            return {"power": b.power, "percent": None, "color": None}
        want = self.want(b)
        color = self.color(b)
        if color is not None:
            color = {**color, "rgb": color_rgb(color)}
        return {"power": b.power, "percent": want if want is not None else b.sent, "color": color}

    def note_intended(self) -> None:
        """Report every bulb whose intended state changed since the last report."""
        if not self.watchers:
            self.intended.clear()
            return
        for b in list(self.bulbs.values()):
            now = self.intended_of(b)
            if self.intended.get(b.id) != now:
                self.intended[b.id] = now
                self.emit({"event": "intended", "t": time.time(), "bulb": b.id, **now})

    def emit_event(self, event: str, bulb: Bulb, **detail) -> None:
        if self.watchers:
            self.emit({"event": event, "t": time.time(), "bulb": bulb.id, **detail})

    def emit(self, obj: dict) -> None:
        line = json.dumps(obj).encode() + b"\n"
        for w in list(self.watchers):
            try:
                w.queue.put_nowait(line)
            except asyncio.QueueFull:  # too slow or stuck: never let it hold the daemon up
                self.watchers.discard(w)
                w.writer.transport.abort()
                log.warning("watch client dropped: %d lines behind", WATCH_QUEUE)

    def drop_watchers(self) -> None:
        """Shutdown: end every watch connection, or closing the server would
        wait for them (and a restart would find the lock still held)."""
        for watcher in list(self.watchers):
            self.watchers.discard(watcher)
            with contextlib.suppress(asyncio.QueueFull):
                watcher.queue.put_nowait(b"")
            watcher.writer.transport.abort()

    async def watch(self, w: int, reader, writer) -> None:
        """A watch client: the snapshot, then every event line until it goes."""
        if self.watchers:
            self.note_intended()  # the others get what is pending; the baseline is then current
        else:
            self.intended = {b.id: self.intended_of(b) for b in self.bulbs.values()}
        watcher = Watcher(writer)
        self.watchers.add(watcher)
        snapshot = {"event": "snapshot", "t": time.time(), **self.describe(w),
                    "intended": {bid: dict(v) for bid, v in self.intended.items()}}

        async def gone() -> None:
            with contextlib.suppress(Exception):
                while await reader.read(4096):
                    pass
            self.watchers.discard(watcher)
            with contextlib.suppress(asyncio.QueueFull):
                watcher.queue.put_nowait(b"")  # wakes the loop below

        closed = asyncio.get_running_loop().create_task(gone())
        try:
            writer.write(json.dumps(snapshot).encode() + b"\n")
            await writer.drain()
            while watcher in self.watchers:
                line = await watcher.queue.get()
                if not line:
                    break
                writer.write(line)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.watchers.discard(watcher)
            closed.cancel()
            writer.close()

    # ------------------------------------------------------------------ ctl

    def parse(self, req: dict) -> tuple[int, str, list[str], str | None]:
        """Widget number (a leading `@N` token, default 1), command, arguments, which."""
        cmd = req.get("cmd")
        arg = req.get("arg")
        args = req.get("args")
        if args is None:
            args = str(arg).split() if arg not in (None, "") else []
        args = [str(a) for a in args]
        w = 1
        if isinstance(cmd, str) and cmd.startswith("@"):
            w = self.widget(cmd[1:])
            if not args:
                raise UserError(f"{cmd} COMMAND ...")
            cmd, args = args[0], args[1:]
        return w, cmd, args, req.get("which")

    async def command(self, req: dict) -> dict:
        w, cmd, args, which = self.parse(req)
        get = lambda: {"results": [self.describe(w)]}  # noqa: E731

        if cmd == "get":
            return get()
        if cmd in ("pause", "resume"):
            for b in self.driven(self.targets(which, w)):
                b.paused, b.sent, b.manual, b.manual_exact = cmd == "pause", None, None, False
            self.wake.set()
            return get()
        if cmd in ("mode", "track", "off-at-zero", "lid", "flick"):
            if args:
                self.set_value(cmd.replace("-", "_"), args[0], w=w)
            return get()
        if cmd == "set":
            key, sep, value = " ".join(args).partition("=")
            if not sep:
                raise UserError("set KEY=VALUE")
            self.set_value(key.strip(), value.strip(), w=w)
            return get()
        if cmd in ("preset", "profile"):
            if not args:
                raise UserError(f"{cmd} save|apply|rename|delete NAME")
            self.profile_command(args[0], args[1:], w)
            return get()
        if cmd in ("on", "off", "toggle"):
            bulbs = self.driven(self.targets(which, w))
            on = (not any(b.power for b in bulbs)) if cmd == "toggle" else cmd == "on"
            self.set_power(bulbs, on, cmd)
            return get()
        if cmd == "nudge":
            self.nudge(float(args[0]), w)
            return get()
        if cmd == "bulb":
            # The widget's brightness slider: the level while it rules,
            # otherwise a manual setting until `resume`.
            value = max(0.0, min(100.0, float(args[0])))
            if self.settings["mode"] == "independent" or not self.tracking(self.selected_profile(w)):
                self.set_value("level", value, w=w)
            else:
                self.manual(self.selected(w), value, exact=False)
            return get()
        if cmd == "slider":
            value = max(0, min(KbdSlider.MAX, round(float(args[0]))))
            if not (self.desktop.slider_ready and await self.desktop.push_slider(value)):
                self.slider_moved(value)
            return {"results": [{**self.describe(w), "slider": value}]}
        if cmd == "dim":
            bulbs = self.driven(self.targets(which, w))
            self.manual(bulbs, float(args[0]), exact=True)
            log.info("manual dim %s: following paused", args[0])
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and any(b.sent is None and b.online is not False for b in bulbs):
                await asyncio.sleep(0.1)
            return {"results": [{"bulb": b.id, "percent": b.sent} for b in bulbs]}
        if cmd == "status":
            results = []
            for b in self.targets(which, w):
                got = None
                if b.backend is not None and b.kind in self.locks:
                    async with self.locks[b.kind]:
                        with contextlib.suppress(Offline):
                            got = await b.backend.query(b)
                results.append({**(got or {"on": b.power and bool(b.sent), "percent": b.sent}), "bulb": b.id})
            return {"results": results}
        if cmd == "raw":
            results = []
            for b in self.targets(which, w):
                raw = getattr(b.backend, "raw", None)
                if raw is None:
                    raise UserError(f"{b.id}: raw is for mesh bulbs")
                async with self.locks[b.kind]:
                    results += await raw(b, args[0])
            return {"results": results}
        if cmd == "select":
            self.select(args[0] if args else "all", w)
            return get()
        if cmd == "order":
            self.order(args)
            return get()
        if cmd == "widget-bulbs":
            self.set_widget_bulbs(w, args)
            return get()
        if cmd == "bulb-set":
            if len(args) < 2:
                raise UserError("bulb-set ID name=...|profile=...|use=on|off|icon_on=...|icon_off=...")
            self.bulb_set(args[0], _kv(args[1:]))
            return get()
        if cmd == "discover":
            return {"results": [{"discovered": await self.discover(args[0] if args else None)}]}
        if cmd == "add":
            if not args:
                raise UserError("add KIND key=value ...")
            bid = await self.add_bulb(args[0], _kv(args[1:]))
            return {"results": [{**self.describe(w), "added": bid}]}
        if cmd == "remove":
            if not args:
                raise UserError("remove ID")
            await self.remove_bulb(args[0])
            return get()
        if cmd == "backends":
            return {"results": [{"backends": registry.describe()}]}
        if cmd == "setup-root":
            self.start_setup()
            return get()
        if cmd in ("mock-flick", "mock-return", "mock-offline"):
            bulb = self.find(args[0]) if args else None
            if bulb is None or bulb.kind != "mock" or bulb.backend is None:
                raise UserError(f"{cmd} ID: a mock bulb")
            if cmd == "mock-flick":
                bulb.backend.simulate_flick(bulb)
            elif cmd == "mock-offline":
                bulb.backend.simulate_offline(bulb)
            else:
                gap = args[1] if len(args) > 1 else "none"
                bulb.backend.simulate_return(bulb, None if gap == "none" else float(gap))
            return get()
        raise UserError(f"unknown command {cmd}")

    async def handle_client(self, reader, writer) -> None:
        try:
            req = json.loads(await reader.readline())
            w, cmd, _args, _which = self.parse(req)
            if cmd == "watch":
                await self.watch(w, reader, writer)  # holds the connection until the client goes
                return
            reply = await self.command(req)
        except (UserError, SystemExit) as exc:
            reply = {"error": str(exc)}
        except Exception as exc:  # report, keep serving
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()

    # ------------------------------------------------------------------ run

    async def follow(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), self.interval)
            self.wake.clear()
            if self.sleeping:
                continue
            self.check_lid()
            self.scan_status()
            self.note_intended()  # before the workers send it
            self.note_manual()
            for event in self.events.values():
                event.set()

    async def start(self) -> None:
        """Backends with bulbs, then the desktop (for tests: without the socket)."""
        self.save()  # migrated, now under the lock
        for kind in sorted({b.kind for b in self.bulbs.values()}):
            await self.start_backend(kind)
        self.scan_status()
        key = KBD_BINDING.get(self.settings["mode"])
        if key is not None:  # start where the bound control stands
            self.settings["slider"] = value_to_kbd(key, self.selected_profile()[key])
        await self.desktop.start(self.settings["slider"])
        if self.settings["flick"] == "exclusive":
            log.info("flick mode exclusive is not implemented yet: it works like fast")
        if self.kde is not None:
            try:
                await self.kde.start()
            except Exception as exc:  # never fatal
                log.warning("KDE integration: %s", exc)

    async def stop(self) -> None:
        await self.shutdown_arm()  # while the scanners still feed a reconnect
        self.note_manual()
        if self.manual_saver is not None:
            self.manual_saver.cancel()
        self.save()  # the manual levels, written now rather than after the debounce
        for task in self.workers.values():
            task.cancel()
        for kind in list(self.started):
            with contextlib.suppress(Exception):
                await self.backends[kind].stop()
        self.desktop.close()
        if self.kde is not None:
            self.kde.close()

    async def run(self) -> int:
        with link_lock(self.state_path):
            with contextlib.suppress(FileNotFoundError):
                self.sock_path.unlink()
            server = await asyncio.start_unix_server(self.handle_client, path=str(self.sock_path))
            os.chmod(self.sock_path, 0o600)
            log.info("following %s, mode %s, bulbs %s",
                     getattr(self.desktop.backlight, "name", "-"), self.settings["mode"],
                     ", ".join(f"{b.id} {'on' if b.power else 'off'}" for b in self.bulbs.values()) or "none")
            main = asyncio.current_task()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, main.cancel)
            try:
                await self.start()
                async with server:
                    try:
                        await self.follow()
                    finally:
                        self.drop_watchers()  # before the server waits for its connections
            except asyncio.CancelledError:
                log.info("stopping")
            finally:
                await self.stop()
                with contextlib.suppress(FileNotFoundError):
                    self.sock_path.unlink()
        return 0

