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
    hauslicht -w N COMMAND           in widget N's context (its bulbs, selection, clap mode)
    hauslicht --json COMMAND         the daemon's reply as JSON (otherwise text)
    hauslicht info | bulbs | watch   the widget's state, every bulb, the event stream
    hauslicht level 40               the widget's brightness slider
    hauslicht range LO HI | curve N | hue N | sat N | shift N | follow-ct on|off
                                     the selection's profile; set KEY=VALUE: any key
    hauslicht widgets N | widget-bulbs ID... | order ID...   widgets and lists
    hauslicht exclusive-adapter hciN | setup-status
    hauslicht bulb-set ID name=X profile=Y use=on|off icon_on=...
    hauslicht profile list|save|apply|rename|delete NAME...
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
    hauslicht notify [on|off]        desktop notifications blink the bulbs by rules (default off)
    hauslicht notify-rules           the rules of the selection's profile (JSON)
    hauslicht notify-rules-set JSON  replace them: [{name, app, title, body, pattern, count,
                                     intensity, color, bulbs, dark}], see docs/ctl-protocol.md
    hauslicht notify-test RULE       run a rule's pattern now
    hauslicht notify-status          watching or not, Do Not Disturb, the last match
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
import time

from .common import SOCK_PATH, STATE_PATH, TRANSLATION, _, link_lock, read_state
from .profiles import FAST_FLICK, FLICK_MODES, LID_EVENTS, MODES, TRACKING, migrate

log = logging.getLogger("hauslicht")

DAEMON_ONLY = ("pause", "resume", "mode", "track", "off-at-zero", "lid", "monitor-power", "toggle", "slider", "nudge",
               "flick", "select", "bulb-set", "profile", "discover", "add", "remove", "backends", "setup-root",
               "autoconnect", "unignore", "clap", "clap-source", "clap-sensitivity", "clap-sources",
               "clap-meter", "clap-hits", "adapters", "identify", "test", "audio", "audio-source",
               "audio-color", "audio-rate", "audio-sources", "audio-status", "video", "video-region",
               "video-max", "video-smooth", "video-forget", "video-status", "follow-rate", "notify",
               "notify-rules", "notify-rules-set", "notify-test", "notify-status", "info", "bulbs", "level",
               "range", "curve", "hue", "sat", "shift", "follow-ct", "set", "widgets", "widget-bulbs", "order",
               "exclusive-adapter", "setup-status", "watch")
DIRECT = ("on", "off", "dim", "status", "raw")  # mesh, without the daemon
LIST_REPLIES = ("status", "dim", "raw")  # the daemon replies one entry per bulb
# CLI commands that set one `set` key of the protocol (no value: show it)
SET_KEYS = {"curve": "curve", "hue": "hue", "sat": "sat", "shift": "shift", "follow-ct": "follow_ct",
            "widgets": "widgets", "exclusive-adapter": "exclusive_adapter"}
# CLI command -> the daemon's command, where the names differ
PROTOCOL = {"level": "bulb", "info": "get", "bulbs": "get", "setup-status": "setup-status"}
# What a command that replies the get object shows of it
SHOW = {
    "mode": ("mode", "kbd_binds"), "track": ("track", "tracking"), "off-at-zero": ("off_at_zero",),
    "lid": ("lid", "laptop"), "monitor-power": ("monitor_power",),
    "flick": ("flick", "flick_last", "exclusive_adapter", "exclusive"),
    "exclusive-adapter": ("exclusive_adapter", "exclusive"),
    "autoconnect": ("autoconnect", "autoconnect_ignore"), "unignore": ("autoconnect", "autoconnect_ignore"),
    "clap": ("widget", "clap", "clap_status"), "clap-source": ("clap_source",),
    "clap-sensitivity": ("clap_sensitivity",),
    "audio": ("selected", "audio", "audio_level"), "audio-source": ("audio_source",),
    "audio-color": ("audio_color",), "audio-rate": ("follow_rate",), "follow-rate": ("follow_rate",),
    "video": ("selected", "video", "video_level", "video_region"), "video-max": ("video_max",),
    "video-smooth": ("video_smooth",), "video-forget": ("video_status",),
    "notify": ("notify", "notify_status"),
    "select": ("widget", "selected", "profile"),
    "range": ("profile", "range_lo", "range_hi"), "curve": ("profile", "curve"), "hue": ("profile", "hue"),
    "sat": ("profile", "sat"), "shift": ("profile", "shift"), "follow-ct": ("profile", "follow_ct"),
    "widgets": ("widgets",), "widget-bulbs": ("widget", "in_widget"),
    "identify": ("identifying",), "setup-root": ("setup",), "setup-status": ("setup",),
    "level": ("selected", "power", "bulbs", "level", "following"),
}
SELECTION = ("widget", "selected", "power", "bulbs", "following")  # on, off, toggle, pause, ...
# booleans shown as on/off (settings and switches); the rest as yes/no
LABEL = {"bulbs": "output %"}  # get's "bulbs" is the selection's output, not a list
ONOFF = {"off_at_zero", "follow_ct", "autoconnect", "audio_color", "notify", "audio", "video", "power",
         "follow", "use"}


