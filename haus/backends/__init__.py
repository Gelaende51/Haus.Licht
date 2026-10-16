"""Backends: one module per kind of bulb, found here without a list to edit.

A module defines one subclass of Backend with a `kind`. The registry imports
every module of this package on first use; a module that fails to import (a
missing optional library at the top) is listed as unavailable, so optional
libraries are best imported inside methods or guarded. See docs/backend-api.md.
HAUSLICHT_BACKENDS (comma list of kinds) limits which kinds load: a test
instance with `mock` only never touches Bluetooth. The mock backend is hidden
unless HAUSLICHT_BACKENDS names it or HAUSLICHT_MOCK=1.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import re

from ..common import UserError, _

log = logging.getLogger("hauslicht")

CAPS = ("onoff", "dim", "flick", "powerup", "ct", "hs")  # ct: white temperature, hs: hue/saturation
FIELD_TYPES = ("text", "password", "int", "bool", "choice")


class Offline(Exception):
    """apply / set_powerup: the device is known unreachable right now. The core
    waits quietly (no retry back-off) until status() reports it online again."""


class Backend:
    """One instance per kind drives every bulb of that kind.

    `bulb` arguments are the core's bulb objects: read `bulb.id`, `bulb.name`
    and `bulb.config` (the json `config` dict; change it, then ctx.save())."""

    kind = ""            # id prefix: bulbs are "<kind>:<suffix>"
    title = ""           # shown when adding
    experimental = False
    requires: tuple = ()  # pip names
    modules: tuple | None = None  # import names of `requires` (default: derived from the pip names)
    fields: tuple = ()   # what `add` needs: {key, label, label_de, type, required, default, choices}

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"}

    def min_lit(self, bulb) -> float:
        return 1.0

    async def start(self) -> None:
        """Called once the first bulb of this kind exists (never for discover/add alone)."""

    async def stop(self) -> None:
        """Daemon shutdown, after the core has set OnPowerUp back to "on"."""

    async def apply(self, bulb, percent: float) -> None:
        """Show percent (0 = off). Raise on failure (the core retries with a
        back-off), Offline when the device is known gone. Called on every
        screen change, never concurrently within one backend."""
        raise NotImplementedError

    def ct_range(self, bulb) -> tuple[int, int]:
        """(min_k, max_k) of a `ct` bulb's white."""
        return (2700, 6500)

    async def apply_color(self, bulb, color: dict) -> bool:
        """Show a colour: {"mode": "hs", "hue": 0-360, "sat": 0-100} or
        {"mode": "ct", "kelvin": int} (within ct_range). Only for bulbs with
        the `hs` / `ct` cap, only while lit, after apply(). True once the
        device has it; False: retried later. Raise like apply()."""
        return False

    async def set_powerup(self, bulb, value: str) -> bool:
        """OnPowerUp "off" | "on" | "restore"; True once the device has it."""
        return False

    def status(self, bulb) -> dict:
        """{connected, online} (online None: not known yet); extra keys are kept."""
        return {"connected": True, "online": True}

    async def discover(self, timeout: float) -> list[dict]:
        """[{kind, address, name, config}] of devices that could be added."""
        return []

    async def add(self, config: dict) -> dict:
        """Validate / pair; return the config to store (raise UserError with a
        message for the user). Set config["id"] to a stable device-derived
        suffix if there is one, else the core numbers the bulb."""
        return config

    # optional hooks
    async def remove(self, bulb) -> None:
        """The bulb was removed from the configuration."""

    async def suspend(self) -> None:
        """Before the laptop sleeps, after the core switched and re-armed the bulbs."""

    async def resume(self) -> None:
        """After the laptop woke: links did not survive."""

    async def idle(self, idle: bool) -> None:
        """True: every bulb of this kind is idle (not in use; the core sends
        nothing and has OnPowerUp "on" confirmed), so a link need not be held.
        False: one is driven again (or needs arming); apply/set_powerup follow."""

    async def query(self, bulb) -> dict | None:
        """`status` command: what the device itself reports ({on, percent}), or None."""
        return None


