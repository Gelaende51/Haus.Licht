"""Matter lights through a Matter controller's WebSocket API (experimental).

The controller is the user's matterjs-server (or the Home Assistant Matter
add-on, the same server; the archived python-matter-server speaks the same
protocol): `ws://host:5580/ws`. Commissioning stays with the controller; a
bulb here is a node id plus endpoint. Plain aiohttp: `start_listening` gives
every node with its attributes ("endpoint/cluster/attribute" keys) and then
pushes changes. A node going unavailable and coming back is a power return.
Power-on behaviour: OnOff StartUpOnOff (0x0006/0x4003: 0 off, 1 on,
null previous), written through the controller, when the endpoint has it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time

from . import Backend, Offline
from ..common import UserError, _language

log = logging.getLogger("hauslicht")

MIN_INTERVAL_S = 0.2  # per node: Thread bulbs answer in tens to hundreds of ms
REQUEST_TIMEOUT_S = 10.0
TRANSITION_DS = 2  # tenths of a second
ONOFF, LEVEL = 6, 8
ATTR_ONOFF, ATTR_LEVEL, ATTR_MIN_LEVEL, ATTR_STARTUP_ONOFF = 0, 0, 2, 0x4003
STARTUP = {"off": 0, "on": 1, "restore": None}

GERMAN = {
    "Matter server not reachable at {url}: {error}": "Matter-Server unter {url} nicht erreichbar: {error}",
    "no dimmable light at node {node} endpoint {endpoint}; known: {known}":
        "Kein dimmbares Licht an Knoten {node}, Endpunkt {endpoint}; bekannt: {known}",
}


def _t(text: str) -> str:
    return GERMAN.get(text, text) if _language() == "de" else text


def attr(node: dict, endpoint: int, cluster: int, attribute: int):
    return (node.get("attributes") or {}).get(f"{endpoint}/{cluster}/{attribute}")


def has_attr(node: dict, endpoint: int, cluster: int, attribute: int) -> bool:
    return f"{endpoint}/{cluster}/{attribute}" in (node.get("attributes") or {})


def light_endpoints(node: dict) -> list[int]:
    """Endpoints with Level Control's CurrentLevel: dimmable lights."""
    eps = set()
    for key in node.get("attributes") or {}:
        ep, cluster, attribute = (int(x) for x in key.split("/"))
        if cluster == LEVEL and attribute == ATTR_LEVEL and has_attr(node, ep, ONOFF, ATTR_ONOFF):
            eps.add(ep)
    return sorted(eps)


def node_name(node: dict) -> str:
    return (attr(node, 0, 0x28, 5) or attr(node, 0, 0x28, 3)  # NodeLabel, ProductName
            or f"Matter node {node.get('node_id')}")


class MatterConnection:
    def __init__(self, url: str, on_node=None, on_change=None) -> None:
        self.url = url
        self.on_node = on_node      # callable(node_id, old node or None, new node or None)
        self.on_change = on_change
        self.session = None
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.ids = itertools.count(1)
        self.pending: dict[str, asyncio.Future] = {}
        self.nodes: dict[int, dict] = {}
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def ensure(self) -> None:
        async with self.lock:
            if not self.connected:
                await self._connect()

    async def _connect(self) -> None:
        import aiohttp
        await self.close(quiet=True)
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await self.session.ws_connect(self.url, heartbeat=30)
            info = await asyncio.wait_for(self.ws.receive_json(), REQUEST_TIMEOUT_S)  # server_info
            log.info("matter server %s: schema %s", self.url, info.get("schema_version"))
        except BaseException:
            await self.close(quiet=True)
            raise
        self.reader = asyncio.get_running_loop().create_task(self._read())
        nodes = await self.command("start_listening")
        self.nodes = {n["node_id"]: n for n in nodes or []}
        if self.on_change:
            self.on_change()

    async def _read(self) -> None:
        import aiohttp
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.json())
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("matter server connection closed"))
            self.pending.clear()
            if self.on_change:
                self.on_change()

    def _dispatch(self, msg: dict) -> None:
        if "message_id" in msg and "event" not in msg:
            fut = self.pending.pop(str(msg["message_id"]), None)
            if fut is None or fut.done():
                return
            if "error_code" in msg:
                fut.set_exception(RuntimeError(f"matter: {msg.get('error_code')}: {msg.get('details')}"))
            else:
                fut.set_result(msg.get("result"))
            return
        event, data = msg.get("event"), msg.get("data")
        if event in ("node_added", "node_updated") and isinstance(data, dict):
            old = self.nodes.get(data["node_id"])
            self.nodes[data["node_id"]] = data
            self._notify(data["node_id"], old, data)
        elif event == "node_removed":
            old = self.nodes.pop(data, None)
            self._notify(data, old, None)
        elif event == "attribute_updated" and isinstance(data, list) and len(data) == 3:
            node = self.nodes.get(data[0])
            if node is not None:
                node.setdefault("attributes", {})[data[1]] = data[2]
        elif event in ("node_unavailable", "node_available"):  # older servers
            node = self.nodes.get(data)
            if node is not None:
                old = dict(node)
                node["available"] = event == "node_available"
                self._notify(data, old, node)

    def _notify(self, node_id, old, new) -> None:
        if self.on_node:
            self.on_node(node_id, old, new)

    async def command(self, command: str, **args):
        if not self.connected:
            raise ConnectionError("matter server not connected")
        mid = str(next(self.ids))
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self.ws.send_json({"message_id": mid, "command": command, "args": args})
        try:
            return await asyncio.wait_for(fut, REQUEST_TIMEOUT_S)
        finally:
            self.pending.pop(mid, None)

    async def close(self, quiet: bool = False) -> None:
        was = self.connected
        if self.reader:
            self.reader.cancel()
            self.reader = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        if self.session is not None:
            await self.session.close()
            self.session = None
        if was and not quiet and self.on_change:
            self.on_change()


