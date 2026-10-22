#!/usr/bin/env python3
"""Write man/hauslicht.1 and man/de/hauslicht.1 from the argparse parser.

    .venv/bin/python dev/gen-man.py          (re)generate both pages
    .venv/bin/python dev/gen-man.py --check  exit 1 when a page is out of date

The commands, their arguments and help texts come from haus.cli.build_parser()
(German: the same parser under LANGUAGE=de, so haus/common.py's GERMAN table
translates them); the fixed sections (description, files, environment, exit
status, examples) are below. argparse-manpage was tried first: it lists
subcommands flat (no groups) and knows no second language, so this small
generator writes the roff itself. man/hauslicht-ctl.1 and its German page are
written by hand (the ctl client has no parser).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = {"en": ROOT / "man" / "hauslicht.1", "de": ROOT / "man" / "de" / "hauslicht.1"}

TEXT = {
    "en": {
        "section": "User Commands",
        "name": "hauslicht \\- lights that follow the screen (Bluetooth mesh and more)",
        "description": [
            "\\fBhauslicht\\fR makes lights follow the screen: the bulbs dim and brighten with the screen "
            "backlight, switch with the lid or the monitor, follow what the computer plays or shows, blink on "
            "notifications and react to a double clap or a wall-switch flick.",
            "The daemon, \\fBhauslicht sync\\fR (normally run by the systemd user unit), owns the Bluetooth link "
            "and the mesh sequence number. While it runs, the other commands go to it over its Unix socket "
            "(the protocol is in \\fBhauslicht-ctl\\fR(1)). Without the daemon, \\fBon\\fR, \\fBoff\\fR, "
            "\\fBdim\\fR, \\fBstatus\\fR and \\fBraw\\fR talk to Bluetooth mesh bulbs directly; the commands "
            "marked \\(lqdaemon running\\(rq need it.",
            "The Plasma widget offers the same features. Its pure display preferences (the icon set, inverted "
            "scrolling on the tray icon) are kept by the widget and have no command. Replies are printed as "
            "text; \\fB\\-\\-json\\fR prints the daemon's reply as JSON for scripts.",
        ],
        "options": "OPTIONS", "commands": "COMMANDS", "files": "FILES", "environment": "ENVIRONMENT",
        "exit": "EXIT STATUS", "examples_h": "EXAMPLES", "see": "SEE ALSO", "synopsis": "SYNOPSIS",
        "desc": "DESCRIPTION", "name_h": "NAME",
        "help": "show the help and exit", "command": "COMMAND", "args": "ARGS",
        "files_list": [
            ("hauslicht.json", "The state file, in the checkout (or \\fB$HAUSLICHT_STATE\\fR): bulbs, mesh keys "
             "and sequence number, profiles, widgets, settings. Readable by its owner only; it holds the mesh "
             "keys, so never share it."),
            ("hauslicht.armed.json", "The armed marker beside the state file: the bulbs whose OnPowerUp the "
             "daemon may have left armed to something other than \\(lqon\\(rq. \\fBdisarm \\-\\-if\\-armed\\fR "
             "reads it."),
            ("hauslicht.lock", "Beside the state file: the lock that guards the mesh link and sequence number."),
            ("$XDG_RUNTIME_DIR/hauslicht.sock", "The daemon's socket (or \\fB$HAUSLICHT_SOCK\\fR)."),
            ("~/.config/systemd/user/hauslicht.service", "The unit: \\fBhauslicht sync\\fR, and "
             "\\fBhauslicht disarm \\-\\-if\\-armed\\fR after every stop. Written by \\fBinstall/install.sh\\fR."),
            ("~/.cache/hauslicht/", "The clap model, downloaded on first use."),
            ("/usr/local/lib/hauslicht/setup-version", "The stamp of the one-time system setup "
             "(\\fBsetup\\-root\\fR)."),
        ],
        "env_list": [
            ("HAUSLICHT_STATE", "Another state file (a test instance)."),
            ("HAUSLICHT_SOCK", "Another daemon socket (a test instance). Keep the path short."),
            ("HAUSLICHT_BACKENDS", "A comma list of the kinds of bulbs to load, e.g. \\fBmock\\fR for a test "
             "instance that never touches Bluetooth."),
            ("HAUSLICHT_MOCK", "\\fB1\\fR shows the mock backend even when \\fBHAUSLICHT_BACKENDS\\fR does not "
             "name it."),
            ("HAUSLICHT_DEBUG", "Set: \\fBsync\\fR logs at debug level."),
            ("HAUSLICHT_NO_PANEL", "\\fB1\\fR: the daemon adds no panel widgets, \\fB0\\fR: it does; unset: "
             "it does, except for a test instance."),
            ("LANGUAGE, LC_ALL, LC_MESSAGES, LANG", "German messages when the language is \\fBde\\fR, "
             "English otherwise."),
        ],
        "exit_list": [
            ("0", "Success."),
            ("1", "The daemon is not running, it refused the command (the message goes to standard error), "
             "a bulb did not answer, or a test ended early."),
            ("2", "A usage error, or the Bluetooth link failed without the daemon."),
        ],
        "examples": [
            ("Switch the lights on and dim them to 40 percent:", "hauslicht on\nhauslicht dim 40"),
            ("What the widget shows, and every bulb:", "hauslicht info\nhauslicht bulbs"),
            ("Up to 150 percent of the screen's brightness, warm colour:", "hauslicht range 0 150\n"
             "hauslicht hue 30\nhauslicht sat 60"),
            ("Two widgets, the second for the shelf lamp:", "hauslicht widgets 2\n"
             "hauslicht \\-w 2 widget\\-bulbs mesh:0003"),
            ("Find, then add a bulb:", "hauslicht discover bluetooth\nhauslicht backends\n"
             "hauslicht add wiz address=192.168.1.40"),
            ("A test instance beside the live one:", "export HAUSLICHT_STATE=/tmp/hl.json "
             "HAUSLICHT_SOCK=$XDG_RUNTIME_DIR/hl.sock\nHAUSLICHT_BACKENDS=mock hauslicht sync "
             "\\-\\-no\\-desktop\\-writes &\nhauslicht add mock name=Desk"),
        ],
        "see_also": "\\fBhauslicht\\-ctl\\fR(1), \\fBsystemctl\\fR(1); docs/ctl\\-protocol.md and README.md in "
                    "the checkout.",
    },
    "de": {
        "section": "Benutzerbefehle",
        "name": "hauslicht \\- Lampen, die dem Bildschirm folgen (Bluetooth Mesh und mehr)",
        "description": [
            "\\fBhauslicht\\fR lässt Lampen dem Bildschirm folgen: sie werden mit der Bildschirmhelligkeit "
            "heller und dunkler, schalten mit dem Deckel oder dem Monitor, folgen dem, was der Computer "
            "abspielt oder zeigt, blinken bei Benachrichtigungen und reagieren auf doppeltes Klatschen oder "
            "kurzes Aus- und Einschalten am Wandschalter.",
            "Der Dienst, \\fBhauslicht sync\\fR (normalerweise von der systemd-Benutzer-Unit gestartet), hält "
            "die Bluetooth-Verbindung und die Mesh-Sequenznummer. Während er läuft, gehen die anderen Befehle "
            "über seinen Unix-Socket an ihn (das Protokoll steht in \\fBhauslicht-ctl\\fR(1)). Ohne Dienst "
            "sprechen \\fBon\\fR, \\fBoff\\fR, \\fBdim\\fR, \\fBstatus\\fR und \\fBraw\\fR Bluetooth-Mesh-Lampen "
            "direkt an; die mit \\(BqDienst läuft\\(lq markierten Befehle brauchen ihn.",
            "Das Plasma-Widget bietet dieselben Funktionen. Seine reinen Anzeige-Einstellungen (der Symbolsatz, "
            "umgekehrtes Scrollen auf dem Tray-Symbol) verwaltet das Widget selbst; dafür gibt es keinen Befehl. "
            "Antworten erscheinen als Text; \\fB\\-\\-json\\fR gibt die Antwort des Dienstes für Skripte als "
            "JSON aus.",
        ],
        "options": "OPTIONEN", "commands": "BEFEHLE", "files": "DATEIEN", "environment": "UMGEBUNG",
        "exit": "RÜCKGABEWERT", "examples_h": "BEISPIELE", "see": "SIEHE AUCH", "synopsis": "ÜBERSICHT",
        "desc": "BESCHREIBUNG", "name_h": "BEZEICHNUNG",
        "help": "die Hilfe zeigen und beenden", "command": "BEFEHL", "args": "ARGUMENTE",
        "files_list": [
            ("hauslicht.json", "Die Zustandsdatei, im Checkout (oder \\fB$HAUSLICHT_STATE\\fR): Lampen, "
             "Mesh-Schlüssel und Sequenznummer, Profile, Widgets, Einstellungen. Nur für den Besitzer lesbar; "
             "sie enthält die Mesh-Schlüssel, also nie weitergeben."),
            ("hauslicht.armed.json", "Die Scharf-Markierung neben der Zustandsdatei: die Lampen, deren "
             "OnPowerUp der Dienst auf etwas anderes als \\(Bqon\\(lq gestellt haben kann. "
             "\\fBdisarm \\-\\-if\\-armed\\fR liest sie."),
            ("hauslicht.lock", "Neben der Zustandsdatei: die Sperre für die Mesh-Verbindung und die "
             "Sequenznummer."),
            ("$XDG_RUNTIME_DIR/hauslicht.sock", "Der Socket des Dienstes (oder \\fB$HAUSLICHT_SOCK\\fR)."),
            ("~/.config/systemd/user/hauslicht.service", "Die Unit: \\fBhauslicht sync\\fR, und nach jedem "
             "Stopp \\fBhauslicht disarm \\-\\-if\\-armed\\fR. Von \\fBinstall/install.sh\\fR geschrieben."),
            ("~/.cache/hauslicht/", "Das Klatsch-Modell, beim ersten Gebrauch heruntergeladen."),
            ("/usr/local/lib/hauslicht/setup-version", "Der Stempel der einmaligen Systemeinrichtung "
             "(\\fBsetup\\-root\\fR)."),
        ],
        "env_list": [
            ("HAUSLICHT_STATE", "Eine andere Zustandsdatei (eine Testinstanz)."),
            ("HAUSLICHT_SOCK", "Ein anderer Socket des Dienstes (eine Testinstanz). Den Pfad kurz halten."),
            ("HAUSLICHT_BACKENDS", "Eine Komma-Liste der Lampenarten, die geladen werden, z.\\~B. \\fBmock\\fR "
             "für eine Testinstanz, die Bluetooth nie anfasst."),
            ("HAUSLICHT_MOCK", "\\fB1\\fR zeigt das Mock-Backend, auch wenn \\fBHAUSLICHT_BACKENDS\\fR es nicht "
             "nennt."),
            ("HAUSLICHT_DEBUG", "Gesetzt: \\fBsync\\fR protokolliert auf Debug-Stufe."),
            ("HAUSLICHT_NO_PANEL", "\\fB1\\fR: der Dienst legt keine Panel-Widgets an, \\fB0\\fR: er tut es; "
             "nicht gesetzt: er tut es, außer in einer Testinstanz."),
            ("LANGUAGE, LC_ALL, LC_MESSAGES, LANG", "Deutsche Meldungen bei der Sprache \\fBde\\fR, sonst "
             "englische."),
        ],
        "exit_list": [
            ("0", "Erfolg."),
            ("1", "Der Dienst läuft nicht, er hat den Befehl abgelehnt (die Meldung geht auf die "
             "Standardfehlerausgabe), eine Lampe hat nicht geantwortet, oder ein Test endete vorzeitig."),
            ("2", "Ein Bedienfehler, oder die Bluetooth-Verbindung ohne Dienst schlug fehl."),
        ],
        "examples": [
            ("Licht einschalten und auf 40 Prozent dimmen:", "hauslicht on\nhauslicht dim 40"),
            ("Was das Widget zeigt, und jede Lampe:", "hauslicht info\nhauslicht bulbs"),
            ("Bis 150 Prozent der Bildschirmhelligkeit, warme Farbe:", "hauslicht range 0 150\n"
             "hauslicht hue 30\nhauslicht sat 60"),
            ("Zwei Widgets, das zweite für die Regallampe:", "hauslicht widgets 2\n"
             "hauslicht \\-w 2 widget\\-bulbs mesh:0003"),
            ("Eine Lampe suchen, dann hinzufügen:", "hauslicht discover bluetooth\nhauslicht backends\n"
             "hauslicht add wiz address=192.168.1.40"),
            ("Eine Testinstanz neben der laufenden:", "export HAUSLICHT_STATE=/tmp/hl.json "
             "HAUSLICHT_SOCK=$XDG_RUNTIME_DIR/hl.sock\nHAUSLICHT_BACKENDS=mock hauslicht sync "
             "\\-\\-no\\-desktop\\-writes &\nhauslicht add mock name=Desk"),
        ],
        "see_also": "\\fBhauslicht\\-ctl\\fR(1), \\fBsystemctl\\fR(1); docs/ctl\\-protocol.md und README.md im "
                    "Checkout.",
    },
}


def esc(text: str) -> str:
    """Plain text for roff: backslashes, and a leading dot or quote."""
    text = text.replace("\\", "\\e")
    return re.sub(r"^([.'])", r"\\&\1", text, flags=re.M)


def opt(text: str) -> str:
    """An option or command word: hyphens as \\- (minus, copy-paste safe)."""
    return esc(text).replace("-", "\\-")


def args_of(p: argparse.ArgumentParser) -> str:
    """The subcommand's argument synopsis, from its usage line."""
    usage = " ".join(p.format_usage().split())
    usage = usage.split(p.prog, 1)[1].replace("[-h]", "").strip()
    return opt(usage)