def _module_name(pip_name: str) -> str:
    return re.split(r"[\[<>=!~ ;]", pip_name, maxsplit=1)[0].strip().lower().replace("-", "_")


class Info:
    def __init__(self, kind: str, cls: type[Backend] | None, missing: list[str], title: str = "") -> None:
        self.kind, self.cls, self.missing = kind, cls, missing
        self.title = cls.title if cls else (title or kind)
        self.experimental = bool(cls.experimental) if cls else True

    @property
    def available(self) -> bool:
        return self.cls is not None and not self.missing

    def describe(self) -> dict:
        return {"kind": self.kind, "title": self.title, "experimental": self.experimental,
                "available": self.available, "missing": self.missing,
                "fields": [dict(f) for f in (self.cls.fields if self.cls else ())]}


_registry: dict[str, Info] | None = None


def allowed_kinds() -> set[str] | None:
    value = os.environ.get("HAUSLICHT_BACKENDS", "").strip()
    return {k.strip() for k in value.split(",") if k.strip()} if value else None


def mock_enabled(allowed: set[str] | None) -> bool:
    return (allowed is not None and "mock" in allowed) or os.environ.get("HAUSLICHT_MOCK", "") == "1"


def registry() -> dict[str, Info]:
    global _registry
    if _registry is not None:
        return _registry
    allowed = allowed_kinds()
    found: dict[str, Info] = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or (allowed is not None and mod.name not in allowed):
            continue
        if mod.name == "mock" and not mock_enabled(allowed):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except ImportError as exc:
            found[mod.name] = Info(mod.name, None, [exc.name or str(exc)])
            log.info("backend %s unavailable: %s", mod.name, exc)
            continue
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, Backend) and obj is not Backend
                    and obj.kind and obj.__module__ == module.__name__):
                if allowed is not None and obj.kind not in allowed:
                    continue
                mods = obj.modules or tuple(_module_name(p) for p in obj.requires)
                missing = [pip for pip, m in zip(obj.requires, mods) if importlib.util.find_spec(m) is None]
                found[obj.kind] = Info(obj.kind, obj, missing)
    _registry = dict(sorted(found.items()))
    return _registry


def describe() -> list[dict]:
    return [info.describe() for info in registry().values()]


def requires(kinds: list[str]) -> list[str]:
    """pip names the given kinds need ("all": every kind), for install.sh."""
    reg = registry()
    if "all" in kinds:
        kinds = list(reg)
    out: list[str] = []
    for kind in kinds:
        info = reg.get(kind)
        if info is None:
            raise UserError(_("unknown kind of bulb {kind}; known: {known}").format(
                kind=kind, known=", ".join(reg) or "-"))
        if info.cls is None:
            raise UserError(f"{kind}: {', '.join(info.missing)}")
        out += [p for p in info.cls.requires if p not in out]
    return out


def backend_class(kind: str) -> type[Backend]:
    info = registry().get(kind)
    if info is None:
        raise UserError(_("unknown kind of bulb {kind}; known: {known}").format(
            kind=kind, known=", ".join(registry()) or "-"))
    if not info.available:
        raise UserError(_("{kind} needs {missing}: pip install it into the venv").format(
            kind=kind, missing=", ".join(info.missing)))
    return info.cls


def check_fields(cls: type[Backend], given: dict) -> dict:
    """Typed config from `add KIND key=value...` against the backend's fields."""
    from ..profiles import parse_bool
    config = dict(given)
    for f in cls.fields:
        key, label = f["key"], f.get("label", f["key"])
        if key not in config or config[key] in ("", None):
            if f.get("required"):
                raise UserError(_("{label} is required").format(label=label))
            if "default" in f:
                config[key] = f["default"]
            else:
                continue
        value = config[key]
        try:
            if f.get("type") == "int":
                config[key] = int(value)
            elif f.get("type") == "bool":
                config[key] = parse_bool(value)
            elif f.get("type") == "choice" and value not in f.get("choices", ()):
                raise ValueError(f"one of {', '.join(map(str, f.get('choices', ())))}")
        except ValueError as exc:
            raise UserError(f"{label}: {exc}") from exc
    return config