class MatterBackend(Backend):
    kind = "matter"
    title = "Matter (through a matterjs-server / Matter server)"
    experimental = True  # tested against a fake server only; depends on a beta controller
    requires = ("aiohttp",)
    fields = (
        {"key": "url", "label": "Matter server WebSocket", "label_de": "WebSocket des Matter-Servers",
         "type": "text", "required": True, "default": "ws://localhost:5580/ws"},
        {"key": "node", "label": "Node id", "label_de": "Knoten-ID", "type": "int", "required": True},
        {"key": "endpoint", "label": "Endpoint (0: the first light)", "label_de": "Endpunkt (0: das erste Licht)",
         "type": "int", "required": False, "default": 0},
    )

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.conns: dict[str, MatterConnection] = {}
        self.last_sent: dict[tuple, float] = {}
        self.lost_at: dict[tuple, float] = {}

    def conn(self, url: str) -> MatterConnection:
        url = url.strip()
        if url not in self.conns:
            self.conns[url] = MatterConnection(url, lambda n, o, new, url=url: self._on_node(url, n, o, new),
                                               self.ctx.changed)
        return self.conns[url]

    def _on_node(self, url: str, node_id, old: dict | None, new: dict | None) -> None:
        was = bool(old and old.get("available"))
        now = bool(new and new.get("available"))
        if was == now:
            return
        key = (url, node_id)
        if was and not now:
            self.lost_at[key] = time.monotonic()
        elif key in self.lost_at:
            gap = time.monotonic() - self.lost_at.pop(key)
            for b in self.ctx.bulbs():
                if b.config.get("url") == url and b.config.get("node") == node_id:
                    self.ctx.power_returned(b.id, gap)
        self.ctx.changed()

    def _node(self, bulb) -> dict | None:
        conn = self.conns.get(bulb.config["url"])
        return conn.nodes.get(bulb.config["node"]) if conn else None

    async def _pace(self, key: tuple) -> None:
        wait = self.last_sent.get(key, 0.0) + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_sent[key] = time.monotonic()

    # ------------------------------------------------------------ contract

    def caps(self, bulb) -> set[str]:
        return {"onoff", "dim"} | ({"powerup"} if bulb.config.get("powerup") else set())

    def step(self, bulb) -> float:
        return 100 / 254  # Level cluster 1-254

    def min_lit(self, bulb) -> float:
        return max(0.4, round(int(bulb.config.get("min_level") or 1) / 254 * 100, 2))

    async def stop(self) -> None:
        for conn in self.conns.values():
            conn.on_change = None
            await conn.close()

    async def resume(self) -> None:
        for conn in self.conns.values():
            await conn.close()

    def status(self, bulb) -> dict:
        conn = self.conns.get(bulb.config["url"])
        if conn is None or not conn.connected:
            return {"connected": False, "online": None}
        node = conn.nodes.get(bulb.config["node"])
        return {"connected": True, "online": None if node is None else bool(node.get("available"))}

    async def apply(self, bulb, percent: float) -> None:
        c = bulb.config
        conn = self.conn(c["url"])
        await conn.ensure()
        node = conn.nodes.get(c["node"])
        if node is not None and not node.get("available"):
            raise Offline(f"matter node {c['node']} unavailable")
        await self._pace((c["url"], c["node"]))
        if percent <= 0:
            await conn.command("device_command", node_id=c["node"], endpoint_id=c["endpoint"],
                               cluster_id=ONOFF, command_name="Off", payload={})
        else:
            level = max(int(c.get("min_level") or 1), min(254, round(percent / 100 * 254)))
            await conn.command("device_command", node_id=c["node"], endpoint_id=c["endpoint"],
                               cluster_id=LEVEL, command_name="MoveToLevelWithOnOff",
                               payload={"level": level, "transitionTime": TRANSITION_DS,
                                        "optionsMask": 0, "optionsOverride": 0})

    async def set_powerup(self, bulb, value: str) -> bool:
        c = bulb.config
        if not c.get("powerup"):
            return False
        conn = self.conn(c["url"])
        await conn.ensure()
        want = STARTUP[value]
        node = conn.nodes.get(c["node"]) or {}
        path = f"{c['endpoint']}/{ONOFF}/{ATTR_STARTUP_ONOFF}"
        if path in (node.get("attributes") or {}) and node["attributes"][path] == want:
            return True
        result = await conn.command("write_attribute", node_id=c["node"], attribute_path=path, value=want)
        statuses = result if isinstance(result, list) else [result]
        ok = all(isinstance(s, dict) and s.get("Status", s.get("status", 0)) == 0 for s in statuses)
        if ok and node:
            node.setdefault("attributes", {})[path] = want
        return ok

    async def query(self, bulb) -> dict | None:
        node = self._node(bulb)
        if node is None:
            return None
        c = bulb.config
        level = attr(node, c["endpoint"], LEVEL, ATTR_LEVEL)
        return {"on": attr(node, c["endpoint"], ONOFF, ATTR_ONOFF),
                "percent": None if level is None else round(level / 254 * 100, 1)}

    async def add(self, config: dict) -> dict:
        url = str(config.get("url") or "").strip()
        conn = self.conn(url)
        try:
            await conn.ensure()
        except Exception as exc:
            raise UserError(_t("Matter server not reachable at {url}: {error}").format(url=url, error=exc)) from exc
        node_id, endpoint = int(config["node"]), int(config.get("endpoint") or 0)
        node = conn.nodes.get(node_id) or {}
        eps = light_endpoints(node)
        if endpoint == 0 and eps:
            endpoint = eps[0]
        if endpoint not in eps:
            known = ", ".join(f"{n}/{ep}" for n, nd in sorted(conn.nodes.items()) for ep in light_endpoints(nd)) or "-"
            raise UserError(_t("no dimmable light at node {node} endpoint {endpoint}; known: {known}").format(
                node=node_id, endpoint=endpoint or "-", known=known))
        return {"url": url, "node": node_id, "endpoint": endpoint,
                "min_level": int(attr(node, endpoint, LEVEL, ATTR_MIN_LEVEL) or 1),
                "powerup": has_attr(node, endpoint, ONOFF, ATTR_STARTUP_ONOFF),
                "id": f"{node_id}-{endpoint}"}

    async def remove(self, bulb) -> None:
        url = bulb.config.get("url")
        if not any(b is not bulb and b.config.get("url") == url for b in self.ctx.bulbs()):
            conn = self.conns.pop(url, None)
            if conn is not None:
                await conn.close(quiet=True)

    async def discover(self, timeout: float) -> list[dict]:
        """Dimmable lights on the Matter servers already configured, not added yet."""
        found = []
        added = {(b.config.get("url"), b.config.get("node"), b.config.get("endpoint")) for b in self.ctx.bulbs()}
        for url in sorted({b.config.get("url") for b in self.ctx.bulbs() if b.config.get("url")}):
            conn = self.conn(url)
            try:
                await asyncio.wait_for(conn.ensure(), timeout)
            except Exception as exc:
                log.info("discover matter %s: %s", url, exc)
                continue
            for node_id, node in sorted(conn.nodes.items()):
                for ep in light_endpoints(node):
                    if (url, node_id, ep) not in added:
                        found.append({"kind": self.kind, "address": f"{url} {node_id}/{ep}",
                                      "name": node_name(node),
                                      "config": {"url": url, "node": node_id, "endpoint": ep}})
        return found