def scalar(value, key: str = "") -> str:
    """One value for reading: on/off or yes/no, - for none, lists joined."""
    if isinstance(value, bool):
        return ("on" if value else "off") if key in ONOFF else ("yes" if value else "no")
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict):
        return "{" + compact(value) + "}"
    if isinstance(value, (list, tuple)):
        return ", ".join(scalar(v, key) for v in value) or "-"
    return str(value)


def compact(d: dict) -> str:
    return ", ".join(f"{k}={scalar(v, k)}" for k, v in d.items())


def print_fields(d: dict, keys=None, indent: int = 0) -> None:
    """key: value lines; a dict nests, a list of dicts gives one line per item."""
    pad = " " * indent
    for k in keys or d:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, dict) and v:
            print(f"{pad}{k}:")
            print_fields(v, None, indent + 2)
        elif isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            print(f"{pad}{k}:")
            for item in v:
                print(f"{pad}  - {compact(item)}")
        else:
            print(f"{pad}{LABEL.get(k, k)}: {scalar(v, k)}")


def bulb_line(b: dict, in_widget=()) -> str:
    """One bulb of bulbs_list: id, name, kind, state, flags, icons."""
    parts = [b["id"], repr(b.get("name", "")), b.get("kind", "?"), f"profile {b.get('profile', '-')}"]
    if not b.get("use", True):
        parts.append(_("not in use"))
    else:
        out = b.get("output")
        parts.append(("on" if b.get("power") else "off") + (f" {out:g} %" if isinstance(out, (int, float)) else ""))
        if b.get("online") is False:
            parts.append(_("offline"))
        elif not b.get("connected", True):
            parts.append(_("not connected"))
    if b["id"] in in_widget:
        parts.append(_("in widget"))
    if b.get("audio"):
        parts.append(_("follows audio"))
    if b.get("video"):
        parts.append(_("follows video"))
    if b.get("video_region", "whole") != "whole":
        parts.append(f"region {b['video_region']}")
    if b.get("caps"):
        parts.append("caps " + ",".join(b["caps"]))
    icons = {k[5:]: b[k] for k in ("icon_on", "icon_off", "icon_disconnected") if b.get(k)}
    if icons:
        parts.append("icons " + compact(icons))
    if b.get("autoconnected"):
        parts.append(_("autoconnected"))
    return "  ".join(parts)


def print_bulbs(g: dict) -> None:
    bulbs = g.get("bulbs_list") or []
    if not bulbs:
        print(_("no bulbs"))
    for b in bulbs:
        print(bulb_line(b, g.get("in_widget") or ()))


def print_info(g: dict) -> None:
    """The get object in short: widget, selection, settings, bulbs."""
    print(_("widget {w} of {n}, selection {sel} (profile {profile})").format(
        w=g.get("widget", 1), n=g.get("widgets", 1), sel=g.get("selected"), profile=g.get("profile")))
    print_fields(g, ("power", "bulbs", "following", "tracking", "screen", "slider", "mode", "kbd_binds"))
    print(_("profile {name}:").format(name=g.get("profile")))
    print_fields(g, ("track", "off_at_zero", "range_lo", "range_hi", "curve", "level", "hue", "sat", "follow_ct",
                     "shift"), 2)
    print_fields(g, ("lid" if g.get("laptop") else "monitor_power", "flick", "autoconnect", "clap", "audio",
                     "video", "notify", "follow_rate"))
    print(f"setup: {(g.get('setup') or {}).get('status', '-')}")
    print(_("bulbs:"))
    for b in g.get("bulbs_list") or []:
        print("  " + bulb_line(b, g.get("in_widget") or ()))


