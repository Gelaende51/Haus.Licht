"""Fakes shared by the LAN backend tests: the core's ctx and bulbs, and a
local aiohttp server. Nothing here touches the network beyond 127.0.0.1."""

from __future__ import annotations

from types import SimpleNamespace


class FakeCtx:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self._bulbs: list = []
        self.state: dict = {}

    def bulbs(self):
        return self._bulbs

    def flick(self, bulb_id):
        self.events.append(("flick", bulb_id))

    def power_returned(self, bulb_id, gap):
        self.events.append(("returned", bulb_id, gap))

    def changed(self):
        self.events.append(("changed",))

    def save(self):
        pass


def bulb(kind: str, suffix: str, **config):
    return SimpleNamespace(id=f"{kind}:{suffix}", name=suffix, config=config)


async def serve(app):
    """Start an aiohttp app on 127.0.0.1, return (runner, "127.0.0.1:port")."""
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"127.0.0.1:{port}"