def option_tag(action: argparse.Action) -> str:
    names = ", ".join(f"\\fB{opt(s)}\\fR" for s in action.option_strings)
    if action.nargs != 0:
        names += f" \\fI{opt(action.metavar or action.dest.upper())}\\fR"
    return names


def page(lang: str, version: str, date: str) -> str:
    from haus import cli
    t = TEXT[lang]
    parser = cli.build_parser()
    out = [f'.TH HAUSLICHT 1 "{date}" "Haus.Licht {version}" "{t["section"]}"',
           f'.SH {t["name_h"]}', t["name"], f'.SH {t["synopsis"]}', ".B hauslicht",
           f"[\\fB\\-w\\fR \\fIN\\fR] [\\fB\\-\\-json\\fR] \\fI{t['command']}\\fR [\\fI{t['args']}\\fR ...]",
           f'.SH {t["desc"]}']
    for i, para in enumerate(t["description"]):
        out += ([".PP"] if i else []) + [para]
    out.append(f'.SH {t["options"]}')
    for action in parser._actions:
        if action.option_strings:
            help_text = t["help"] if action.dest == "help" else action.help
            out += [".TP", option_tag(action), esc(help_text)]
    out.append(f'.SH {t["commands"]}')
    for title, cmds in parser.groups:
        out.append(f".SS {esc(title)}")
        for name, help_text, p in cmds:
            args = args_of(p)
            out += [".TP", f"\\fB{opt(name)}\\fR" + (f" {args}" if args else ""), esc(help_text)]
            for action in p._actions:
                if action.option_strings and action.dest != "help":
                    out += [".RS", ".TP", option_tag(action), esc(action.help or ""), ".RE"]
    out.append(f'.SH {t["files"]}')
    for path, text in t["files_list"]:
        out += [".TP", f"\\fI{opt(path)}\\fR", text]
    out.append(f'.SH {t["environment"]}')
    for name, text in t["env_list"]:
        out += [".TP", f"\\fB{name}\\fR", text]
    out.append(f'.SH {t["exit"]}')
    for code, text in t["exit_list"]:
        out += [".TP", f"\\fB{code}\\fR", text]
    out.append(f'.SH {t["examples_h"]}')
    for text, cmd in t["examples"]:
        out += [".PP", text, ".PP", ".RS", ".nf", cmd, ".fi", ".RE"]
    out += [f'.SH {t["see"]}', t["see_also"]]
    text = "\n".join(out) + "\n"
    # ASCII only: groff reads plain input as Latin-1 unless run with -k (preconv)
    return "".join(c if ord(c) < 128 else f"\\[u{ord(c):04X}]" for c in text)


