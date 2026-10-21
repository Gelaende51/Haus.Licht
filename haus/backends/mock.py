"""Mock bulbs: no device, no library. Each remembers the last percent, colour
and OnPowerUp it was given (config `color`: none | ct | hs | both sets the colour caps;
`transition` on: fade() takes a transition time, one call), for tests and widget work (with HAUSLICHT_BACKENDS=mock
in a test instance; see docs/ctl-protocol.md). The core's ctl commands
mock-flick, mock-return and mock-offline drive the callbacks."""

from __future__ import annotations

import asyncio
import logging

from . import CAPS, Backend, Offline
from ..common import UserError

log = logging.getLogger("hauslicht")


class MockBackend(Backend):
    kind = "mock"
    title = "Mock bulb (testing)"
    experimental = True
    requires = ()
    fields = (
        {"key": "min_lit", "label": "Lowest lit %", "label_de": "Niedrigste Helligkeit %",
         "type": "choice", "required": False, "default": "1", "choices": ["0.5", "1"]},
        {"key": "address", "label": "Address", "label_de": "Adresse",
         "type": "text", "required": False, "default": ""},
        {"key": "color", "label": "Colour", "label_de": "Farbe",
         "type": "choice", "required": False, "default": "none", "choices": ["none", "ct", "hs", "both"]},
        {"key": "transition", "label": "Fades by itself (transition time)",
         "label_de": "Blendet selbst über (Übergangszeit)",
         "type": "bool", "required": False, "default": False},
        {"key": "fail", "label": "Refuse to be added (tests the error path)",
         "label_de": "Hinzufügen verweigern (testet den Fehlerfall)",
         "type": "bool", "required": False, "default": False},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.applied: dict[str, float] = {}  # bulb id -> last percent
        self.powerup: dict[str, str] = {}    # bulb id -> last OnPowerUp
        self.colors: dict[str, dict] = {}    # bulb id -> last colour
        self.offline: set[str] = set()
        self.calls: list[tuple] = []         # (what, bulb id, value), for tests

    def caps(self, bulb) -> set[str]:
        color = bulb.config.get("color", "none")
        extra = {"ct", "hs"} if color == "both" else {color} & {"ct", "hs"}
        return (set(CAPS) - {"ct", "hs"}) | extra

    def min_lit(self, bulb) -> float:
        return float(bulb.config.get("min_lit", 1))

    async def apply(self, bulb, percent: float) -> None:
        if bulb.id in self.offline:
            raise Offline("mock bulb offline")
        self.applied[bulb.id] = percent
        self.calls.append(("apply", bulb.id, percent))
        log.info("mock %s -> %.1f %%", bulb.id, percent)

    async def apply_color(self, bulb, color: dict) -> bool:
        if bulb.id in self.offline:
            raise Offline("mock bulb offline")
        self.colors[bulb.id] = dict(color)
        self.calls.append(("color", bulb.id, dict(color)))
        log.info("mock %s -> colour %s", bulb.id, color)
        return True

    async def fade(self, bulb, percent: float, seconds: float) -> bool:
        if not bulb.config.get("transition"):
            return False
        if bulb.id in self.offline:
            raise Offline("mock bulb offline")
        self.applied[bulb.id] = percent
        self.calls.append(("fade", bulb.id, percent))
        log.info("mock %s -> %.1f %% over %.1f s", bulb.id, percent, seconds)
        return True

    async def set_powerup(self, bulb, value: str) -> bool:
        if bulb.id in self.offline:
            raise Offline("mock bulb offline")
        self.powerup[bulb.id] = value
        self.calls.append(("powerup", bulb.id, value))
        return True

    def status(self, bulb) -> dict:
        online = bulb.id not in self.offline
        return {"connected": online, "online": online,
                "applied": self.applied.get(bulb.id), "device_powerup": self.powerup.get(bulb.id),
                "color": self.colors.get(bulb.id)}

    async def query(self, bulb) -> dict | None:
        if bulb.id in self.offline:
            raise Offline("mock bulb offline")
        percent = self.applied.get(bulb.id)
        return {"on": bool(percent), "percent": percent, "color": self.colors.get(bulb.id)}

    async def discover(self, timeout: float) -> list[dict]:
        await asyncio.sleep(min(timeout, 0.5))
        return [{"kind": self.kind, "address": f"mock-{n}", "name": f"Mock bulb {n}",
                 "config": {"address": f"mock-{n}", "min_lit": "1"}} for n in (1, 2)]

    async def add(self, config: dict) -> dict:
        if config.get("fail"):
            raise UserError("the mock bulb refused (fail=on)")
        if config.get("address"):
            config["id"] = str(config["address"]).replace(":", "").lower()
        return config

    async def idle(self, idle: bool) -> None:
        self.calls.append(("idle", None, idle))

    async def remove(self, bulb) -> None:
        for d in (self.applied, self.powerup, self.colors):
            d.pop(bulb.id, None)
        self.offline.discard(bulb.id)

    # test hooks (ctl: mock-flick ID, mock-return ID GAP, mock-offline ID)
    def simulate_flick(self, bulb) -> None:
        self.ctx.flick(bulb.id)

    def simulate_offline(self, bulb) -> None:
        self.offline.add(bulb.id)
        self.ctx.changed()

    def simulate_return(self, bulb, gap: float | None) -> None:
        self.offline.add(bulb.id)
        self.ctx.changed()  # the core notes the power before the outage
        self.ctx.power_returned(bulb.id, gap)
        self.offline.discard(bulb.id)
        self.ctx.changed()

