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

from . import actions as actionsmod
from . import audiofollow as audiomod
from . import backends as registry
from . import clap as clapmod
from . import followrate as ratemod
from . import hci
from . import notifywatch
from . import rootsetup
from . import videofollow as videomod
from .backends import QUICK_GAP_S, Offline
from .common import UserError, _, armed_path, link_lock, read_armed, read_state, save_state, write_armed
from .desktop import Desktop, KbdSlider
from .kde import Kde, panel_disabled
from .profiles import (
    AUDIO_KEYS, CLAP_KEYS, DEFAULT, FOLLOW_KEYS, VIDEO_KEYS, FAST_FLICK, KBD_BINDING, PROFILE_KEYS, SETTABLE_GLOBALS, check_global_value, check_icon,
    flick_toggles,
    SCHEMA, check_profile_value, check_widget_count, clean_selected, clean_widget_bulbs, clean_widget_clap, color_moved,
    color_rgb, color_want, compute_want, is_tracking, kbd_to_value, mesh_product, migrate, new_state, parse_bool, powerup_target, value_to_kbd,
    NOTIFY_RULES, check_notify_rules, notify_match,
)

log = logging.getLogger("hauslicht")

# QUICK_GAP_S (haus/backends): back within this after the drop: a flick; longer: a power-off, back lit
ARM_RETRY_S = 60.0  # after an OnPowerUp Get/Set got no answer on a link that stays up (a new link: at once)
NUDGE_ECHO_S = 3.0  # a keyboard-slider echo of a nudge's own push arrives within this
SEND_STEP = 0.25  # resend only when the want moved this much
BACKOFF_S, BACKOFF_MAX_S = 5.0, 300.0
SHUTDOWN_ARM_S = 8.0
DISCOVER_S = 5.0
AUTOCONNECT_S = 300.0  # settings.autoconnect: look for new devices this often (and at once when switched on)
POWERUP_INT = {"off": 0, "on": 1, "restore": 2}  # get's `onpowerup` keeps the old numbers
WATCH_QUEUE = 1000  # lines a watcher may lag behind before it is dropped
MONITOR_POLL_S = 1.0  # desktops: the DRM connectors (cheap sysfs reads)
MONITOR_GRACE_S = 15.0  # after a resume the DP links come back: no monitor events
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
        self.link_epoch = None  # the backend's link epoch the armed/colour state belongs to (scan_status)
        self.connected = False
        self.online: bool | None = None
        self.power_before_loss = self.power
        self.extra: dict = {}
        self.follow_at = -math.inf  # follow audio/video: when its last update went out (the rate cap; never reset)
        self.follow_retry: asyncio.TimerHandle | None = None  # the worker's wake once the cap allows a send

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

    @property
    def audio(self) -> bool:
        """Switched to follow audio (kept in its entry across restarts)."""
        return self.entry.get("audio") is True

    @property
    def video(self) -> bool:
        """Switched to follow video (kept in its entry across restarts)."""
        return self.entry.get("video") is True

    @property
    def video_region(self) -> str:
        return self.entry.get("video_region") or videomod.DEFAULT_REGION

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