def print_sources(r: dict) -> None:
    """clap-sources / audio-sources: one per line, * the chosen one."""
    selected = r.get("selected") or ""
    for s in r.get("sources") or []:
        # "" is the default: the default output's monitor (audio), the default microphone (clap)
        chosen = s["name"] == selected or (not selected and s.get("default") and s.get("monitor", True))
        flags = [f for f in ("default", "monitor", "muted") if s.get(f)]
        print(f"{'*' if chosen else ' '} {s['name']}  {s.get('description', '')}"
              + (f"  ({', '.join(flags)})" if flags else ""))


def print_adapters(r: dict) -> None:
    for a in r.get("adapters") or []:
        flags = [f for f in ("builtin", "powered", "in_bluez", "claimed") if a.get(f)]
        devices = ", ".join(str(d.get("name") or d.get("address") or d) if isinstance(d, dict) else str(d)
                            for d in a.get("devices") or [])
        print(f"{'*' if a.get('name') == r.get('chosen') else ' '} {a.get('name')}  {a.get('address', '-')}  "
              f"{a.get('bus', '-')}  {' '.join(flags)}" + (f"  {_('devices')}: {devices}" if devices else ""))
    print(f"exclusive_adapter: {scalar(r.get('selected'))}  chosen: {scalar(r.get('chosen'))}"
          + ("  " + _("(taking it leaves BlueZ without an adapter)") if r.get("others_stop") else ""))


def print_discovered(r: dict) -> None:
    found = r.get("discovered") or []
    if not found:
        print(_("nothing found"))
    for d in found:
        config = " ".join(f"{k}={v}" for k, v in (d.get("config") or {}).items())
        print(f"{d.get('kind')}  {d.get('address')}  {d.get('name', '')!r}  "
              + (_("(added already)") if d.get("known") else f"add: hauslicht add {d.get('kind')} {config}".rstrip()))
    for kind, why in (r.get("errors") or {}).items():
        print(f"{kind}: {why}", file=sys.stderr)


def print_backends(r: dict) -> None:
    """Each kind, whether it can be used, and the fields `add` takes."""
    for b in r.get("backends") or []:
        missing = ", ".join(b.get("missing") or [])
        state = _("ready") if b.get("available") else _("needs {missing}").format(missing=missing)
        print(f"{b['kind']}  {b.get('title', '')}  ({state}{', experimental' if b.get('experimental') else ''})")
        for f in b.get("fields") or []:
            label = f.get("label_de") if TRANSLATION and f.get("label_de") else f.get("label", "")
            extra = []
            if f.get("required"):
                extra.append(_("required"))
            if f.get("choices"):
                extra.append("|".join(map(str, f["choices"])))
            if f.get("default") not in (None, "", False):
                extra.append(f"default {f['default']}")
            print(f"    {f['key']}=  {label}  [{f.get('type', 'text')}{'; ' + '; '.join(extra) if extra else ''}]")


def print_rules(profile: str, rules: list) -> None:
    print(_("notification rules of profile {name}:").format(name=profile))
    if not rules:
        print("  -")
    for r in rules:
        match = ", ".join(f"{k}~{r[k]!r}" for k in ("app", "title", "body") if r.get(k)) or _("every notification")
        print(f"  {r.get('name')}: {match} -> {r.get('pattern')} x{r.get('count')} {scalar(r.get('intensity'))} %"
              f" {r.get('color') or ''} bulbs={r.get('bulbs')} dark={scalar(bool(r.get('dark')), 'follow')}")


