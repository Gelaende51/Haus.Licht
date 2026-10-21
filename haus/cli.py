"""hauslicht: lights that follow the laptop screen (Bluetooth mesh and more).

    hauslicht on | off               switch the selection (or one bulb: on mesh:0002)
    hauslicht dim 40                 brightness in percent (0 switches off)
    hauslicht status                 present state of every bulb
    hauslicht list                   bulbs in hauslicht.json
    hauslicht adopt                  provision a factory-fresh mesh bulb, then set it up
    hauslicht setup                  (re)bind AppKey and models on every mesh bulb
    hauslicht sync                   daemon: follow the screen backlight; adds a
                                     "keyboard brightness" slider to Plasma's applet
    hauslicht mode [MODE]            what the slider does: independent | minimum |
                                     maximum (default) | coupling | off
    hauslicht track [WHEN]           follow the screen: always (default) | night |
                                     day (Night Light) | dark | light (colour scheme) | never
    hauslicht off-at-zero [on|off]   a computed 0 % switches the bulbs dark; off
                                     (default): never below the lowest lit step while on
    hauslicht lid [WHEN]             lid events switch the bulbs: close (off) |
                                     open (on) | both (default) | never (laptops)
    hauslicht monitor-power [on|off] desktops: lights off when you switch the monitor
                                     off (not on standby), on again when it returns
                                     (default on where the switch can be seen)
    hauslicht flick [MODE]           wall-switch flicks: on (default: toggle) | off
                                     (just switch on) | fast (on + system tuning) |
                                     exclusive (fast, plus one Bluetooth adapter taken
                                     from BlueZ: mesh without a connection, see adapters)
    hauslicht adapters               the Bluetooth adapters exclusive can take; choose
                                     one: hauslicht-ctl set exclusive_adapter=hciN|MAC|auto
    hauslicht setup-root             the one-time system setup (sudo on a terminal,
                                     else the daemon asks via pkexec)
    hauslicht toggle                 switch the selection on/off (the widget's middle click)
    hauslicht slider 60              move the slider from the command line
    hauslicht nudge 5 | nudge -5     move the slider (or, in mode off, the brightness)
    hauslicht pause | resume         stop / restart following (daemon running)
    hauslicht select ID|all          which bulb the slider, nudge and on/off act on
    hauslicht identify BULB [off]    blink the bulb slowly to find it (stops after 2 minutes)
    hauslicht test BULB [stop]       brightest, a fade, colours, then on/off fast to slow (~25 s;
                                     flashes quickly); prints the fastest on/off rate
    hauslicht bulb-set ID name=X profile=Y
    hauslicht profile save|apply|rename|delete NAME...
    hauslicht discover [KIND|bluetooth] | add KIND key=value... | remove ID | backends
    hauslicht autoconnect [on|off]   add newly found devices by itself (never mesh
                                     bulbs; may take over a neighbour's lights)
    hauslicht unignore ID|ADDRESS    let autoconnect add a removed device again
    hauslicht clap [MODE]            a double clap/snap toggles widget 1's lights: off (default) |
                                     clap | snap | both (needs requirements-clap.txt)
    hauslicht clap-source [NAME]     the microphone (default: the default source)
    hauslicht clap-sensitivity [N]   0 (low) - 100 (high), default 50
    hauslicht clap-sources           the microphones to choose from
    hauslicht clap-meter             level, last hit and its class, last double
    hauslicht clap-hits              the last 30 hits: gate numbers, family score, top 3, verdict
    hauslicht audio on|off|toggle [BULB]   the selection (or one bulb) follows what the
                                     computer plays: loudness -> brightness, pitch -> colour
    hauslicht audio-source [NAME]    what it listens to (default: the default output's monitor)
    hauslicht audio-color [on|off]   pitch colours the bulbs (default on)
    hauslicht audio-rate [N]         the same as follow-rate (its old name)
    hauslicht audio-sources          the sources to choose from, monitors included
    hauslicht audio-status           capture state, level, pitch, messages sent this run
    hauslicht video on|off|toggle [BULB]   the selection (or one bulb) follows its region of
                                     the screen: luminance -> brightness, colour -> hue
    hauslicht video-region ID REGION whole, left, right, top, bottom, top-left, top-right,
                                     bottom-left or bottom-right (per bulb, default whole)
    hauslicht video-max [N]          brightness of the brightest screen, 0-100 (default 100)
    hauslicht video-smooth [S]       time averaging in seconds: 0 0.2 0.5 1 2 5 (default 0.5)
    hauslicht video-forget           forget the screen permission (the next start asks)
    hauslicht video-status           capture state, frames, messages sent this run
    hauslicht follow-rate [N]        updates per second per bulb following audio or video,
                                     1-3 (default 2)
    hauslicht backends --requires KIND[,KIND...]|all   pip names to install (no daemon)
    hauslicht debug                  window with each bulb's intended state, live
                                     (PyQt6; detached, see haus/debugview.py)
    hauslicht disarm [--if-armed]    no daemon: OnPowerUp back to "on" (the unit's
                                     ExecStopPost; --if-armed: only the bulbs the
                                     daemon left armed, nothing when none)

While the daemon runs it owns the Bluetooth link and the mesh sequence number;
the other commands then go to it over a Unix socket instead of connecting.
HAUSLICHT_STATE and HAUSLICHT_SOCK point at another state file and socket (a
test instance; see docs/ctl-protocol.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from .common import SOCK_PATH, STATE_PATH, _, link_lock, read_state
from .profiles import FAST_FLICK, FLICK_MODES, LID_EVENTS, MODES, TRACKING, migrate

log = logging.getLogger("hauslicht")

DAEMON_ONLY = ("pause", "resume", "mode", "track", "off-at-zero", "lid", "monitor-power", "toggle", "slider", "nudge",
               "flick", "select", "bulb-set", "profile", "discover", "add", "remove", "backends", "setup-root",
               "autoconnect", "unignore", "clap", "clap-source", "clap-sensitivity", "clap-sources",
               "clap-meter", "clap-hits", "adapters", "identify", "test", "audio", "audio-source",
               "audio-color", "audio-rate", "audio-sources", "audio-status", "video", "video-region",
               "video-max", "video-smooth", "video-forget", "video-status", "follow-rate")
DIRECT = ("on", "off", "dim", "status", "raw")  # mesh, without the daemon


def print_results(results: list) -> int:
    missing = 0
    for r in results:
        r = {k: v for k, v in r.items() if k not in ("bulbs_list", "backends")}
        fields = ", ".join(f"{k}={v}" for k, v in r.items() if k != "bulb")
        if any(v is None for v in r.values()):
            missing += 1
            fields += "  " + _("(no answer: switched off at the wall?)")
        print(f"{r.get('bulb', '-')}: {fields}")
    return 1 if missing else 0


async def via_daemon(request: dict) -> dict | None:
    """Send one request to a running sync daemon; None when none is running."""
    if not SOCK_PATH.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCK_PATH))
    except (ConnectionRefusedError, FileNotFoundError):
        return None
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=30)
    writer.close()
    return json.loads(line)


TEST_STEPS = {"starting": "starting", "bright": "brightest", "fade": "fade to the dimmest level",
              "colour": "colours", "strobe": "on/off, fast to slow"}


async def cmd_test(args) -> int:
    """test BULB: start it, print its steps, then the fastest on/off rate the
    link managed; Ctrl+C stops it. test BULB stop: stop it."""
    words = [str(w) for w in (args.words or [])]
    reply = await via_daemon({"cmd": "test", "args": [args.percent] + words})
    if reply is None:
        print(_("the sync daemon is not running"), file=sys.stderr)
        return 1
    if "error" in reply:
        print(reply["error"], file=sys.stderr)
        return 1
    testing = reply["results"][0].get("testing") or {}
    bid = args.percent if args.percent in testing else next(
        (k for k, v in testing.items() if v.get("running")), args.percent)
    if words:
        return 0
    step = None
    try:
        while True:
            t = testing.get(bid)
            if t is None:
                return 1
            if t["step"] != step and t["step"] in TEST_STEPS:
                step = t["step"]
                print(_(TEST_STEPS[step]), flush=True)
            if not t["running"]:
                break
            await asyncio.sleep(0.5)
            reply = await via_daemon({"cmd": "get"})
            if reply is None or "error" in reply:
                return 1
            testing = reply["results"][0].get("testing") or {}
    except asyncio.CancelledError:  # Ctrl+C: stop the test too
        await via_daemon({"cmd": "test", "args": [bid, "stop"]})
        raise
    if t["step"] != "done":
        print(_("test stopped ({why})").format(why=t.get("why") or "-"))
    if t.get("rate") is not None:
        print(_("fastest on/off: {rate} per second").format(rate=t["rate"]))
    return 0 if t["step"] == "done" else 1


async def cmd_control(args) -> int:
    words = [str(w) for w in (getattr(args, "words", None) or [])]
    arg = getattr(args, "percent", None)
    if arg is not None:
        words = [str(arg)] + words
    request = {"cmd": args.cmd, "arg": " ".join(words) or None, "args": words, "which": args.bulb}
    reply = await via_daemon(request)
    if reply is not None:
        if "error" in reply:
            print(reply["error"], file=sys.stderr)
            return 1
        if args.cmd in ("discover", "backends", "add"):
            out = reply["results"][0]
            print(json.dumps({k: v for k, v in out.items() if k in ("discovered", "errors", "backends", "added")}, indent=2))
            return 0
        if args.cmd in ("clap-sources", "clap-meter", "clap-hits", "adapters", "audio-sources", "audio-status",
                        "video-status"):
            print(json.dumps(reply["results"][0], indent=2, ensure_ascii=False))
            return 0
        return print_results(reply["results"])
    if args.cmd in DAEMON_ONLY:
        print(_("the sync daemon is not running"), file=sys.stderr)
        return 1
    from .backends import mesh
    try:
        return print_results(await mesh.direct_control(STATE_PATH, args.cmd, request["arg"], args.bulb))
    except ConnectionError as exc:
        print(f"hauslicht: {exc}", file=sys.stderr)
        return 2


def cmd_list() -> int:
    state = read_state(STATE_PATH)
    if state is None:
        sys.exit(_("no {name} yet: run `hauslicht adopt` first").format(name=STATE_PATH.name))
    migrate(state)  # in memory only: the daemon may own the file
    devices = state.get("devices", {})
    for bid, e in state["bulbs"].items():
        extra = ""
        if e["kind"] == "mesh":
            dev = devices.get(e["config"].get("unicast", ""), {})
            extra = f"  {dev.get('address', '?')}  scale {e['config'].get('scale', 100)}"
        print(f"{bid}  {e['name']!r}  profile {e['profile']}  power {'on' if e['power'] else 'off'}{extra}")
    return 0


def qt_python() -> str | None:
    """An interpreter that has PyQt6: this one, else the system python3."""
    import importlib.util
    import shutil
    import subprocess
    if importlib.util.find_spec("PyQt6") is not None:
        return sys.executable
    for cand in ("/usr/bin/python3", shutil.which("python3")):
        if cand and os.path.exists(cand) and subprocess.run(
                [cand, "-c", "import PyQt6.QtWidgets, PyQt6.QtNetwork"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return cand
    return None


def cmd_debug() -> int:
    """Start the debug window (haus/debugview.py) detached from this shell."""
    import subprocess
    from pathlib import Path
    py = qt_python()
    if py is None:
        print(_("the debug window needs PyQt6 (e.g. pacman -S python-pyqt6)"), file=sys.stderr)
        return 1
    root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.Popen([py, "-m", "haus.debugview"], cwd=root, env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hauslicht", description=_(__doc__.split("\n\n")[0].strip()))
    sub = parser.add_subparsers(dest="cmd", required=True)
    bulb_help = _("bulb id like mesh:0002 or unicast address like 0x0002 (default: the selection)")
    helps = {"on": _("switch bulbs on"), "off": _("switch bulbs off"), "status": _("show bulb state")}
    for name in ("on", "off", "status"):
        p = sub.add_parser(name, help=helps[name])
        p.add_argument("bulb", nargs="?", help=bulb_help)
    p = sub.add_parser("raw", help=_("send one hex access payload, print replies"))
    p.add_argument("percent", metavar="hex")
    p.add_argument("bulb", nargs="?", help=bulb_help)
    p = sub.add_parser("dim", help=_("set brightness in percent, 0 switches off"))
    p.add_argument("percent", type=float)
    p.add_argument("bulb", nargs="?", help=bulb_help)
    for name in ("pause", "resume"):
        sub.add_parser(name, help=_("stop following the screen") if name == "pause"
                       else _("follow the screen again")).set_defaults(bulb=None)
    sub.add_parser("list", help=_("list the bulbs"))
    p = sub.add_parser("adopt", help=_("take a factory-fresh bulb into the mesh"))
    p.add_argument("--scan", type=float, default=60.0, help=_("seconds to wait for a fresh bulb"))
    p.add_argument("--verbose", action="store_true")
    p = sub.add_parser("setup", help=_("bind keys and models on adopted bulbs again"))
    p.add_argument("bulb", nargs="?", help=_("unicast address like 0x0002 (default: all)"))
    p = sub.add_parser("mode", help=_("how the slider shapes the bulbs (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=MODES)
    p = sub.add_parser("track", help=_("when the screen drives the bulbs (daemon running)"))
    p.add_argument("percent", metavar="when", nargs="?", choices=TRACKING)
    p = sub.add_parser("off-at-zero", help=_(
        "a computed 0 %% switches the bulbs dark; off: never below 0.5 %% while on (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=("on", "off"))
    p = sub.add_parser("lid", help=_("what lid events do: close (off), open (on), both, never (daemon running)"))
    p.add_argument("percent", metavar="when", nargs="?", choices=LID_EVENTS)
    p = sub.add_parser("monitor-power", help=_(
        "desktops: lights off when you switch the monitor off (not on standby), on when it returns (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=("on", "off"))
    p = sub.add_parser("flick", help=_(
        "wall-switch flicks: off | on | fast | exclusive; fast and exclusive need the system setup (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=FLICK_MODES)
    sub.add_parser("setup-root", help=_(
        "the one-time system setup: sudo on a terminal, otherwise the daemon asks via pkexec"))
    sub.add_parser("toggle", help=_("switch the bulbs on or off (daemon running)"))
    p = sub.add_parser("slider", help=_("set the slider, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="value", type=float)
    p = sub.add_parser("nudge", help=_("move the slider by N percent, in mode off the brightness (daemon running)"))
    p.add_argument("percent", metavar="N", type=float)
    p = sub.add_parser("select", help=_(
        "which bulb the slider, nudge and on/off act on: a bulb id or all (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p = sub.add_parser("bulb-set", help=_(
        "rename a bulb or give it a profile: ID name=... profile=... (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p.add_argument("words", metavar="key=value", nargs="+")
    p = sub.add_parser("profile", help=_(
        "profiles: save NAME | apply NAME | rename OLD NEW | delete NAME (daemon running)"))
    p.add_argument("percent", metavar="action", choices=("save", "apply", "rename", "delete"))
    p.add_argument("words", metavar="NAME", nargs="+")
    p = sub.add_parser("discover", help=_("look for new bulbs, optionally of one kind or every Bluetooth kind (daemon running)"))
    p.add_argument("percent", metavar="KIND", nargs="?")
    p = sub.add_parser("add", help=_("add a bulb: KIND key=value ... (daemon running)"))
    p.add_argument("percent", metavar="KIND")
    p.add_argument("words", metavar="key=value", nargs="*")
    p = sub.add_parser("remove", help=_("stop controlling a bulb (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p = sub.add_parser("autoconnect", help=_(
        "add newly found devices by itself, never mesh bulbs; may take over a neighbour's lights (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=("on", "off"))
    p = sub.add_parser("unignore", help=_(
        "let autoconnect add a removed device again: its id or address (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p = sub.add_parser("clap", help=_(
        "a double clap or snap toggles widget 1's lights: off | clap | snap | both (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=("off", "clap", "snap", "both"))
    p = sub.add_parser("clap-source", help=_(
        "the microphone for clap detection: a source name or default (daemon running)"))
    p.add_argument("percent", metavar="NAME", nargs="?")
    p = sub.add_parser("clap-sensitivity", help=_("clap detection sensitivity, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    sub.add_parser("clap-sources", help=_("list the microphones (daemon running)"))
    p = sub.add_parser("audio", help=_(
        "the selection (or one bulb) follows what the computer plays: loudness to brightness, "
        "pitch to colour (daemon running)"))
    p.add_argument("percent", metavar="on|off|toggle", choices=("on", "off", "toggle"))
    p.add_argument("bulb", nargs="?", help=bulb_help)
    p = sub.add_parser("audio-source", help=_(
        "what follow-audio listens to: a source name or default, the default output's monitor (daemon running)"))
    p.add_argument("percent", metavar="NAME", nargs="?")
    p = sub.add_parser("audio-color", help=_("follow-audio: pitch colours the bulbs, on or off (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=("on", "off"))
    p = sub.add_parser("audio-rate", help=_(
        "follow-audio: updates per second per bulb, 1-3; Bluetooth mesh at most 2 recommended (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=int, choices=(1, 2, 3))
    p = sub.add_parser("follow-rate", help=_(
        "follow audio and video: updates per second per bulb, 1-3; Bluetooth mesh at most 2 recommended "
        "(daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=int, choices=(1, 2, 3))
    sub.add_parser("audio-sources", help=_(
        "list the sources follow-audio can listen to, monitors included (daemon running)"))
    sub.add_parser("audio-status", help=_(
        "follow-audio: capture state, level, pitch and the messages sent this run (daemon running)"))
    p = sub.add_parser("video", help=_(
        "the selection (or one bulb) follows its region of the screen: luminance to brightness, "
        "colour to hue (daemon running)"))
    p.add_argument("percent", metavar="on|off|toggle", choices=("on", "off", "toggle"))
    p.add_argument("bulb", nargs="?", help=bulb_help)
    p = sub.add_parser("video-region", help=_(
        "the part of the screen a bulb follows: whole, left, right, top, bottom, top-left, top-right, "
        "bottom-left or bottom-right (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p.add_argument("words", metavar="REGION", nargs=1)
    p = sub.add_parser("video-max", help=_(
        "follow-video: the brightness of the brightest screen, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    p = sub.add_parser("video-smooth", help=_(
        "follow-video: time averaging in seconds, 0, 0.2, 0.5, 1, 2 or 5 (daemon running)"))
    p.add_argument("percent", metavar="S", nargs="?", type=float)
    sub.add_parser("video-forget", help=_(
        "follow-video: forget the screen permission; the next start asks again (daemon running)"))
    sub.add_parser("video-status", help=_(
        "follow-video: capture state, frames and the messages sent this run (daemon running)"))
    p = sub.add_parser("identify", help=_(
        "blink a bulb slowly to find it, off: stop; it stops by itself after 2 minutes (daemon running)"))
    p.add_argument("percent", metavar="BULB")
    p.add_argument("words", metavar="on|off", nargs="*")
    p = sub.add_parser("test", help=_(
        "test a bulb: brightest, a fade, colours, then on/off fast to slow, about 25 s; flashes quickly, "
        "not for people sensitive to flashing light; stop: end it (daemon running)"))
    p.add_argument("percent", metavar="BULB")
    p.add_argument("words", metavar="stop", nargs="*")
    sub.add_parser("adapters", help=_("list the Bluetooth adapters for flick mode exclusive (daemon running)"))
    sub.add_parser("clap-meter", help=_(
        "clap detection: level, last hit with class and score, last double (daemon running)"))
    sub.add_parser("clap-hits", help=_(
        "clap detection: the last 30 hits with gate numbers, scores and verdict (daemon running)"))
    p = sub.add_parser("backends", help=_("list the kinds of bulbs and what they need (daemon running)"))
    p.add_argument("--requires", metavar="KIND[,KIND...]", help=_(
        "print the pip packages these kinds need (all: every kind), one per line; no daemon needed"))
    sub.add_parser("debug", help=_(
        "open a window showing each bulb's intended state and the daemon's events, live (PyQt6)"))
    p = sub.add_parser("adverts", help=_("debug: print the bulbs' adverts with timestamps"))
    p.add_argument("seconds", type=float, nargs="?", default=30.0)
    p = sub.add_parser("disarm", help=_(
        "no daemon: OnPowerUp back to \"on\" so that the wall switch just switches on (the service's stop)"))
    p.add_argument("--if-armed", action="store_true", help=_(
        "only the bulbs the daemon left armed (its marker); nothing at all when there are none"))
    p = sub.add_parser("sync", help=_("daemon: follow the screen, provide the Plasma slider"))
    p.add_argument("--interval", type=float, default=0.3, help=_("seconds between backlight reads"))
    p.add_argument("--no-desktop-writes", action="store_true", help=_(
        "test instance: no keyboard-slider LED, no PowerDevil restart, no suspend inhibitor"))
    args = parser.parse_args(argv)
    if not hasattr(args, "bulb"):
        args.bulb = None

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "backends" and args.requires:
        from .backends import requires
        from .common import UserError
        try:
            print("\n".join(requires([k.strip() for k in args.requires.split(",") if k.strip()])))
        except UserError as exc:
            print(f"hauslicht: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "debug":
        return cmd_debug()
    if args.cmd == "adverts":
        from .backends import mesh
        return asyncio.run(mesh.adverts(read_state(STATE_PATH) or {}, args.seconds))
    if args.cmd == "adopt":
        from .backends import mesh
        return asyncio.run(mesh.adopt(args, STATE_PATH))
    logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    if args.cmd == "setup":
        from .backends import mesh
        state = read_state(STATE_PATH)
        if state is None:
            sys.exit(_("no {name} yet: run `hauslicht adopt` first").format(name=STATE_PATH.name))
        with link_lock(STATE_PATH):
            asyncio.run(mesh.setup(state, mesh.device_addrs(state, args.bulb), STATE_PATH))
        return 0
    if args.cmd == "disarm":
        import signal
        from . import disarm
        logging.getLogger("btmesh").setLevel(logging.WARNING)
        signal.alarm(int(disarm.DISARM_S) + 5)  # a backstop: a hung library call never holds the stop longer
        return disarm.run(STATE_PATH, if_armed=args.if_armed)
    if args.cmd == "sync":
        from . import logrepeat
        logrepeat.install()
        if os.environ.get("HAUSLICHT_DEBUG"):
            logging.getLogger("bleak_retry_connector").setLevel(logging.DEBUG)
            log.setLevel(logging.DEBUG)
        from .daemon import Daemon

        async def run() -> int:
            return await Daemon(STATE_PATH, SOCK_PATH, args.interval,
                                desktop_writes=not args.no_desktop_writes).run()
        return asyncio.run(run())
    logging.getLogger("btmesh").setLevel(logging.WARNING)
    log.setLevel(logging.WARNING)
    if args.cmd in ("setup-root", "flick") and sys.stdin.isatty():
        from . import rootsetup
        if args.cmd == "setup-root":
            return rootsetup.run_on_tty()
        if args.percent in FAST_FLICK and rootsetup.status()["status"] != "current":
            print(_("flick mode {mode} needs the system setup first (sudo; Bluetooth restarts)").format(
                mode=args.percent))
            if rootsetup.run_on_tty() != 0:
                print(_("system setup failed: flick mode unchanged"), file=sys.stderr)
                return 1
    if args.cmd == "test":
        return asyncio.run(cmd_test(args))
    return asyncio.run(cmd_control(args))