def version() -> str:
    meta = json.loads((ROOT / "plasmoid" / "haus.licht.tray" / "metadata.json").read_text())
    return meta["KPlugin"]["Version"]


def main() -> int:
    ap = argparse.ArgumentParser(description="write man/hauslicht.1 and man/de/hauslicht.1 from the parser")
    ap.add_argument("--check", action="store_true", help="exit 1 when a page is out of date (dates ignored)")
    ap.add_argument("--lang", help=argparse.SUPPRESS)  # the child process: print one page
    args = ap.parse_args()
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    date = (datetime.datetime.fromtimestamp(int(epoch), datetime.UTC).date() if epoch
            else datetime.date.today()).isoformat()
    if args.lang:
        sys.path.insert(0, str(ROOT))
        sys.stdout.write(page(args.lang, version(), date))
        return 0
    stale = []
    for lang, path in PAGES.items():
        # a child per language: haus.common picks the language at import
        env = dict(os.environ, LANGUAGE=lang, LC_ALL="C.UTF-8", LANG="C.UTF-8", COLUMNS="100")
        text = subprocess.run([sys.executable, __file__, "--lang", lang], env=env, check=True,
                              capture_output=True, text=True).stdout
        undated = lambda s: re.sub(r'^(\.TH \S+ \S+) "[^"]*"', r"\1", s, count=1)  # noqa: E731
        if args.check:
            if not path.exists() or undated(path.read_text()) != undated(text):
                stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("out of date: " + ", ".join(stale) + " (run dev/gen-man.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
