"""`hauslicht disarm [--if-armed]`: OnPowerUp back to "on" once the daemon is gone.

The unit's ExecStopPost runs `disarm --if-armed` after every stop. The
daemon keeps a marker beside the state file (common.armed_path) of the
bulbs whose OnPowerUp it may have armed to something other than "on"; a
clean stop clears it (Daemon.shutdown_arm), so the helper finds it empty
and returns at once, without the radio. After a clean stop (systemd's
$SERVICE_RESULT "success") it returns at once even with bulbs listed: the
daemon tried them itself, and one without power at the wall would hold up
every poweroff. After an unclean exit (crash, SIGKILL, OOM, a stop out of
time) it takes the
link lock (refusing while another hauslicht process holds it), opens the
mesh as the one-shot CLI commands do (MeshLink: its SEQ reserved ahead in
the state file before the first PDU), sets OnPowerUp "on" on the listed
bulbs only and takes each off the marker once confirmed. Other kinds go
through their own backend. Everything within DISARM_S.

Flick mode exclusive: the kernel hands the claimed adapter back to BlueZ
when the daemon's copy of the user channel closes (with the process), and
the root helper hci-claim, seeing its socket close, powers it on again
(install/hci-claim, up to 8 s). The helper never claims an adapter itself:
it waits up to ADAPTER_WAIT_S for BlueZ to have the adapters back and
powered, then uses GATT like the CLI; with no adapter powered it skips with
a log line and leaves the marker for the next stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from .common import link_lock, read_armed, read_state, save_state, write_armed

log = logging.getLogger("hauslicht")

DISARM_S = 20.0  # the whole run, from the lock to the link closed
CLOSE_S = 2.0  # of that: closing the link or the backend
ADAPTER_WAIT_S = 8.0  # BlueZ getting an adapter back from exclusive mode (hci-claim restores within 8 s)


class _Context:
    """What a backend may use of the core, without the core."""

    def __init__(self, state: dict, state_path: Path, kind: str, bulbs: list) -> None:
        self.state, self.state_path, self.kind, self._bulbs = state, state_path, kind, bulbs

    def save(self) -> None:
        save_state(self.state_path, self.state)

    def bulbs(self) -> list:
        return list(self._bulbs)

    def flick(self, bulb_id: str) -> None:
        pass

    def power_returned(self, bulb_id: str, gap_s: float | None) -> None:
        pass

    def changed(self) -> None:
        pass


def run(state_path: Path, if_armed: bool = True) -> int:
    """0: nothing (left) armed; 1: refused, skipped or a bulb still armed."""
    if if_armed and not read_armed(state_path):
        return 0  # the clean-stop case: no lock, no state file, no radio
    if if_armed and os.environ.get("SERVICE_RESULT") == "success":
        log.info("disarm: clean stop, the daemon tried itself; still armed: %s",
                 ", ".join(read_armed(state_path)))
        return 0
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(link_lock(state_path))
        except SystemExit as exc:
            log.warning("disarm: %s; skipped", exc)
            return 1
        return asyncio.run(_disarm(state_path, if_armed))


async def _disarm(state_path: Path, if_armed: bool) -> int:
    armed = read_armed(state_path)  # again, now under the lock
    state = read_state(state_path)
    if state is None:
        log.warning("disarm: no %s", state_path.name)
        return 1
    entries = state.get("bulbs", {})
    todo = list(armed)
    if not if_armed:  # every mesh bulb as well: the marker may be gone
        todo += [bid for bid, e in entries.items() if e.get("kind") == "mesh" and bid not in armed]
    gone = [bid for bid in todo if bid not in entries]
    for bid in gone:
        log.info("disarm: %s is no longer configured: taken off the marker", bid)
        armed.pop(bid, None)
    if gone:
        write_armed(state_path, armed)
    todo = [bid for bid in todo if bid in entries]
    if not todo:
        return 0
    log.info("disarm: OnPowerUp on for %s", ", ".join(todo))

    confirmed: set[str] = set()

    def done(bid: str) -> None:  # at once: the time limit may end the run after the next bulb
        confirmed.add(bid)
        if armed.pop(bid, None) is not None:
            write_armed(state_path, armed)

    kinds: dict[str, list[str]] = {}
    for bid in todo:
        kinds.setdefault(entries[bid].get("kind", ""), []).append(bid)
    try:
        async with asyncio.timeout(DISARM_S - CLOSE_S):
            for kind, bids in kinds.items():
                if kind == "mesh":
                    await _mesh(state_path, state, bids, done)
                else:
                    await _backend(state_path, state, kind, bids, done)
    except TimeoutError:
        log.warning("disarm: out of time (%.0f s)", DISARM_S)
    left = [bid for bid in todo if bid not in confirmed]
    if left:
        log.warning("disarm: still armed (the marked ones kept for the next stop): %s", ", ".join(left))
        return 1
    return 0


async def _close(what: str, coro) -> None:
    try:
        async with asyncio.timeout(CLOSE_S):
            await coro
    except Exception as exc:
        log.info("disarm: closing %s: %s", what, exc)


async def _bluetooth_ready() -> bool:
    """Every adapter back in BlueZ and the first one powered (bleak's
    default), waited for up to ADAPTER_WAIT_S; after that any powered one
    will do. False: none powered (Bluetooth off, rfkilled, or exclusive's
    adapter not handed back), or no adapter at all."""
    from . import hci
    deadline = time.monotonic() + ADAPTER_WAIT_S
    while True:
        sysfs = await asyncio.to_thread(hci.scan_sysfs)
        if not sysfs:
            log.warning("disarm: no Bluetooth adapter")
            return False
        bluez = await hci.bluez_adapters()
        if not bluez:
            return True  # BlueZ could not be asked: the connect will tell
        powered = [a["name"] for a in sysfs if bluez.get(a["name"], {}).get("powered")]
        absent = [a["name"] for a in sysfs if "powered" not in bluez.get(a["name"], {})]
        if not absent and sysfs[0]["name"] in powered:
            return True
        if time.monotonic() >= deadline:
            if powered:
                log.info("disarm: going on with %s (%s not powered in BlueZ)", ", ".join(powered),
                         ", ".join(a["name"] for a in sysfs if a["name"] not in powered))
                return True
            log.warning("disarm: no Bluetooth adapter powered in BlueZ (%s): Bluetooth off or rfkilled, "
                        "or flick mode exclusive's adapter not handed back; skipped",
                        ", ".join(f"{a['name']} {'absent' if a['name'] in absent else 'off'}" for a in sysfs))
            return False
        await asyncio.sleep(0.5)


async def _mesh(state_path: Path, state: dict, bids: list[str], done) -> None:
    """The CLI's one-shot path (mesh.direct_control): MeshLink with the
    state file as its SEQ store, connect, Set, close."""
    from .backends import mesh
    if not await _bluetooth_ready():
        return
    entries = state["bulbs"]
    link = mesh.MeshLink(state, lambda: save_state(state_path, state))
    try:
        await link.connect()
        for bid in bids:
            addr = int(entries[bid]["config"]["unicast"], 16)
            got = await link.set_onpowerup(addr, mesh.POWERUP_CODES["on"])
            confirmed = got == mesh.POWERUP_CODES["on"]
            log.info("disarm: %s OnPowerUp on (%s)", bid,
                     "ok" if confirmed else "no answer" if got is None else f"answer {got}")
            if confirmed:
                done(bid)
    except (ConnectionError, OSError) as exc:
        log.warning("disarm: mesh: %s", exc)
    except Exception as exc:  # bleak / BlueZ errors have no common base
        log.warning("disarm: mesh: %s: %s", type(exc).__name__, exc)
    finally:
        await _close("the mesh link", link.close())


async def _backend(state_path: Path, state: dict, kind: str, bids: list[str], done) -> None:
    """Any other kind: its backend, started for this alone."""
    from . import backends as registry
    from .backends import Offline
    from .daemon import Bulb
    try:
        cls = registry.backend_class(kind)
    except Exception as exc:
        log.warning("disarm: %s: %s", kind, exc)
        return
    bulbs = [Bulb(bid, state["bulbs"][bid]) for bid in bids]
    backend = cls(_Context(state, state_path, kind, bulbs))
    try:
        await backend.start()
        for b in bulbs:
            b.backend = backend
            try:
                confirmed = await backend.set_powerup(b, "on")
            except Offline as exc:
                confirmed = False
                log.info("disarm: %s: %s", b.id, exc)
            log.info("disarm: %s OnPowerUp on (%s)", b.id, "ok" if confirmed else "no answer")
            if confirmed:
                done(b.id)
    except Exception as exc:
        log.warning("disarm: %s: %s: %s", kind, type(exc).__name__, exc)
    finally:
        await _close(kind, backend.stop())