def cancel_count() -> int:
    """The current task's cancelling(): a CancelledError while it stays at
    the count taken before is a stray one (a request cancelled under us),
    not this task's own cancel."""
    task = asyncio.current_task()
    return task.cancelling() if task is not None else 0


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
                 desktop_writes: bool = True, desktop=None, kde=None, notifier=None) -> None:
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
        self.monitor_grace_until = 0.0  # monotonic; monitor changes before it are the resume's
        self.monitor_watcher: asyncio.Task | None = None
        # bulb id -> OnPowerUp possibly armed to other than "on" (note_armed);
        # kept from an earlier run until a confirmed "on" clears the bulb
        self.armed_marker: dict[str, str] = read_armed(state_path)
        self.kbd_echo: dict[int, float] = {}  # keyboard-slider values a nudge pushed, and when
        self.kbd_want: int | None = None
        self.kbd_pusher: asyncio.Task | None = None
        self.idle_kinds: set[str] = set()  # kinds whose bulbs are all idle (told so)
        self.watchers: set[Watcher] = set()
        self.intended: dict[str, dict] = {}  # bulb id -> last reported intended state
        self.actions: dict[str, actionsmod.Action] = {}  # bulb id -> its running Identify or Test
        self.test_results: dict[str, dict] = {}  # bulb id -> its last Test ({step, rate, running})
        # notification blinks: the session-bus watcher (tests inject theirs, or none), the last match
        self.notifier = notifier if notifier is not None else (
            notifywatch.NotifyWatcher(self.on_notification) if desktop is None else None)
        self.notify_last: dict | None = None  # {rule, app, time}: never the notification's text
        self.autoconnect_wake = asyncio.Event()
        self.autoconnector: asyncio.Task | None = None
        self.auto_seen: set[tuple[str, str]] = set()  # (kind, address) tried by autoconnect this run
        self.clap = clapmod.ClapService(self.on_clap)  # settings.clap; runs from start()
        # follow audio: captures only while a bulb follows it (apply_audio)
        self.audio = audiomod.AudioFollowService(self.on_audio_block, self.on_audio_failure)
        self.audio_messages: dict[str, int] = {}  # kind -> messages follow-audio sent this run
        self.audio_wake_at = 0.0
        # follow video: a screen cast only while a bulb follows it (apply_video)
        self.video = videomod.VideoFollowService(self.on_video_frame, self.on_video_failure, self.on_video_token)
        self.video.set_smooth(self.settings["video_smooth"])
        self.video_messages: dict[str, int] = {}  # kind -> messages follow-video sent this run
        self.video_wake_at = 0.0
        self.video_hint = ""  # why follow-video went off at start (no usable permission)
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
        """What a bulb should show now; None while a manual command rules.
        Following audio, the audio level instead of the screen."""
        if not paused and self.audio_following(bulb):
            return self.audio_want(bulb) if (bulb.power if power is None else power) else 0.0
        if not paused and self.video_following(bulb):
            return self.video_want(bulb) if (bulb.power if power is None else power) else 0.0
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
        if bulb.online is False or bulb.id not in self.bulbs or bulb.id in self.actions:
            return  # an Identify or Test has the bulb: nothing in between (actions.py)
        if not self.active(bulb):
            if "powerup" in self.caps(bulb) and bulb.armed != "on" and time.monotonic() >= bulb.arm_retry_at:
                await self.arm(backend, bulb, "on")
            return
        want = self.want(bulb)
        if bulb.power and self.audio_following(bulb):
            await self.serve_follow(backend, bulb, want, self.audio_color(bulb), self.audio_messages, "audio")
        elif bulb.power and self.video_following(bulb):
            await self.serve_follow(backend, bulb, want, self.video_color(bulb), self.video_messages, "video")
        else:
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

    async def serve_follow(self, backend, bulb: Bulb, want: float, color: dict | None,
                           counter: dict[str, int], what: str) -> None:
        """Follow audio or video, a lit bulb: one update (level, colour) at
        most every ratemod.interval(follow_rate) (never more than MAX_RATE a
        second), and only past the deadband (ratemod.should_send). The level
        goes as a fade over that interval where the backend has a transition
        time (one message), else as apply(). Every message is counted per
        kind in `counter`. color None: the colour is left alone."""
        now = time.monotonic()
        step = ratemod.interval(self.settings["follow_rate"])
        if now - bulb.follow_at < step:
            # a frame or block just under one interval after the last send:
            # wake the worker again when the cap allows it, not a whole
            # frame later (that would pull the rate below follow_rate)
            if bulb.follow_retry is None or bulb.follow_retry.cancelled() or \
                    bulb.follow_retry.when() <= asyncio.get_running_loop().time():
                bulb.follow_retry = asyncio.get_running_loop().call_later(
                    step - (now - bulb.follow_at), self.events[bulb.kind].set)
            return
        if now < bulb.color_retry_at:
            color = None
        send_level = bulb.sent is None or abs(want - bulb.sent) >= SEND_STEP
        send_color = color_moved(color, bulb.sent_color)
        if not (send_level or send_color):
            return
        if not ratemod.should_send(want, bulb.sent, color if send_color else None, bulb.sent_color,
                                   now - bulb.follow_at, True):
            return
        bulb.follow_at = now  # before the send: a failed one counts against the cap too
        if send_level:
            counter[bulb.kind] = counter.get(bulb.kind, 0) + 1
            if not await backend.fade(bulb, want, step):
                await backend.apply(bulb, want)
            bulb.sent = want
            log.debug("%s: %s -> %.1f %%", bulb.id, what, want)
        if send_color:
            counter[bulb.kind] = counter.get(bulb.kind, 0) + 1
            if await backend.apply_color(bulb, color):
                bulb.sent_color = color
            else:
                bulb.color_retry_at = now + ARM_RETRY_S
                log.warning("%s: colour %s not confirmed; retrying in %.0f s", bulb.id, color, ARM_RETRY_S)

    def note_armed(self, bulb: Bulb, target: str | None) -> None:
        """The on-disk marker (common.armed_path) that `hauslicht disarm
        --if-armed` (the unit's ExecStopPost) reads after an unclean exit:
        target None takes the bulb off it. Written only on a change."""
        if self.armed_marker.get(bulb.id) == target:
            return
        if target is None:
            self.armed_marker.pop(bulb.id, None)
        else:
            self.armed_marker[bulb.id] = target
        try:
            write_armed(self.state_path, self.armed_marker)
        except OSError as exc:
            log.warning("%s: arm marker not written: %s", armed_path(self.state_path).name, exc)

    async def set_powerup(self, backend, bulb: Bulb, target: str) -> bool:
        """Every OnPowerUp Set goes through here. A target other than "on" is
        put on the marker before the Set (an unanswered or interrupted Set
        may have reached the bulb); only a confirmed "on" takes it off."""
        if target != "on":
            self.note_armed(bulb, target)
        ok = await backend.set_powerup(bulb, target)
        if ok and target == "on":
            self.note_armed(bulb, None)
        return ok

    async def arm(self, backend, bulb: Bulb, target: str) -> None:
        if await self.set_powerup(backend, bulb, target):
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
            # A backend may say which links can follow a reboot: "link_epoch"
            # moves only then (mesh exclusive, where every silence check opens
            # a session of the same boot: _next_epoch). Without one every new
            # link may follow a reboot the backend cannot tell (GATT, the rest).
            epoch = st.get("link_epoch")
            new_link = connected and (not bulb.connected if epoch is None else epoch != bulb.link_epoch)
            if connected or epoch is None:
                bulb.link_epoch = epoch
            if new_link:
                bulb.armed = None
                bulb.sent_color = None  # a new link: the colour is sent again
                bulb.arm_retry_at = bulb.color_retry_at = 0.0  # a failure on the old link waits no longer
            if online is False and bulb.online is not False:
                self.end_action(bulb.id, "offline")
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
        self.end_action(bid, "flick")  # a power cycle ends Identify / Test: then the new intended state
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
        self.end_action(bid, "flick")
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

    @property
    def monitor_power(self):
        """The desktop's monitor watch; None on a laptop (it follows the lid)."""
        return getattr(self.desktop, "monitor_power", None)

    def check_monitor_power(self, baseline: bool = False) -> None:
        """Desktops: lights off when every monitor that was on is switched off
        (never on standby), back on when one returns, like the lid."""
        watch = self.monitor_power
        if watch is None:
            return
        self.apply_monitor_event(watch.poll(baseline=baseline))

    def apply_monitor_event(self, event) -> None:
        if event is None:
            return
        what, connector = event
        if not self.settings["monitor_power"]:
            log.info("monitor %s switched %s (monitor_power off: no switch)", connector, what)
            return
        self.set_power(self.active_bulbs(), what == "on", f"monitor power: {connector} switched {what}")

    async def watch_monitors(self) -> None:
        """sysfs once a second; DDC/CI at most every DDC_POLL_S, in a thread,
        only while the option is on. A resume takes the state as it is: suspend
        is not a monitor switched off."""
        watch = self.monitor_power
        if watch is None:
            return
        while True:
            await asyncio.sleep(MONITOR_POLL_S)
            if self.sleeping:
                continue
            baseline = time.monotonic() < self.monitor_grace_until
            self.check_monitor_power(baseline)
            if self.settings["monitor_power"] and not baseline and watch.ddc_due():
                try:
                    answers = await asyncio.to_thread(watch.ddc_read)
                except Exception as exc:  # never fatal
                    log.warning("ddcutil: %s", exc)
                    continue
                if not self.sleeping:
                    self.apply_monitor_event(watch.ddc_apply(answers))

    async def before_sleep(self) -> None:
        """Dark while asleep if lid is close/both; OnPowerUp back to "on" always."""
        self.sleeping = True  # the workers must not relight or re-arm meanwhile
        await self.end_actions("suspend")  # Identify / Test give their bulbs back first
        self.check_lid()  # the lid close that sent the laptop to sleep
        dark = self.settings["lid"] in ("close", "both")
        base = cancel_count()
        for kind in list(self.started):
            backend = self.backends[kind]
            async with self.locks[kind]:
                for bulb in self.bulbs_of(kind):
                    if bulb.online is False:
                        continue  # no power at the wall: nothing to switch off
                    try:
                        with contextlib.suppress(Offline):
                            if dark and self.active(bulb):
                                await backend.apply(bulb, 0.0)
                                bulb.sent, bulb.sent_color = 0.0, None
                            if "powerup" in self.caps(bulb) and bulb.armed != "on":
                                ok = await self.set_powerup(backend, bulb, "on")
                                bulb.armed = "on" if ok else None
                                log.info("suspend: %s OnPowerUp on (%s)", bulb.id, "ok" if ok else "no answer")
                    except asyncio.CancelledError:
                        if cancel_count() > base:
                            raise  # this task is cancelled (desktop's wait_for ran out): stop here
                        log.warning("suspend: %s interrupted (a request cancelled under it); next bulb", bulb.id)
            with contextlib.suppress(Exception):
                await backend.suspend()
        log.info("suspend: %s", "bulbs off" if dark else "bulbs left as they are")

    async def after_wake(self) -> None:
        log.info("resumed from suspend")
        self.monitor_grace_until = time.monotonic() + MONITOR_GRACE_S  # links come back: no events
        self.sleeping = False
        for kind in list(self.started):
            with contextlib.suppress(Exception):
                await self.backends[kind].resume()
        for b in self.bulbs.values():
            b.sent, b.sent_color = None, None
        self.wake.set()

    async def shutdown_arm(self) -> None:
        """Daemon stops: OnPowerUp back to "on" (bounded wait; skipped for bulbs
        out of reach or known to be "on" already). Runs after the SIGTERM's
        cancel was caught (run()): cancelling() is 1 already, so only a cancel
        since (the timeout, a second signal) stops it; a CancelledError from a
        request cancelled under it (2026-10-21 16:21:27) is one bulb's failure."""
        base = cancel_count()
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
                            # once more when a link abort cut the Set off: after a clean
                            # stop the unit's ExecStopPost leaves the bulb alone
                            for _attempt in range(2):
                                try:
                                    ok = await self.set_powerup(backend, b, "on")
                                except Offline:
                                    continue
                                except asyncio.CancelledError:
                                    if cancel_count() > base:
                                        raise  # the timeout (turned into TimeoutError below) or a second signal
                                    log.warning("shutdown: %s OnPowerUp on interrupted (a request cancelled under it)",
                                                b.id)
                                    continue
                                b.armed = "on" if ok else None
                                log.info("shutdown: %s OnPowerUp on (%s)", b.id, "ok" if ok else "no answer")
                                break
        except Exception as exc:
            log.warning("shutdown: OnPowerUp not reset: %s", exc)

    # ------------------------------------------------------------------ identify, test

    async def start_action(self, bid: str, kind: str) -> None:
        """Identify or Test one used, reachable bulb (haus/actions.py). One at a
        time per bulb: a Test ends its Identify, an Identify waits for no Test
        (refused); asking for the running one again changes nothing."""
        bulb = self.driven([self.find(bid)])[0]
        if bulb.backend is None or bulb.kind not in self.locks or bulb.online is False or self.sleeping:
            raise UserError(_("{bid} is not reachable").format(bid=bulb.id))
        running = self.actions.get(bulb.id)
        if running is not None:
            if running.kind == kind:
                return
            if kind == "identify" and running.kind == "test":
                raise UserError(_("a test is running on {bid}").format(bid=bulb.id))
        action = actionsmod.Action(self, bulb, kind)
        self.actions[bulb.id] = action  # at once: the worker never gets the bulb in between
        if kind == "test":
            self.test_results.pop(bulb.id, None)
        if running is not None:
            running.end(kind, restore=False)  # the new one gives the bulb back at its end
            with contextlib.suppress(Exception):
                await running.task
        action.task = asyncio.get_running_loop().create_task(action.run())

    # ------------------------------------------------------------------ notification blinks

    def apply_notify(self) -> None:
        """The watcher runs while the global switch is on."""
        if self.notifier is not None:
            self.notifier.apply(bool(self.settings["notify"]))

    def notify_targets(self, rule: dict) -> list[Bulb]:
        return self.selected(1) if rule["bulbs"] == "selection" else self.active_bulbs()

    def is_lit(self, bulb: Bulb) -> bool:
        level = self.want(bulb)
        level = level if level is not None else bulb.sent
        return bulb.power and (level or 0) > 0

    def start_notify(self, bulb: Bulb, rule: dict) -> bool:
        """Run a rule's pattern on one bulb; False when it is skipped: busy
        (a running Identify, Test or Notify: dropped, never queued), dark
        (unless the rule allows it), idle or unreachable."""
        if (bulb.id in self.actions or not self.active(bulb) or bulb.backend is None
                or bulb.kind not in self.locks or bulb.online is False or self.sleeping):
            return False
        lit = self.is_lit(bulb)
        if not lit and not rule["dark"]:
            return False  # a notification at night never lights a dark room
        action = actionsmod.Action(self, bulb, "notify", rule=rule, lit=lit)
        self.actions[bulb.id] = action
        action.task = asyncio.get_running_loop().create_task(action.run())
        return True

    def on_notification(self, app: str, title: str, body: str) -> list[str]:
        """A desktop notification (not inhibited): per bulb the first rule of
        its own profile that matches. Title and body are matched, never kept
        or logged; the app name and the rule's name are."""
        if not self.settings["notify"]:
            return []
        started, matched = [], None
        rules_of: dict[str, dict | None] = {}
        for b in self.active_bulbs():
            name = b.profile if b.profile in self.profiles else DEFAULT
            if name not in rules_of:
                rules_of[name] = notify_match(self.profiles[name].get(NOTIFY_RULES, []), app, title, body)
            rule = rules_of[name]
            if rule is None or b not in self.notify_targets(rule):
                continue
            matched = matched or rule
            if self.start_notify(b, rule):
                started.append(b.id)
        if matched is not None:
            self.notify_last = {"rule": matched["name"], "app": app, "time": time.time()}
            log.info("notification from %s: rule %s, %s", app, matched["name"],
                     ", ".join(started) if started else "no bulb free and lit")
        return started

    def notify_test(self, name: str, w: int = 1) -> list[str]:
        """notify-test RULE: the rule of widget w's profile, now (its bulbs and
        its dark setting; the global switch and Do Not Disturb do not apply)."""
        rules = self.selected_profile(w).get(NOTIFY_RULES, [])
        rule = next((r for r in rules if r["name"] == name), None)
        if rule is None:
            raise UserError(_("no rule named {name} in profile {profile}").format(
                name=name, profile=self.selected_profile_name(w)))
        return [b.id for b in self.notify_targets(rule) if self.start_notify(b, rule)]

    def set_notify_rules(self, rules, w: int = 1) -> None:
        if isinstance(rules, str):
            try:
                rules = json.loads(rules)
            except ValueError as exc:
                raise UserError(_("the rules are not valid JSON ({why})").format(why=exc)) from None
        try:
            rules = check_notify_rules(rules)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        name = self.selected_profile_name(w)
        self.profiles[name] = {**self.profiles[name], NOTIFY_RULES: rules}  # a new dict: saved copies stay apart
        self.save()
        log.info("profile %s: %d notification rules", name, len(rules))

    def notify_status(self) -> dict:
        watcher = self.notifier.describe() if self.notifier is not None else {
            "state": "off", "available": False, "error": "", "inhibited": None}
        return {**watcher, "last": dict(self.notify_last) if self.notify_last else None,
                "running": [bid for bid, a in self.actions.items() if a.kind == "notify"]}

    def end_action(self, bid: str, why: str, restore: bool = True) -> None:
        action = self.actions.get(bid)
        if action is not None:
            action.end(why, restore)

    async def end_actions(self, why: str) -> None:
        """Suspend, daemon stop: end every action, each gives its bulb back
        (at most actions.END_WAIT_S, then cancelled)."""
        tasks = [a.task for a in self.actions.values() if a.task is not None]
        for action in list(self.actions.values()):
            action.end(why)
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=actionsmod.END_WAIT_S)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(BaseException):
                await task

    def action_done(self, action: actionsmod.Action) -> None:
        """Called by the action as it ends (also when cancelled): following resumes."""
        bid = action.bulb.id
        if self.actions.get(bid) is action:
            del self.actions[bid]
        if action.kind == "test":  # kept for the widget until the next test or a restart
            self.test_results[bid] = {"step": "done" if action.step == "done" else "stopped",
                                      "rate": action.rate, "running": False, "why": action.why,
                                      "fade": action.fade}
        self.emit_event(action.kind, action.bulb, running=False, why=action.why,
                        **({"rate": action.rate} if action.kind == "test" else {}))
        self.wake.set()

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
        if key == "audio_rate":  # became the shared follow_rate
            key = "follow_rate"
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
        elif key == "autoconnect":
            self.settings[key] = check_global_value(key, value)
            affected = []
            if self.settings[key]:
                self.autoconnect_wake.set()  # look now, not in five minutes
        elif key == "clap":
            self.set_clap_mode(w, value)
            affected = []
        elif key in CLAP_KEYS:
            self.set_clap(key, value)
            affected = []
        elif key in AUDIO_KEYS:
            try:
                self.settings[key] = check_global_value(key, value)
            except ValueError as exc:
                raise UserError(str(exc)) from None
            log.info("%s %s", key, self.settings[key])
            self.apply_audio()  # a new source restarts the capture
            affected = []
        elif key in (*FOLLOW_KEYS, *VIDEO_KEYS):
            try:
                self.settings[key] = check_global_value(key, value)
            except ValueError as exc:
                raise UserError(str(exc)) from None
            log.info("%s %s", key, self.settings[key])
            self.video.set_smooth(self.settings["video_smooth"])
            affected = []
        elif key == "monitor_power":  # switches nothing by itself; the next change of a monitor does
            try:
                self.settings[key] = check_global_value(key, value)
            except ValueError as exc:
                raise UserError(str(exc)) from None
            log.info("follow monitor power %s", "on" if self.settings[key] else "off")
            affected = []
        elif key == "notify":
            try:
                self.settings[key] = check_global_value(key, value)
            except ValueError as exc:
                raise UserError(str(exc)) from None
            log.info("notification blinks %s", "on" if self.settings[key] else "off")
            self.apply_notify()
            affected = []
        elif key == "exclusive_adapter":  # the mesh backend's watch takes the new one
            try:
                self.settings[key] = check_global_value(key, value)
            except ValueError as exc:
                raise UserError(str(exc)) from None
            log.info("exclusive adapter %s", self.settings[key] or "automatic")
            affected = []
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
        """off | on | fast | exclusive. fast and exclusive need the root setup
        current: the widget runs it first (pkexec in the login session), the
        CLI `hauslicht setup-root` (sudo); here it is only checked, never run.
        flick_last keeps the last mode that is not off (the popup's switch)."""
        try:
            mode = check_global_value("flick", value)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        if mode in FAST_FLICK and rootsetup.status()["status"] != "current":
            raise UserError(_("flick mode {mode} needs the system setup first: hauslicht setup-root").format(
                mode=mode))
        self.settings["flick"] = mode
        if mode != "off":
            self.settings["flick_last"] = mode
        self.setup_note = ""
        log.info("flick mode %s", mode)

    def set_clap(self, key: str, value) -> None:
        """clap_source, clap_sensitivity: global (Configure → Sound)."""
        try:
            value = check_global_value(key, value)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        self.settings[key] = value
        log.info("%s %s", key, value)
        self.apply_clap()

    def set_clap_mode(self, w: int, value) -> None:
        """Widget w's clap mode (off clap snap both, the popup's selector);
        refused while the optional extra is missing."""
        try:
            value = check_global_value("clap", value)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        if value != "off" and clapmod.missing():
            raise UserError(_("clap detection is not installed ({missing}): {cmd}").format(
                missing=", ".join(clapmod.missing()), cmd=clapmod.install_hint()))
        self.settings["widget_clap"][str(w)] = value
        log.info("widget %d: clap %s", w, value)
        self.apply_clap()

    def clap_mode(self, w: int) -> str:
        return self.settings["widget_clap"].get(str(w), "off")

    def apply_clap(self) -> None:
        """Capture runs while any widget has a mode, for what any of them wants."""
        s = self.settings
        mode = clapmod.union_mode(self.clap_mode(w) for w in range(1, self.widget_count + 1))
        self.clap.apply(mode, s["clap_source"], s["clap_sensitivity"])

    def on_clap(self, event: dict) -> None:
        """A double clap or snap: in every widget whose mode it matches, toggle
        that widget's selection like the hotkey. A bulb in two such widgets is
        switched by the first only."""
        if self.sleeping:
            return
        label = event.get("label", "Clap")
        done: set[str] = set()
        for w in range(1, self.widget_count + 1):
            if not clapmod.mode_matches(self.clap_mode(w), label):
                continue
            bulbs = [b for b in self.selected(w) if self.active(b) and b.id not in done]
            if not bulbs:
                continue
            done.update(b.id for b in bulbs)
            on = not any(b.power for b in bulbs)
            self.set_power(bulbs, on, f"double {label.lower()} (widget {w})")
            if self.watchers:
                self.emit({"event": "clap", "t": time.time(), "label": label, "widget": w,
                           "score": event.get("score"), "power": on})

    # ------------------------------------------------------------------ follow audio

    def audio_following(self, bulb: Bulb) -> bool:
        """Its level and colour come from the audio now: switched to it, in
        use, no manual level (manual / dim / the slider end it anyway)."""
        return bulb.audio and self.active(bulb) and not bulb.paused

    def audio_want(self, bulb: Bulb) -> float:
        """The audio level on the profile's brightness range (range_lo …
        range_hi, within 0-100); silence holds the low end; never below the
        lowest lit step (the mode never switches a bulb off)."""
        profile = self.profile_of(bulb)
        level = audiomod.level_percent(self.audio.level(), profile["range_lo"], profile["range_hi"])
        return max(level, self.min_lit(bulb))

    def audio_color(self, bulb: Bulb) -> dict | None:
        """Pitch as colour: hue on a colour bulb, its own Kelvin range on a
        white one, nothing on a plain dimmer; None before any sound (left as
        it is). audio_color off: the profile's colour as usual."""
        if not self.settings["audio_color"]:
            return self.color(bulb)
        tone = self.audio.tone()
        caps = self.caps(bulb)
        if tone is None or bulb.backend is None:
            return None
        if "hs" in caps:
            return {"mode": "hs", "hue": audiomod.tone_hue(tone), "sat": 100.0}
        if "ct" in caps:
            return {"mode": "ct", "kelvin": audiomod.tone_kelvin(tone, bulb.backend.ct_range(bulb))}
        return None

    def apply_audio(self) -> None:
        """The capture runs while some bulb follows audio, and only then."""
        self.audio.apply(any(self.audio_following(b) for b in self.bulbs.values()), self.settings["audio_source"])

    def set_audio(self, bulbs: list[Bulb], on: bool) -> None:
        """Switch bulbs to follow audio (a manual level ends) or back to the
        screen. Refused while numpy or a recorder is missing."""
        if on and audiomod.missing():
            raise UserError(_("follow audio is not installed ({missing}): {cmd}").format(
                missing=", ".join(audiomod.missing()), cmd=clapmod.install_hint()))
        for b in bulbs:
            if on:
                b.entry["audio"] = True
                b.entry.pop("video", None)  # exclusive per bulb
                b.paused, b.manual, b.manual_exact = False, None, False
            else:
                b.entry.pop("audio", None)
            b.sent, b.sent_color = None, None  # sent afresh by whichever source rules now
        self.save()
        log.info("follow audio %s: %s", "on" if on else "off", ", ".join(b.id for b in bulbs) or "-")
        self.apply_audio()
        self.apply_video()
        self.wake.set()

    def end_audio(self, bulbs: list[Bulb]) -> None:
        """The slider, manual / automatic, dim: the bulbs that followed audio stop."""
        following = [b for b in bulbs if b.audio]
        if following:
            self.set_audio(following, False)

    def end_follow(self, bulbs: list[Bulb]) -> None:
        """The slider, manual / automatic, dim: audio and video both end."""
        self.end_audio(bulbs)
        self.end_video(bulbs)

    def on_audio_block(self) -> None:
        """After each chunk of audio: wake the workers when an update may be
        due (at most follow_rate times a second; the follow loop ticks anyway)."""
        now = time.monotonic()
        if now >= self.audio_wake_at:
            self.audio_wake_at = now + ratemod.interval(self.settings["follow_rate"])
            self.wake.set()

    def on_audio_failure(self, error: str) -> None:
        """The capture failed: the mode goes off for every bulb (the screen
        rules again), the error stays in audio_status. No retry loop."""
        following = [b for b in self.bulbs.values() if b.audio]
        for b in following:
            b.entry.pop("audio", None)
            b.sent, b.sent_color = None, None
        if following:
            self.save()
        self.apply_audio()
        if self.watchers:
            self.emit({"event": "audio", "t": time.time(), "on": False, "error": error})
        self.wake.set()

    def audio_status(self) -> dict:
        return {**self.audio.describe(), "messages": sum(self.audio_messages.values()),
                "mesh_messages": self.audio_messages.get("mesh", 0), "by_kind": dict(self.audio_messages),
                "bulbs": [b.id for b in self.bulbs.values() if self.audio_following(b)]}

    # ------------------------------------------------------------------ follow video

    def video_following(self, bulb: Bulb) -> bool:
        """Its level (and colour) come from its region of the screen now:
        switched to it, in use, no manual level."""
        return bulb.video and self.active(bulb) and not bulb.paused

    def video_want(self, bulb: Bulb) -> float:
        """The region's luminance on 0 … video_max, never below the lowest lit
        step (the mode never switches a bulb off); before the first frame the
        lowest lit step too."""
        lum = self.video.luminance(bulb.video_region)
        level = 0.0 if lum is None else videomod.brightness(lum, self.settings["video_max"])
        return max(level, self.min_lit(bulb))

    def video_color(self, bulb: Bulb) -> dict | None:
        """The region's hue and saturation on a colour (hs) bulb; None on the
        others (brightness only) and before the first frame."""
        rgb = self.video.color(bulb.video_region)
        if rgb is None or bulb.backend is None or "hs" not in self.caps(bulb):
            return None
        hue, sat = videomod.hue_sat(rgb)
        return {"mode": "hs", "hue": hue, "sat": sat}

    def apply_video(self) -> None:
        """The screen cast runs while some bulb follows video, and only then."""
        self.video.apply(any(self.video_following(b) for b in self.bulbs.values()), self.settings["video_token"])

    def set_video(self, bulbs: list[Bulb], on: bool) -> None:
        """Switch bulbs to follow video (audio and a manual level end) or back
        to the screen brightness. Refused while GStreamer's parts are missing.
        Without a restore token the portal asks (once: the token is kept)."""
        if on and videomod.missing():
            raise UserError(_("follow video is not installed ({missing}): {cmd}").format(
                missing=", ".join(videomod.missing()), cmd=videomod.install_hint()))
        for b in bulbs:
            if on:
                b.entry["video"] = True
                b.entry.pop("audio", None)  # exclusive per bulb
                b.paused, b.manual, b.manual_exact = False, None, False
            else:
                b.entry.pop("video", None)
            b.sent, b.sent_color = None, None  # sent afresh by whichever source rules now
        if on and bulbs:
            self.video_hint = ""
        self.save()
        log.info("follow video %s: %s", "on" if on else "off", ", ".join(b.id for b in bulbs) or "-")
        self.apply_audio()
        self.apply_video()
        self.wake.set()

    def end_video(self, bulbs: list[Bulb]) -> None:
        following = [b for b in bulbs if b.video]
        if following:
            self.set_video(following, False)

    def set_video_region(self, bulb: Bulb, region) -> None:
        try:
            bulb.entry["video_region"] = videomod.check_region(region)
        except ValueError as exc:
            raise UserError(str(exc)) from None
        self.save()
        self.wake.set()

    async def forget_video(self) -> None:
        """Forget the screen permission: the token here and its grant in the
        permission store. Bulbs that follow video keep doing so until the
        capture stops; the next start asks again."""
        token, self.settings["video_token"] = self.settings["video_token"], ""
        self.save()
        await videomod.forget_token(token)
        log.info("follow video: screen permission forgotten")

    def on_video_frame(self) -> None:
        """After each frame: wake the workers when an update may be due."""
        now = time.monotonic()
        if now >= self.video_wake_at:
            self.video_wake_at = now + ratemod.interval(self.settings["follow_rate"])
            self.wake.set()

    def on_video_token(self, token: str) -> None:
        """The portal's restore token: the next start (and a restart) needs no dialog."""
        self.settings["video_token"] = token
        self.save()

    def on_video_failure(self, error: str) -> None:
        """The cast was refused or ended: the mode goes off for every bulb,
        the error stays in video_status. No retry loop."""
        following = [b for b in self.bulbs.values() if b.video]
        for b in following:
            b.entry.pop("video", None)
            b.sent, b.sent_color = None, None
        if following:
            self.save()
        self.apply_video()
        if self.watchers:
            self.emit({"event": "video", "t": time.time(), "on": False, "error": error})
        self.wake.set()

    async def check_video_permission(self) -> None:
        """At start: bulbs that followed video resume only with a restore
        token the permission store still holds; otherwise they stay off with
        a hint, and no dialog pops up unasked."""
        following = [b for b in self.bulbs.values() if b.video]
        if not following or await videomod.token_usable(self.settings["video_token"]):
            return
        for b in following:
            b.entry.pop("video", None)
        self.video_hint = videomod.NEEDS_PERMISSION
        self.save()
        log.info("follow video: no usable screen permission; off for %s", ", ".join(b.id for b in following))

    def video_status(self) -> dict:
        return {**self.video.describe(), "messages": sum(self.video_messages.values()),
                "mesh_messages": self.video_messages.get("mesh", 0), "by_kind": dict(self.video_messages),
                "bulbs": [b.id for b in self.bulbs.values() if self.video_following(b)],
                "permission": bool(self.settings["video_token"]), "hint": self.video_hint}

    def setup_running(self) -> bool:
        return self.setup_task is not None and not self.setup_task.done()

    def start_setup(self) -> None:
        """ctl setup-root (the CLI without a terminal): pkexec install/setup-root.sh
        in the background. The widget runs pkexec itself, inside the login
        session where the polkit agent is; this daemon is in none."""
        if self.setup_running():
            return
        self.setup_note = ""
        self.setup_task = asyncio.get_running_loop().create_task(self.run_setup())

    async def run_setup(self) -> None:
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
            self.setup_note = _("System setup {why}.").format(why=why)
            log.warning("system setup: exit %s, %s", rc, last or "-")
        self.wake.set()

    def exclusive_state(self) -> dict:
        """Flick mode exclusive as the mesh backend runs it: active, the
        adapter held, and why it works like fast (note)."""
        status = getattr(self.backends.get("mesh"), "exclusive_status", None)
        if status is None or "mesh" not in self.started:
            return {"active": False, "adapter": None, "note": ""}
        try:
            return status()
        except Exception as exc:  # never fail a get
            return {"active": False, "adapter": None, "note": str(exc)}

    async def adapters(self) -> dict:
        """ctl adapters: every Bluetooth controller, the one exclusive takes
        (chosen) and whether that leaves BlueZ without one (others_stop)."""
        mesh = self.backends.get("mesh") if "mesh" in self.started else None
        if mesh is not None and hasattr(mesh, "adapters"):
            adapters = await mesh.adapters()
        else:
            adapters = await hci.list_adapters()
        chosen = hci.choose_adapter(adapters, self.settings["exclusive_adapter"])
        return {"adapters": adapters, "selected": self.settings["exclusive_adapter"],
                "chosen": chosen["name"] if chosen else None,
                "others_stop": hci.sole_or_builtin(adapters, chosen)}

    def setup_state(self) -> dict:
        try:
            st = rootsetup.status()
        except (OSError, ValueError) as exc:
            st = {"status": "missing", "installed": None, "required": None, "error": str(exc)}
        return {**st, "running": self.setup_running(), "note": self.setup_note, "script": str(rootsetup.SCRIPT)}

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
        self.settings["widget_clap"] = clean_widget_clap(self.settings["widget_clap"], n)
        self.apply_clap()
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
        self.end_follow(self.selected(w))  # like the popup's slider
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
        self.end_follow(bulbs)
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

    async def add_bulb(self, kind: str, given: dict, auto_address: str | None = None) -> str:
        """auto_address: added by autoconnect (the discovered address, kept for the ignore list)."""
        cls = registry.backend_class(kind)
        name = given.pop("name", "").strip()
        backend = self.instance(kind)
        config = await backend.add(registry.check_fields(cls, given))
        suffix = str(config.pop("id", "") or "")
        if not suffix:
            n = 1
            while f"{kind}:{n}" in self.bulbs:
                n += 1
            suffix = str(n)
        bid = f"{kind}:{suffix}"
        if bid in self.bulbs:
            raise UserError(_("{bid} is added already").format(bid=bid))
        if auto_address is not None and self.ignored(kind, auto_address, bid):
            raise UserError(f"{bid} is on the autoconnect ignore list")
        entry = {"kind": kind, "name": name or f"{backend.product_name(config)} - {suffix}", "profile": DEFAULT,
                 "power": True, "use": True, "icon_on": "", "icon_off": "", "config": config}
        if auto_address is not None:
            entry["auto_address"] = auto_address
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
        self.end_action(bulb.id, "removed", restore=False)
        self.bulbs.pop(bulb.id)
        self.state["bulbs"].pop(bulb.id, None)
        self.intended.pop(bulb.id, None)
        if "auto_address" in bulb.entry:  # autoconnected: never again by itself
            ignore = self.settings["autoconnect_ignore"]
            item = {"id": bulb.id, "address": str(bulb.entry["auto_address"]), "name": bulb.name}
            if item not in ignore:
                ignore.append(item)
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
            elif key in ("icon_on", "icon_off", "icon_disconnected"):
                try:
                    bulb.entry[key] = check_icon(value)
                except ValueError:
                    raise UserError(_("an icon is an icon name or an absolute file path")) from None
            else:
                raise UserError(f"bulb-set takes name=, profile=, use=, icon_on=, icon_off= and icon_disconnected=, not {key}")
        if not self.active(bulb):
            self.end_action(bulb.id, "not in use")
            bulb.entry.pop("audio", None)
            bulb.entry.pop("video", None)
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

    async def discover(self, kind: str | None) -> tuple[list[dict], dict[str, str], list[str]]:
        """(entries, errors, access). kind None: every available kind; "bluetooth":
        every available kind with Backend.bluetooth. Each entry carries its
        `kind` and `known` (a bulb has its address already); a failing kind
        is an error message in `errors`, the others still count. `access`:
        the kinds whose error is a missing device permission (NeedsAccess),
        which the root setup's udev rule grants."""
        reg = registry.registry()
        if kind == "bluetooth" and "bluetooth" not in reg:
            kinds = [k for k, info in reg.items() if info.available and info.cls.bluetooth]
        else:
            kinds = [kind] if kind else [k for k, info in reg.items() if info.available]
        held = bool(self.exclusive_state().get("active"))

        async def one(k: str) -> list[dict]:
            from .backends._bulbkit import RADIO_HELD, ScanRefused
            RADIO_HELD.set(held)  # this task's own context
            try:
                return await self.instance(k).discover(DISCOVER_S)
            except ScanRefused as exc:
                log.info("discover %s while exclusive: %s", k, exc)
                raise UserError(_("No Bluetooth adapter is free to scan: flick mode Exclusive holds it. "
                                  "Set the flick mode to Fast to search.")) from exc

        results = await asyncio.gather(*(one(k) for k in kinds), return_exceptions=True)
        found, errors, access = [], {}, []
        for k, res in zip(kinds, results):
            if isinstance(res, BaseException):
                if not isinstance(res, Exception):
                    raise res
                log.warning("discover %s: %s", k, res)
                errors[k] = str(res) or type(res).__name__
                if not getattr(res, "access", False):
                    continue
                access.append(k)
                res = res.found  # registry.NeedsAccess: the entries still count
            for dev in res:
                dev = {**dev, "kind": dev.get("kind") or k}
                dev["known"] = bool(dev.get("address")) and self.known(dev["kind"], str(dev["address"]))
                found.append(dev)
        return found, errors, access

    # ------------------------------------------------------------------ autoconnect

    def ignored(self, kind: str, address: str, bid: str | None = None) -> bool:
        for item in self.settings["autoconnect_ignore"]:
            if bid is not None and item["id"] == bid:
                return True
            if item["address"] and item["address"].lower() == address.lower() \
                    and item["id"].partition(":")[0] in (kind, ""):
                return True
        return False

    def known(self, kind: str, address: str) -> bool:
        """A bulb of this kind already has the discovered address (in its config or as autoconnected)."""
        a = address.lower()
        return any(b.kind == kind and (str(b.entry.get("auto_address", "")).lower() == a
                                       or any(str(v).lower() == a for v in b.config.values()))
                   for b in self.bulbs.values())

    def unignore(self, target: str) -> None:
        ignore = self.settings["autoconnect_ignore"]
        keep = [i for i in ignore if target not in (i["id"], i["address"])]
        if len(keep) == len(ignore):
            raise UserError(_("{target} is not on the ignore list").format(target=target))
        for item in ignore:
            if item not in keep:
                self.auto_seen.discard((item["id"].partition(":")[0], item["address"]))
        self.settings["autoconnect_ignore"] = keep
        self.save()
        if self.settings["autoconnect"]:
            self.autoconnect_wake.set()

    async def autoconnect_round(self) -> list[str]:
        """Discover every kind that allows it (never mesh: adopting changes the
        device) and add what is new, in use; returns the added ids."""
        kinds = [k for k, info in registry.registry().items() if info.available and info.cls.autoconnect]
        results = await asyncio.gather(*(self.instance(k).discover(DISCOVER_S) for k in kinds),
                                       return_exceptions=True)
        added = []
        for kind, res in zip(kinds, results):
            if isinstance(res, Exception):
                log.info("autoconnect: discover %s: %s", kind, res)
                continue
            for dev in res:
                address = str(dev.get("address") or "")
                if dev.get("kind", kind) != kind or not address:
                    continue
                if (kind, address) in self.auto_seen or self.ignored(kind, address) or self.known(kind, address):
                    continue
                self.auto_seen.add((kind, address))  # one try per run: a refusal is not repeated every round
                try:
                    bid = await self.add_bulb(kind, dict(dev.get("config") or {}), auto_address=address)
                except Exception as exc:  # needs fields the user must give, pairing refused, ...
                    log.info("autoconnect: %s %s not added: %s", kind, address, exc)
                    continue
                log.info("autoconnect: added %s (%s)", bid, address)
                added.append(bid)
        return added

    async def autoconnect_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.autoconnect_wake.wait(), AUTOCONNECT_S)
            self.autoconnect_wake.clear()
            if not self.settings["autoconnect"] or self.sleeping:
                continue
            try:
                await self.autoconnect_round()
            except Exception as exc:  # never fatal
                log.warning("autoconnect: %s", exc)

    # ------------------------------------------------------------------ get

    def describe_bulb(self, b: Bulb) -> dict:
        info = registry.registry().get(b.kind)
        return {"id": b.id, "name": b.name, "kind": b.kind, "profile": b.profile, "power": b.power,
                "output": b.sent, "connected": b.connected, "online": b.online,
                "caps": sorted(self.caps(b)), "experimental": info.experimental if info else True,
                "following": not b.paused, "powerup": b.armed, "use": b.use,
                "step": round(b.backend.step(b) if b.backend else 1.0, 6),
                "autoconnected": "auto_address" in b.entry,
                "icon_on": b.entry.get("icon_on", ""),
                "icon_off": b.entry.get("icon_off", ""),
                "icon_disconnected": b.entry.get("icon_disconnected", ""),
                "audio": b.audio, "video": b.video, "video_region": b.video_region,
                "details": self.bulb_details(b)}

    @staticmethod
    def bulb_details(b: Bulb) -> dict:
        """What the backend knows cheaply about the device (model, address,
        MAC, firmware, RSSI, ...); only known keys. Never fails a get."""
        if not b.backend:
            return {}
        try:
            out = {k: v for k, v in b.backend.details(b).items() if v not in (None, "")}
        except Exception as exc:  # a backend quirk must not break get
            log.debug("details %s: %s", b.id, exc)
            return {}
        if b.kind == "mesh":  # the backend's "model" is the naming prefix (Hauslicht): the product instead
            out.pop("model", None)
            if product := mesh_product(out.get("cid"), out.get("pid")):
                out["model"] = product
        return out

    def monitor_power_state(self) -> dict:
        watch = self.monitor_power
        if watch is None:
            state = {"available": False, "why": _("a laptop: the lid switches the lights instead"), "monitors": []}
        else:
            state = watch.status(getattr(self.desktop, "laptop", False))
        return {**state, "follow": self.settings["monitor_power"]}

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
            "laptop": getattr(self.desktop, "laptop", True),
            "monitor_power": self.monitor_power_state(),
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
            "selected": self.selection(w), "flick": self.settings["flick"],
            "flick_last": self.settings["flick_last"], "setup": self.setup_state(),
            "exclusive_adapter": self.settings["exclusive_adapter"], "exclusive": self.exclusive_state(),
            "autoconnect": self.settings["autoconnect"],
            "autoconnect_ignore": [dict(i) for i in self.settings["autoconnect_ignore"]],
            "clap": self.clap_mode(w),
            **{k: self.settings[k] for k in ("clap_source", "clap_sensitivity")},
            "clap_status": self.clap.describe(),
            "audio": any(self.audio_following(b) for b in sel),
            "audio_level": max((self.want(b) for b in sel if self.audio_following(b)), default=None),
            **{k: self.settings[k] for k in AUDIO_KEYS},
            "audio_status": self.audio_status(),
            "follow_rate": self.settings["follow_rate"],
            "audio_rate": self.settings["follow_rate"],  # the old name of follow_rate
            "video": any(self.video_following(b) for b in sel),
            "video_level": max((self.want(b) for b in sel if self.video_following(b)), default=None),
            "video_region": sel[0].video_region if len(sel) == 1 else None,
            **{k: self.settings[k] for k in VIDEO_KEYS},
            "video_status": self.video_status(),
            "profile": name,
            "widget": w, "widgets": self.widget_count,
            "in_widget": [b.id for b in self.in_widget(w)],
            "bulbs_list": [self.describe_bulb(b) for b in self.bulbs.values()],
            "identifying": [bid for bid, a in self.actions.items() if a.kind == "identify"],
            "testing": {bid: dict(r) for bid, r in self.test_results.items() if bid in self.bulbs}
                       | {bid: a.describe() for bid, a in self.actions.items() if a.kind == "test"},
            "profiles": sorted(self.profiles, key=lambda n: (n != DEFAULT, n.lower())),
            "notify": self.settings["notify"],
            "notify_rules": [dict(r) for r in profile.get(NOTIFY_RULES, [])],
            "notify_status": self.notify_status(),
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
        color = (self.audio_color(b) if self.audio_following(b)
                 else self.video_color(b) if self.video_following(b) else self.color(b))
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
            bulbs = self.driven(self.targets(which, w))
            self.end_follow(bulbs)  # manual / automatic
            for b in bulbs:
                b.paused, b.manual, b.manual_exact = cmd == "pause", None, False
                if cmd == "resume":  # pause keeps the last level sent: get still reports it
                    b.sent = None
            self.wake.set()
            return get()
        if cmd in ("mode", "track", "off-at-zero", "lid", "monitor-power", "flick", "autoconnect", "clap", "clap-source",
                   "clap-sensitivity"):
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
            self.end_follow(self.selected(w))
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
        if cmd == "identify":
            if not args or (len(args) > 1 and args[1] not in ("on", "off")):
                raise UserError("identify ID [on|off]")
            if len(args) > 1 and args[1] == "off":
                bulb = self.find(args[0])
                action = self.actions.get(bulb.id)
                if action is not None and action.kind == "identify":
                    action.end("stop")
                    with contextlib.suppress(Exception):
                        await action.task  # the reply shows the bulb given back
            else:
                await self.start_action(args[0], "identify")
            return get()
        if cmd == "test":
            if not args or (len(args) > 1 and args[1] != "stop"):
                raise UserError("test ID [stop]")
            if len(args) > 1:
                bulb = self.find(args[0])
                action = self.actions.get(bulb.id)
                if action is not None and action.kind == "test":
                    action.end("stop")
                    with contextlib.suppress(Exception):
                        await action.task
            else:
                await self.start_action(args[0], "test")
            return get()
        if cmd == "notify":
            if args:
                if args[0] not in ("on", "off"):
                    raise UserError("notify on|off")
                self.set_value("notify", args[0], w=w)
            return get()
        if cmd == "notify-rules":
            return {"results": [{"profile": self.selected_profile_name(w),
                                 "rules": [dict(r) for r in self.selected_profile(w).get(NOTIFY_RULES, [])]}]}
        if cmd == "notify-rules-set":
            if not args:
                raise UserError("notify-rules-set JSON")
            self.set_notify_rules(" ".join(args), w)
            return get()
        if cmd == "notify-test":
            if not args:
                raise UserError("notify-test RULE_NAME")
            blinked = self.notify_test(" ".join(args), w)
            return {"results": [{**self.describe(w), "notify_test": blinked}]}
        if cmd == "notify-status":
            return {"results": [self.notify_status()]}
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
                raise UserError("bulb-set ID name=...|profile=...|use=on|off|icon_on=...|icon_off=...|icon_disconnected=...")
            self.bulb_set(args[0], _kv(args[1:]))
            return get()
        if cmd == "discover":
            found, errors, access = await self.discover(args[0] if args else None)
            return {"results": [{"discovered": found, "errors": errors, "access": access}]}
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
        if cmd == "unignore":
            if not args:
                raise UserError("unignore ID|ADDRESS")
            self.unignore(" ".join(args))
            return get()
        if cmd == "backends":
            return {"results": [{"backends": registry.describe()}]}
        if cmd == "clap-sources":
            try:
                return {"results": [{"sources": await clapmod.list_sources(),
                                     "selected": self.settings["clap_source"]}]}
            except clapmod.CaptureError as exc:
                raise UserError(str(exc)) from None
        if cmd == "audio":
            if not args or args[0] not in ("on", "off", "toggle"):
                raise UserError("audio on|off|toggle")
            bulbs = self.driven(self.targets(which, w))
            on = (not any(self.audio_following(b) for b in bulbs)) if args[0] == "toggle" else args[0] == "on"
            self.set_audio(bulbs, on)
            return get()
        if cmd in ("audio-source", "audio-color", "audio-rate"):
            if args:
                self.set_value(cmd.replace("-", "_"), args[0], w=w)
            return get()
        if cmd == "audio-sources":
            try:
                return {"results": [{"sources": await audiomod.list_sources(),
                                     "selected": self.settings["audio_source"]}]}
            except clapmod.CaptureError as exc:
                raise UserError(str(exc)) from None
        if cmd == "audio-status":
            return {"results": [self.audio_status()]}
        if cmd == "follow-rate":
            if args:
                self.set_value("follow_rate", args[0], w=w)
            return get()
        if cmd == "video":
            if not args or args[0] not in ("on", "off", "toggle"):
                raise UserError("video on|off|toggle")
            bulbs = self.driven(self.targets(which, w))
            on = (not any(self.video_following(b) for b in bulbs)) if args[0] == "toggle" else args[0] == "on"
            self.set_video(bulbs, on)
            return get()
        if cmd == "video-region":
            if len(args) != 2:
                raise UserError("video-region ID REGION")
            self.set_video_region(self.find(args[0]), args[1])
            return get()
        if cmd in ("video-max", "video-smooth"):
            if args:
                self.set_value(cmd.replace("-", "_"), args[0], w=w)
            return get()
        if cmd == "video-forget":
            await self.forget_video()
            return get()
        if cmd == "video-status":
            return {"results": [self.video_status()]}
        if cmd == "adapters":
            return {"results": [await self.adapters()]}
        if cmd == "clap-meter":
            return {"results": [{"clap": self.clap.mode, **self.clap.meter()}]}
        if cmd == "clap-hits":
            sens = self.settings["clap_sensitivity"]
            return {"results": [{"clap": self.clap.mode, "sensitivity": sens, "hits": self.clap.hits(),
                                 "bars": {k: round(v, 3) for k, v in clapmod.bars(sens).items()}}]}
        if cmd == "setup-root":
            self.start_setup()
            return get()
        if cmd == "setup-status":
            return get()  # the status is read anew on every get (after the widget's own pkexec run)
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
            if getattr(exc, "access", False):  # backends.NeedsAccess: the widget offers "Grant access…"
                reply["access"] = True
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
            self.apply_audio()  # a bulb out of use, removed, paused: the capture follows
            self.apply_video()
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
        self.autoconnector = asyncio.create_task(self.autoconnect_loop())
        if self.monitor_power is not None:
            self.check_monitor_power(baseline=True)  # the monitors as they are now; changes are events
            self.monitor_watcher = asyncio.create_task(self.watch_monitors())
        if self.settings["autoconnect"]:
            self.autoconnect_wake.set()
        self.apply_clap()
        self.apply_notify()
        self.apply_audio()  # bulbs that followed audio before the restart
        await self.check_video_permission()  # never a dialog unasked
        self.apply_video()
        if self.kde is not None:
            try:
                await self.kde.start()
            except Exception as exc:  # never fatal
                log.warning("KDE integration: %s", exc)

    async def stop(self) -> None:
        await self.end_actions("daemon stop")  # the bulbs back to their intended state, then disarm
        await self.shutdown_arm()  # while the scanners still feed a reconnect
        self.note_manual()
        if self.manual_saver is not None:
            self.manual_saver.cancel()
        self.save()  # the manual levels, written now rather than after the debounce
        if self.autoconnector is not None:
            self.autoconnector.cancel()
        if self.monitor_watcher is not None:
            self.monitor_watcher.cancel()
        self.clap.stop()
        if self.notifier is not None:
            self.notifier.stop()
        self.audio.stop()
        self.video.stop()
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

