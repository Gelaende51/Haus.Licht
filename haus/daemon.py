"""The core: bulbs, profiles and the selection; the screen loop; the ctl socket.

Every bulb's want comes from its own profile, power and pause state; one
worker per backend sends it (never two calls at once per backend) and keeps
OnPowerUp armed for wall-switch flicks. Backends call back through Context.
The protocol is in docs/ctl-protocol.md, the backend contract in
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
from .backends import Offline
from .common import ROOT, UserError, _, link_lock, read_state, save_state
from .desktop import Desktop, KbdSlider
from .profiles import (
    DEFAULT, KBD_BINDING, PROFILE_KEYS, SETTABLE_GLOBALS, check_global_value,
    check_profile_value, compute_want, is_tracking, kbd_to_value, migrate, new_state, powerup_target,
    value_to_kbd,
)

log = logging.getLogger("hauslicht")

QUICK_GAP_S = 15.0  # back within this after the drop was noticed: a flick, at any speed
ARM_RETRY_S = 60.0  # after an OnPowerUp Get/Set got no answer
NUDGE_ECHO_S = 3.0  # a keyboard-slider echo of a nudge's own push arrives within this
SEND_STEP = 0.25  # resend only when the want moved this much
BACKOFF_S, BACKOFF_MAX_S = 5.0, 300.0
SHUTDOWN_ARM_S = 8.0
DISCOVER_S = 5.0
POWERUP_INT = {"off": 0, "on": 1, "restore": 2}  # get's `onpowerup` keeps the old numbers


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

    def reset(self) -> None:
        self.paused, self.manual, self.manual_exact, self.sent = False, None, False, None


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


def flash(on: bool) -> None:
    """Debug: show a detected flick on screen at once (HAUSLICHT_FLASH)."""
    if os.environ.get("HAUSLICHT_FLASH"):
        asyncio.get_running_loop().create_task(asyncio.create_subprocess_exec(
            "/usr/bin/python3", str(ROOT / "dev" / "flash.py"), "on" if on else "off",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))


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
                 desktop_writes: bool = True, desktop=None) -> None:
        self.state_path, self.sock_path, self.interval = state_path, sock_path, interval
        self.state = read_state(state_path) or new_state()
        if migrate(self.state):
            log.info("%s migrated to schema 2", state_path.name)
        self.bulbs: dict[str, Bulb] = {bid: Bulb(bid, e) for bid, e in self.state["bulbs"].items()}
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

    def selected(self) -> list[Bulb]:
        sel = self.settings["selected"]
        return list(self.bulbs.values()) if sel == "all" or sel not in self.bulbs else [self.bulbs[sel]]

    def targets(self, which: str | None) -> list[Bulb]:
        return self.selected() if which in (None, "") else (
            list(self.bulbs.values()) if which == "all" else [self.find(which)])

    def selected_profile_name(self) -> str:
        sel = self.settings["selected"]
        if sel == "all" or sel not in self.bulbs:
            return DEFAULT
        name = self.bulbs[sel].profile
        return name if name in self.profiles else DEFAULT

    def selected_profile(self) -> dict:
        return self.profiles[self.selected_profile_name()]

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

    def powerup_target(self, bulb: Bulb) -> str:
        """Flick mode: boot into what a toggle would show. Otherwise "on": the
        wall switch behaves as with any bulb."""
        if not self.settings["flick"]:
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
                    for bulb in self.bulbs_of(kind):
                        with contextlib.suppress(Offline):  # the backend watches for it; status() tells
                            await self.serve(backend, bulb)
                backoff = BACKOFF_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: %s; retrying in %.0f s", kind, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def serve(self, backend, bulb: Bulb) -> None:
        """Send one bulb's want if it moved; keep its OnPowerUp armed."""
        if bulb.online is False or bulb.id not in self.bulbs:
            return
        want = self.want(bulb)
        if want is not None and (bulb.sent is None or abs(want - bulb.sent) >= SEND_STEP):
            await backend.apply(bulb, want)
            bulb.sent = want
            log.info("%s: screen %s -> %.1f %%", bulb.id, "on" if self.desktop.screen_on else "off", want)
        if "powerup" in self.caps(bulb):
            target = self.powerup_target(bulb)
            if bulb.armed != target and time.monotonic() >= bulb.arm_retry_at:
                await self.arm(backend, bulb, target)

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
            if connected and not bulb.connected:
                bulb.armed = None
            if online is False and bulb.online is not False:
                bulb.power_before_loss = bulb.power
            elif online is not False and bulb.online is False:
                bulb.reset()
            bulb.connected, bulb.online, bulb.extra = connected, online, st

    # ------------------------------------------------------------------ backend events

    def on_flick(self, bid: str) -> None:
        bulb = self.bulbs.get(bid)
        if bulb is None:
            return
        bulb.power = (not bulb.power) if self.settings["flick"] else True
        bulb.reset()
        self.save()
        flash(bulb.power)
        log.info("%s: flick -> %s%s", bid, "on" if bulb.power else "off",
                 "" if self.settings["flick"] else " (flick mode off: a power cycle means on)")
        self.wake.set()

    def on_power_returned(self, bid: str, gap: float | None) -> None:
        bulb = self.bulbs.get(bid)
        if bulb is None:
            return
        if gap is None:
            log.info("%s: first contact, power stays %s", bid, "on" if bulb.power else "off")
        elif gap < QUICK_GAP_S and self.settings["flick"]:
            bulb.power = not bulb.power_before_loss
            flash(bulb.power)
            log.info("%s: back %.1f s after the drop: flick -> %s", bid, gap, "on" if bulb.power else "off")
        else:
            bulb.power = True
            log.info("%s: back %.1f s after the drop: switched on", bid, gap)
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
            self.set_power(list(self.bulbs.values()), False, "lid closed")
        elif not closed and when in ("open", "both"):
            self.set_power(list(self.bulbs.values()), True, "lid opened")
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
                        if dark:
                            await backend.apply(bulb, 0.0)
                            bulb.sent = 0.0
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
            b.sent = None
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

    def set_value(self, key: str, value, from_kbd: bool = False) -> None:
        """Validate and store one setting: profile keys on the selection's
        profile, the rest globally."""
        if key in PROFILE_KEYS:
            name = self.selected_profile_name()
            profile = self.profiles[name]
            profile[key] = check_profile_value(profile, key, value)
            affected = self.using(name)
        elif key in SETTABLE_GLOBALS:
            self.settings[key] = check_global_value(key, value)
            affected = list(self.bulbs.values())
        else:
            raise ValueError(f"unknown setting {key}")
        self.save()
        for b in affected:
            b.paused, b.sent = False, None
        if not from_kbd and (key == "mode" or key == KBD_BINDING.get(self.settings["mode"])):
            self.later(self.sync_kbd())
        self.wake.set()

    def nudge(self, step: float) -> None:
        """Scroll on the tray icon: move the keyboard slider by step (or, in
        mode off, the brightness of the selection). No device traffic here:
        the workers send the latest value, and only the latest push runs."""
        if not math.isfinite(step):
            raise ValueError("nudge needs a number")
        if self.settings["mode"] != "off":
            value = max(0, min(KbdSlider.MAX, round(self.settings["slider"] + step)))
            if value != self.settings["slider"]:
                self.apply_slider(value)  # as a real slider move, minus the echo check
                if self.desktop.slider_ready:
                    self.push_kbd(value)
            return
        profile = self.selected_profile()
        if not self.tracking(profile):  # the level rules (mode off is never independent)
            self.set_value("level", float(profile["level"]) + step)
            return
        for b in self.selected():
            if b.paused and b.manual is not None:
                base = b.manual
            else:
                base = b.sent if b.sent is not None else (self.want(b) or 0.0)
            b.manual, b.manual_exact, b.paused = max(0.0, min(100.0, base + step)), False, True
        self.wake.set()

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

    def profile_command(self, action: str, names: list[str]) -> None:
        """save NAME | apply NAME | rename OLD NEW | delete NAME (also as `preset`)."""
        if action == "rename":
            if len(names) != 2:
                raise UserError("profile rename OLD NEW")
            old, new = names
        else:
            old = new = " ".join(names).strip()
        if not new:
            raise UserError(f"profile {action} needs a name")
        if action == "save":
            self.profiles[new] = dict(self.selected_profile())
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
            if self.settings["selected"] == "all" or self.settings["selected"] not in self.bulbs:
                # "all" works on the default profile: it takes the values, every bulb uses it
                if new != DEFAULT:
                    self.profiles[DEFAULT] = dict(self.profiles[new])
                for b in self.bulbs.values():
                    b.entry["profile"] = DEFAULT
            else:
                self.bulbs[self.settings["selected"]].entry["profile"] = new
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
                 "power": True, "config": config}
        self.state["bulbs"][bid] = entry
        self.bulbs[bid] = Bulb(bid, entry)
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
        if self.settings["selected"] == bulb.id:
            self.settings["selected"] = "all"
            self.later(self.sync_kbd())
        self.save()
        if bulb.backend is not None:
            with contextlib.suppress(Exception):
                await bulb.backend.remove(bulb)
        log.info("removed %s", bulb.id)

    def bulb_set(self, bid: str, values: dict) -> None:
        bulb = self.find(bid)
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
            else:
                raise UserError(f"bulb-set takes name=... and profile=..., not {key}")
        self.save()
        if self.settings["selected"] == bulb.id:
            self.later(self.sync_kbd())
        self.wake.set()

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
                "following": not b.paused, "powerup": b.armed}

    def describe(self) -> dict:
        sel = self.selected()
        name = self.selected_profile_name()
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
            "selected": self.settings["selected"], "flick": self.settings["flick"], "profile": name,
            "bulbs_list": [self.describe_bulb(b) for b in self.bulbs.values()],
            "profiles": sorted(self.profiles, key=lambda n: (n != DEFAULT, n.lower())),
            "backends": registry.describe(),
        }

    # ------------------------------------------------------------------ ctl

    async def command(self, req: dict) -> dict:
        cmd = req.get("cmd")
        arg = req.get("arg")
        args = req.get("args")
        if args is None:
            args = str(arg).split() if arg not in (None, "") else []
        args = [str(a) for a in args]
        which = req.get("which")
        get = lambda: {"results": [self.describe()]}  # noqa: E731

        if cmd == "get":
            return get()
        if cmd in ("pause", "resume"):
            for b in self.targets(which):
                b.paused, b.sent, b.manual, b.manual_exact = cmd == "pause", None, None, False
            self.wake.set()
            return get()
        if cmd in ("mode", "track", "off-at-zero", "lid", "flick"):
            if args:
                self.set_value(cmd.replace("-", "_"), args[0])
            return get()
        if cmd == "set":
            key, sep, value = " ".join(args).partition("=")
            if not sep:
                raise UserError("set KEY=VALUE")
            self.set_value(key.strip(), value.strip())
            return get()
        if cmd in ("preset", "profile"):
            if not args:
                raise UserError(f"{cmd} save|apply|rename|delete NAME")
            self.profile_command(args[0], args[1:])
            return get()
        if cmd in ("on", "off", "toggle"):
            bulbs = self.targets(which)
            on = (not any(b.power for b in bulbs)) if cmd == "toggle" else cmd == "on"
            self.set_power(bulbs, on, cmd)
            return get()
        if cmd == "nudge":
            self.nudge(float(args[0]))
            return get()
        if cmd == "bulb":
            # The widget's brightness slider: the level while it rules,
            # otherwise a manual setting until `resume`.
            value = max(0.0, min(100.0, float(args[0])))
            if self.settings["mode"] == "independent" or not self.tracking(self.selected_profile()):
                self.set_value("level", value)
            else:
                self.manual(self.selected(), value, exact=False)
            return get()
        if cmd == "slider":
            value = max(0, min(KbdSlider.MAX, round(float(args[0]))))
            if not (self.desktop.slider_ready and await self.desktop.push_slider(value)):
                self.slider_moved(value)
            return {"results": [{**self.describe(), "slider": value}]}
        if cmd == "dim":
            bulbs = self.targets(which)
            self.manual(bulbs, float(args[0]), exact=True)
            log.info("manual dim %s: following paused", args[0])
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and any(b.sent is None and b.online is not False for b in bulbs):
                await asyncio.sleep(0.1)
            return {"results": [{"bulb": b.id, "percent": b.sent} for b in bulbs]}
        if cmd == "status":
            results = []
            for b in self.targets(which):
                got = None
                if b.backend is not None and b.kind in self.locks:
                    async with self.locks[b.kind]:
                        with contextlib.suppress(Offline):
                            got = await b.backend.query(b)
                results.append({**(got or {"on": b.power and bool(b.sent), "percent": b.sent}), "bulb": b.id})
            return {"results": results}
        if cmd == "raw":
            results = []
            for b in self.targets(which):
                raw = getattr(b.backend, "raw", None)
                if raw is None:
                    raise UserError(f"{b.id}: raw is for mesh bulbs")
                async with self.locks[b.kind]:
                    results += await raw(b, args[0])
            return {"results": results}
        if cmd == "select":
            target = args[0] if args else "all"
            target = "all" if target == "all" else self.find(target).id
            self.settings["selected"] = target
            self.save()
            self.later(self.sync_kbd())
            return get()
        if cmd == "bulb-set":
            if len(args) < 2:
                raise UserError("bulb-set ID name=...|profile=...")
            self.bulb_set(args[0], _kv(args[1:]))
            return get()
        if cmd == "discover":
            return {"results": [{"discovered": await self.discover(args[0] if args else None)}]}
        if cmd == "add":
            if not args:
                raise UserError("add KIND key=value ...")
            bid = await self.add_bulb(args[0], _kv(args[1:]))
            return {"results": [{**self.describe(), "added": bid}]}
        if cmd == "remove":
            if not args:
                raise UserError("remove ID")
            await self.remove_bulb(args[0])
            return get()
        if cmd == "backends":
            return {"results": [{"backends": registry.describe()}]}
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
            reply = await self.command(json.loads(await reader.readline()))
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

    async def stop(self) -> None:
        await self.shutdown_arm()  # while the scanners still feed a reconnect
        for task in self.workers.values():
            task.cancel()
        for kind in list(self.started):
            with contextlib.suppress(Exception):
                await self.backends[kind].stop()
        self.desktop.close()

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
                    await self.follow()
            except asyncio.CancelledError:
                log.info("stopping")
            finally:
                await self.stop()
                with contextlib.suppress(FileNotFoundError):
                    self.sock_path.unlink()
        return 0