def show(cmd: str, result, args) -> int:
    """Print the daemon's reply for the CLI command cmd, readable (or --json)."""
    if isinstance(result, list):  # status, dim, raw: one entry per bulb
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        return print_results(result)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if cmd == "discover":
        print_discovered(result)
    elif cmd == "backends":
        print_backends(result)
    elif cmd in ("clap-sources", "audio-sources"):
        print_sources(result)
    elif cmd == "adapters":
        print_adapters(result)
    elif cmd == "notify-rules":
        print_rules(result.get("profile"), result.get("rules") or [])
    elif cmd == "notify-rules-set":
        print_rules(result.get("profile"), result.get("notify_rules") or [])
    elif cmd == "notify-test":
        print(_("blinking: {bulbs}").format(bulbs=scalar(result.get("notify_test"))))
    elif cmd == "info":
        print_info(result)
    elif cmd == "add":
        print(_("added: {id}").format(id=result.get("added")))
        for b in result.get("bulbs_list") or []:
            if b["id"] == result.get("added"):
                print(bulb_line(b, result.get("in_widget") or ()))
    elif cmd in ("bulbs", "bulb-set", "remove", "order"):
        print_bulbs(result)
    elif cmd == "video-region":
        for b in result.get("bulbs_list") or []:
            if b["id"] == args.percent or b.get("details", {}).get("address") == args.percent:
                print(f"{b['id']}: video_region {b.get('video_region')}")
    elif cmd == "profile":
        for name in result.get("profiles") or []:
            print(f"{'*' if name == result.get('profile') else ' '} {name}")
    elif cmd == "set":
        keys = [w.partition("=")[0].strip() for w in args.words]
        print_fields(result, [k for k in keys if k in result] or SELECTION)
    elif result.get("bulb") == "sync":  # the get object
        print_fields(result, SHOW.get(cmd, SELECTION))
    else:  # clap-meter, clap-hits, audio-status, video-status, notify-status
        print_fields(result)
    return 0


def request(cmd: str, words: list[str], which=None, widget=None) -> dict:
    """One protocol request; widget N: in widget N's context (@N)."""
    if widget is not None:
        return {"cmd": f"@{widget}", "args": [cmd] + words, "which": which}
    return {"cmd": cmd, "arg": " ".join(words) or None, "args": words, "which": which}


def requests_for(args) -> list[tuple[str, list[str]]]:
    """The protocol commands a CLI command sends, in order; the last reply is shown."""
    cmd = args.cmd
    words = [str(w) for w in (getattr(args, "words", None) or [])]
    arg = getattr(args, "percent", None)
    if arg is not None:
        words = [str(arg)] + words
    if cmd in SET_KEYS:
        return [("set", [f"{SET_KEYS[cmd]}={words[0]}"])] if words else [("get", [])]
    if cmd == "range":
        return [("get", [])]  # see cmd_control: the order depends on the present window
    if cmd == "profile" and words[:1] == ["list"]:
        return [("get", [])]
    if cmd == "widget-bulbs":
        ids = [w for w in words if w != "none"]
        return [("widget-bulbs", [",".join(ids)] if ids else [])]
    if cmd == "set":
        return [("set", [w]) for w in words]
    return [(PROTOCOL.get(cmd, cmd), words)]


async def ask(args, cmd: str, words: list[str]) -> dict | None:
    return await via_daemon(request(cmd, words, args.bulb, args.widget))


async def cmd_control(args) -> int:
    reply = None
    for cmd, words in requests_for(args):
        reply = await ask(args, cmd, words)
        if reply is None or "error" in reply:
            break
    if reply is not None and "error" not in reply and args.cmd == "range" and args.lo is not None:
        g = reply["results"][0]
        steps = [("range_lo", args.lo), ("range_hi", args.hi)]
        if args.lo > g.get("range_hi", args.hi):  # never crossing: widen first
            steps.reverse()
        for key, value in steps:
            reply = await ask(args, "set", [f"{key}={value:g}"])
            if "error" in reply:
                break
    if reply is not None:
        if "error" in reply:
            print(reply["error"], file=sys.stderr)
            return 1
        results = reply["results"]
        return show(args.cmd, results if args.cmd in LIST_REPLIES else results[0], args)
    if args.cmd in DAEMON_ONLY:
        print(_("the sync daemon is not running"), file=sys.stderr)
        return 1
    from .backends import mesh
    try:
        words = requests_for(args)[0][1]
        return print_results(await mesh.direct_control(STATE_PATH, args.cmd, " ".join(words) or None, args.bulb))
    except ConnectionError as exc:
        print(f"hauslicht: {exc}", file=sys.stderr)
        return 2


def event_line(ev: dict) -> str:
    """One watch event for reading: time, event, its fields."""
    t = time.strftime("%H:%M:%S", time.localtime(ev.get("t") or time.time()))
    name = ev.get("event", "?")
    if name == "snapshot":
        lines = [f"{t} snapshot: " + _("widget {w}, {n} bulbs").format(
            w=ev.get("widget", 1), n=len(ev.get("bulbs_list") or []))]
        for bid, i in (ev.get("intended") or {}).items():
            lines.append(f"{t} intended {bid}: {compact(i)}")
        return "\n".join(lines)
    rest = {k: v for k, v in ev.items() if k not in ("event", "t", "bulb")}
    return f"{t} {name}" + (f" {ev['bulb']}" if "bulb" in ev else "") + (f": {compact(rest)}" if rest else "")


async def cmd_watch(args) -> int:
    """Print the daemon's events until it closes the connection (or Ctrl+C)."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCK_PATH), limit=1 << 24)
    except (ConnectionRefusedError, FileNotFoundError):
        print(_("the sync daemon is not running"), file=sys.stderr)
        return 1
    writer.write(json.dumps(request("watch", [], None, args.widget)).encode() + b"\n")
    await writer.drain()
    try:
        while line := await reader.readline():
            ev = json.loads(line)
            if "error" in ev:
                print(ev["error"], file=sys.stderr)
                return 1
            print(json.dumps(ev, ensure_ascii=False) if args.json else event_line(ev), flush=True)
    finally:
        writer.close()
    return 0


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
        reader, writer = await asyncio.open_unix_connection(str(SOCK_PATH), limit=1 << 24)
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



def build_parser() -> argparse.ArgumentParser:
    """The command line. Subcommands come in groups (`parser.groups`: [(title,
    [(name, help, subparser)])]); --help lists them by group, and
    dev/gen-man.py writes man/hauslicht.1 from the same structure."""
    import shutil
    import textwrap
    parser = argparse.ArgumentParser(prog="hauslicht", description=_(__doc__.split("\n\n")[0].strip()),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-w", "--widget", type=int, metavar="N", help=_(
        "act in widget N's context: its bulbs, selection, profile and clap mode (default 1; daemon running)"))
    parser.add_argument("--json", action="store_true", help=_(
        "print the daemon's reply as JSON instead of text"))
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND", help=_(
        "one of the commands below; hauslicht COMMAND --help shows its arguments"))
    groups: list[tuple[str, list]] = []

    def group(title: str) -> None:
        groups.append((title, []))

    def add(name: str, help: str) -> argparse.ArgumentParser:
        # no help= on add_parser: argparse would list every command flat; the epilog groups them
        help = help.replace("%%", "%")  # the strings are argparse-safe; description and epilog are not %-formatted
        p = sub.add_parser(name, description=help)
        groups[-1][1].append((name, help, p))
        return p

    bulb_help = _("bulb id like mesh:0002 or unicast address like 0x0002 (default: the selection)")
    onoff = ("on", "off")

    group(_("Switch and dim"))
    helps = {"on": _("switch bulbs on"), "off": _("switch bulbs off"), "status": _("show bulb state")}
    for name in ("on", "off"):
        add(name, helps[name]).add_argument("bulb", nargs="?", help=bulb_help)
    add("toggle", _("switch the bulbs on or off (daemon running)"))
    p = add("dim", _("set brightness in percent, 0 switches off"))
    p.add_argument("percent", type=float)
    p.add_argument("bulb", nargs="?", help=bulb_help)
    p = add("level", _("the widget's brightness slider, 0-100: the profile's level while the screen is ignored, "
                       "otherwise a manual level until resume (daemon running)"))
    p.add_argument("percent", metavar="N", type=float)
    p = add("slider", _("set the slider, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="value", type=float)
    p = add("nudge", _("move the slider by N percent, in mode off the brightness (daemon running)"))
    p.add_argument("percent", metavar="N", type=float)
    for name in ("pause", "resume"):
        add(name, _("stop following the screen") if name == "pause" else _("follow the screen again"))
    p = add("select", _("which bulb the slider, nudge and on/off act on: a bulb id or all (daemon running)"))
    p.add_argument("percent", metavar="ID")
    add("status", helps["status"]).add_argument("bulb", nargs="?", help=bulb_help)
    add("info", _("the widget's state in short: selection, power, profile values, settings, bulbs (daemon running)"))
    add("watch", _("print the daemon's events as they happen, until Ctrl+C (daemon running)"))

    group(_("Follow the screen: mode, profile, colour"))
    p = add("mode", _("how the slider shapes the bulbs (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=MODES)
    p = add("track", _("when the screen drives the bulbs (daemon running)"))
    p.add_argument("percent", metavar="when", nargs="?", choices=TRACKING)
    p = add("off-at-zero", _(
        "a computed 0 %% switches the bulbs dark; off: never below 0.5 %% while on (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=onoff)
    p = add("range", _("the screen window of the selection's profile: LO HI, -100 to 200 (daemon running)"))
    p.add_argument("lo", metavar="LO", nargs="?", type=float)
    p.add_argument("hi", metavar="HI", nargs="?", type=float)
    p = add("curve", _("the curve of the selection's profile, -100 to 100 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    p = add("hue", _("the colour of the selection's profile: hue 0-360 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    p = add("sat", _("the colour of the selection's profile: saturation 0-100, 0 is white (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    p = add("shift", _("the red shift with Night Light, 0-200: 100 as much as the screen, 0 none (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    p = add("follow-ct", _("white bulbs follow Night Light's screen white: on or off (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=onoff)
    p = add("profile", _("profiles: list | save NAME | apply NAME | rename OLD NEW | delete NAME (daemon running)"))
    p.add_argument("percent", metavar="action", choices=("list", "save", "apply", "rename", "delete"))
    p.add_argument("words", metavar="NAME", nargs="*")
    p = add("set", _("set any key as hauslicht-ctl set does: KEY=VALUE ..., see docs/ctl-protocol.md (daemon running)"))
    p.add_argument("words", metavar="KEY=VALUE", nargs="+")

    group(_("Bulbs and widgets"))
    add("list", _("list the bulbs"))
    add("bulbs", _("every bulb with its use, widget, state, icons, audio/video and screen region (daemon running)"))
    p = add("bulb-set", _("set a bulb's name=, profile=, use=on|off, icon_on=, icon_off=, icon_disconnected= "
                          "(daemon running)"))
    p.add_argument("percent", metavar="ID")
    p.add_argument("words", metavar="key=value", nargs="+")
    p = add("order", _("the list order: every bulb id once (daemon running)"))
    p.add_argument("words", metavar="ID", nargs="+")
    p = add("widgets", _("how many widgets, 1-8 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=int, choices=range(1, 9))
    p = add("widget-bulbs", _("the bulbs of the widget (-w N), kept in list order; none: no bulb (daemon running)"))
    p.add_argument("words", metavar="ID", nargs="*")
    p = add("identify", _(
        "blink a bulb slowly to find it, off: stop; it stops by itself after 2 minutes (daemon running)"))
    p.add_argument("percent", metavar="BULB")
    p.add_argument("words", metavar="on|off", nargs="*")
    p = add("test", _(
        "test a bulb: brightest, a fade, colours, then on/off fast to slow, about 25 s; flashes quickly, "
        "not for people sensitive to flashing light; stop: end it (daemon running)"))
    p.add_argument("percent", metavar="BULB")
    p.add_argument("words", metavar="stop", nargs="*")

    group(_("Add and remove bulbs"))
    p = add("discover", _("look for new bulbs, optionally of one kind or every Bluetooth kind (daemon running)"))
    p.add_argument("percent", metavar="KIND", nargs="?")
    p = add("add", _("add a bulb: KIND key=value ... (daemon running)"))
    p.add_argument("percent", metavar="KIND")
    p.add_argument("words", metavar="key=value", nargs="*")
    p = add("remove", _("stop controlling a bulb (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p = add("backends", _("list the kinds of bulbs and what they need (daemon running)"))
    p.add_argument("--requires", metavar="KIND[,KIND...]", help=_(
        "print the pip packages these kinds need (all: every kind), one per line; no daemon needed"))
    p = add("autoconnect", _(
        "add newly found devices by itself, never mesh bulbs; may take over a neighbour's lights (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=onoff)
    p = add("unignore", _("let autoconnect add a removed device again: its id or address (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p = add("adopt", _("take a factory-fresh bulb into the mesh"))
    p.add_argument("--scan", type=float, default=60.0, help=_("seconds to wait for a fresh bulb"))
    p.add_argument("--verbose", action="store_true")
    p = add("setup", _("bind keys and models on adopted bulbs again"))
    p.add_argument("bulb", nargs="?", help=_("unicast address like 0x0002 (default: all)"))

    group(_("Wall switch, lid and monitor"))
    p = add("flick", _(
        "wall-switch flicks: off | on | fast | exclusive; fast and exclusive need the system setup (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=FLICK_MODES)
    add("adapters", _("list the Bluetooth adapters for flick mode exclusive (daemon running)"))
    p = add("exclusive-adapter", _(
        "the Bluetooth adapter flick mode exclusive takes: hciN, its address or auto (daemon running)"))
    p.add_argument("percent", metavar="ADAPTER", nargs="?")
    p = add("lid", _("what lid events do: close (off), open (on), both, never (daemon running)"))
    p.add_argument("percent", metavar="when", nargs="?", choices=LID_EVENTS)
    p = add("monitor-power", _(
        "desktops: lights off when you switch the monitor off (not on standby), on when it returns (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=onoff)

    group(_("Clap and follow audio"))
    p = add("clap", _("a double clap or snap toggles the widget's lights: off | clap | snap | both (daemon running)"))
    p.add_argument("percent", metavar="mode", nargs="?", choices=("off", "clap", "snap", "both"))
    p = add("clap-source", _("the microphone for clap detection: a source name or default (daemon running)"))
    p.add_argument("percent", metavar="NAME", nargs="?")
    p = add("clap-sensitivity", _("clap detection sensitivity, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    add("clap-sources", _("list the microphones (daemon running)"))
    add("clap-meter", _("clap detection: level, last hit with class and score, last double (daemon running)"))
    add("clap-hits", _("clap detection: the last 30 hits with gate numbers, scores and verdict (daemon running)"))
    p = add("audio", _("the selection (or one bulb) follows what the computer plays: loudness to brightness, "
                       "pitch to colour (daemon running)"))
    p.add_argument("percent", metavar="on|off|toggle", choices=("on", "off", "toggle"))
    p.add_argument("bulb", nargs="?", help=bulb_help)
    p = add("audio-source", _(
        "what follow-audio listens to: a source name or default, the default output's monitor (daemon running)"))
    p.add_argument("percent", metavar="NAME", nargs="?")
    p = add("audio-color", _("follow-audio: pitch colours the bulbs, on or off (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=onoff)
    p = add("audio-rate", _(
        "follow-audio: updates per second per bulb, 1-3; Bluetooth mesh at most 2 recommended (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=int, choices=(1, 2, 3))
    add("audio-sources", _("list the sources follow-audio can listen to, monitors included (daemon running)"))
    add("audio-status", _("follow-audio: capture state, level, pitch and the messages sent this run (daemon running)"))

    group(_("Follow video"))
    p = add("video", _("the selection (or one bulb) follows its region of the screen: luminance to brightness, "
                       "colour to hue (daemon running)"))
    p.add_argument("percent", metavar="on|off|toggle", choices=("on", "off", "toggle"))
    p.add_argument("bulb", nargs="?", help=bulb_help)
    p = add("video-region", _(
        "the part of the screen a bulb follows: whole, left, right, top, bottom, top-left, top-right, "
        "bottom-left or bottom-right (daemon running)"))
    p.add_argument("percent", metavar="ID")
    p.add_argument("words", metavar="REGION", nargs=1)
    p = add("video-max", _("follow-video: the brightness of the brightest screen, 0-100 (daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=float)
    p = add("video-smooth", _("follow-video: time averaging in seconds, 0, 0.2, 0.5, 1, 2 or 5 (daemon running)"))
    p.add_argument("percent", metavar="S", nargs="?", type=float)
    add("video-forget", _("follow-video: forget the screen permission; the next start asks again (daemon running)"))
    add("video-status", _("follow-video: capture state, frames and the messages sent this run (daemon running)"))
    p = add("follow-rate", _(
        "follow audio and video: updates per second per bulb, 1-3; Bluetooth mesh at most 2 recommended "
        "(daemon running)"))
    p.add_argument("percent", metavar="N", nargs="?", type=int, choices=(1, 2, 3))

    group(_("Notification blinks"))
    p = add("notify", _(
        "desktop notifications blink the bulbs by the profile's rules: on or off, default off (daemon running)"))
    p.add_argument("percent", metavar="on|off", nargs="?", choices=onoff)
    add("notify-rules", _("the notification rules of the selection's profile (daemon running)"))
    p = add("notify-rules-set", _(
        "replace the notification rules of the selection's profile: one JSON list (daemon running)"))
    p.add_argument("percent", metavar="JSON")
    p = add("notify-test", _("run a notification rule's pattern now (daemon running)"))
    p.add_argument("percent", metavar="RULE")
    p.add_argument("words", metavar="RULE", nargs="*")
    add("notify-status", _("notification blinks: watching or not, Do Not Disturb, the last match (daemon running)"))

    group(_("Service and setup"))
    p = add("sync", _("daemon: follow the screen, provide the Plasma slider"))
    p.add_argument("--interval", type=float, default=0.3, help=_("seconds between backlight reads"))
    p.add_argument("--no-desktop-writes", action="store_true", help=_(
        "test instance: no keyboard-slider LED, no PowerDevil restart, no suspend inhibitor"))
    add("setup-root", _("the one-time system setup: sudo on a terminal, otherwise the daemon asks via pkexec"))
    add("setup-status", _("the system setup: missing, outdated or current, read anew (daemon running)"))
    p = add("disarm", _(
        "no daemon: OnPowerUp back to \"on\" so that the wall switch just switches on (the service's stop)"))
    p.add_argument("--if-armed", action="store_true", help=_(
        "only the bulbs the daemon left armed (its marker); nothing at all when there are none"))
    add("debug", _("open a window showing each bulb's intended state and the daemon's events, live (PyQt6)"))
    p = add("adverts", _("debug: print the bulbs' adverts with timestamps"))
    p.add_argument("seconds", type=float, nargs="?", default=30.0)
    p = add("raw", _("send one hex access payload, print replies"))
    p.add_argument("percent", metavar="hex")
    p.add_argument("bulb", nargs="?", help=bulb_help)

    width = max(60, min(shutil.get_terminal_size().columns, 100))
    lines = []
    for title, cmds in groups:
        lines.append(f"{title}:")
        for name, help, _p in cmds:
            text = textwrap.wrap(help, width - 22) or [""]
            lines.append(f"  {name:<18}  {text[0]}" if len(name) <= 18 else f"  {name}\n{'':<22}{text[0]}")
            lines += [f"{'':<22}{t}" for t in text[1:]]
        lines.append("")
    lines += textwrap.wrap(_("While the daemon runs it owns the Bluetooth link; the other commands then go to it over "
                             "its socket. man hauslicht: files, environment, exit status, examples."), width)
    parser.epilog = "\n".join(lines)
    parser.groups = groups
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "bulb"):
        args.bulb = None
    if args.cmd == "range" and (args.lo is None) != (args.hi is None):
        parser.error(_("range needs both LO and HI, or neither"))
    if args.cmd == "profile" and args.percent != "list" and not args.words:
        parser.error(_("profile {action} needs a NAME").format(action=args.percent))

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
    if args.cmd == "watch":
        try:
            return asyncio.run(cmd_watch(args))
        except KeyboardInterrupt:
            return 0
    return asyncio.run(cmd_control(args))
