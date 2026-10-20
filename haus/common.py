"""Paths, translations and the state file, shared by the core, the backends and the CLI."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the checkout
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # provision_and_toggle.py lives there

# HAUSLICHT_STATE / HAUSLICHT_SOCK: a second instance (tests, widget work) beside the live one.
STATE_PATH = Path(os.environ.get("HAUSLICHT_STATE") or ROOT / "hauslicht.json")
SOCK_PATH = Path(os.environ.get("HAUSLICHT_SOCK")
                 or Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hauslicht.sock")


def lock_path(state_path: Path) -> Path:
    """One lock per state file: it guards the mesh SEQ counter kept there."""
    return state_path.with_suffix(".lock")


class UserError(Exception):
    """A refusal worth showing as it is (no exception type in front)."""


# ------------------------------------------------------------- i18n

GERMAN = {
    "hauslicht: control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux.":
        "hauslicht: Ledvance-Volkslicht-Lampen (Bluetooth SIG Mesh) unter Linux steuern.",
    "hauslicht: lights that follow the laptop screen (Bluetooth mesh and more).":
        "hauslicht: Lampen, die dem Laptop-Bildschirm folgen (Bluetooth Mesh und mehr).",
    "no {name} yet: run `hauslicht adopt` first": "noch keine {name}: zuerst `hauslicht adopt` ausführen",
    "unknown bulb {which}; known: {known}": "unbekannte Lampe {which}; bekannt: {known}",
    "another hauslicht process holds the mesh link (for adopt/setup: systemctl --user stop hauslicht first)":
        "ein anderer hauslicht-Prozess hält die Mesh-Verbindung "
        "(für adopt/setup zuerst: systemctl --user stop hauslicht)",
    "the sync daemon is not running": "der Sync-Dienst läuft nicht",
    "{target} is not on the ignore list": "{target} steht nicht auf der Ignorierliste",
    "(no answer: switched off at the wall?)": "(keine Antwort: am Wandschalter aus?)",
    "bulb {addr} adopted. Try: hauslicht dim 30 {addr}": "Lampe {addr} übernommen. Probieren: hauslicht dim 30 {addr}",
    "no backlight device in /sys/class/backlight": "keine Hintergrundbeleuchtung in /sys/class/backlight",
    "unicast address like 0x0002 (default: all)": "Unicast-Adresse wie 0x0002 (Standard: alle)",
    "bulb id like mesh:0002 or unicast address like 0x0002 (default: the selection)":
        "Lampen-ID wie mesh:0002 oder Unicast-Adresse wie 0x0002 (Standard: die Auswahl)",
    "send one hex access payload, print replies": "eine Hex-Nachricht senden, Antworten ausgeben",
    "seconds to wait for a fresh bulb": "Sekunden Wartezeit auf eine frische Lampe",
    "how the slider shapes the bulbs (daemon running)": "wie der Regler die Lampen formt (Dienst läuft)",
    "set the slider, 0-100 (daemon running)": "Regler setzen, 0-100 (Dienst läuft)",
    "when the screen drives the bulbs (daemon running)": "wann der Bildschirm die Lampen steuert (Dienst läuft)",
    "what lid events do: close (off), open (on), both, never (daemon running)":
        "was der Deckel schaltet: close (aus), open (ein), both, never (Dienst läuft)",
    "a computed 0 %% switches the bulbs dark; off: never below 0.5 %% while on (daemon running)":
        "ein berechnetes 0 %% schaltet die Lampen dunkel; aus: eingeschaltet nie unter 0,5 %% (Dienst läuft)",
    "move the slider by N percent, in mode off the brightness (daemon running)":
        "Regler um N Prozent verschieben, im Modus off die Helligkeit (Dienst läuft)",
    "switch the bulbs on or off (daemon running)": "Lampen ein- oder ausschalten (Dienst läuft)",
    "print bulb %% against screen %% for a mode": "Lampen-%% über Bildschirm-%% für einen Modus ausgeben",
    "seconds between backlight reads": "Sekunden zwischen zwei Helligkeitsabfragen",
    "switch bulbs on": "Lampen einschalten",
    "switch bulbs off": "Lampen ausschalten",
    "show bulb state": "Zustand der Lampen anzeigen",
    "set brightness in percent, 0 switches off": "Helligkeit in Prozent, 0 schaltet aus",
    "stop following the screen": "dem Bildschirm nicht mehr folgen",
    "follow the screen again": "dem Bildschirm wieder folgen",
    "list adopted bulbs": "übernommene Lampen auflisten",
    "list the bulbs": "die Lampen auflisten",
    "take a factory-fresh bulb into the mesh": "eine zurückgesetzte Lampe ins Mesh aufnehmen",
    "bind keys and models on adopted bulbs again": "Schlüssel und Modelle der Lampen neu binden",
    "daemon: follow the screen, provide the Plasma slider": "Dienst: dem Bildschirm folgen, Plasma-Regler bereitstellen",
    "debug: print the bulbs' adverts with timestamps": "Debug: die Advertisements der Lampen mit Zeitstempel ausgeben",
    "test instance: no keyboard-slider LED, no PowerDevil restart, no suspend inhibitor":
        "Testinstanz: keine Tastatur-Regler-LED, kein PowerDevil-Neustart, keine Ruhezustand-Sperre",
    "which bulb the slider, nudge and on/off act on: a bulb id or all (daemon running)":
        "auf welche Lampe Regler, nudge und ein/aus wirken: eine Lampen-ID oder all (Dienst läuft)",
    "rename a bulb or give it a profile: ID name=... profile=... (daemon running)":
        "eine Lampe umbenennen oder ihr ein Profil geben: ID name=... profile=... (Dienst läuft)",
    "profiles: save NAME | apply NAME | rename OLD NEW | delete NAME (daemon running)":
        "Profile: save NAME | apply NAME | rename ALT NEU | delete NAME (Dienst läuft)",
    "look for new bulbs, optionally of one kind (daemon running)":
        "nach neuen Lampen suchen, wahlweise nur einer Art (Dienst läuft)",
    "add a bulb: KIND key=value ... (daemon running)": "eine Lampe hinzufügen: ART Schlüssel=Wert ... (Dienst läuft)",
    "stop controlling a bulb (daemon running)": "eine Lampe nicht mehr steuern (Dienst läuft)",
    "add newly found devices by itself, never mesh bulbs; may take over a neighbour's lights (daemon running)":
        "neu gefundene Geräte selbst hinzufügen, nie Mesh-Lampen; kann Lampen der Nachbarn übernehmen (Dienst läuft)",
    "a double clap or snap toggles the lights: off | clap | snap | both (daemon running)":
        "doppeltes Klatschen oder Fingerschnippen schaltet das Licht um: off | clap | snap | both (Dienst läuft)",
    "the microphone for clap detection: a source name or default (daemon running)":
        "das Mikrofon für die Klatscherkennung: ein Quellenname oder default (Dienst läuft)",
    "clap detection sensitivity, 0-100 (daemon running)": "Empfindlichkeit der Klatscherkennung, 0-100 (Dienst läuft)",
    "list the microphones (daemon running)": "die Mikrofone auflisten (Dienst läuft)",
    "clap detection: level, last hit with class and score, last double (daemon running)":
        "Klatscherkennung: Pegel, letzter Treffer mit Klasse und Wert, letztes Doppel (Dienst läuft)",
    "clap detection is not installed ({missing}): {cmd}": "Klatscherkennung ist nicht installiert ({missing}): {cmd}",
    "let autoconnect add a removed device again: its id or address (daemon running)":
        "ein entferntes Gerät wieder automatisch verbinden lassen: seine ID oder Adresse (Dienst läuft)",
    "list the kinds of bulbs and what they need (daemon running)":
        "die Lampenarten auflisten und was sie brauchen (Dienst läuft)",
    "wall-switch flicks: off | on | fast | exclusive; fast and exclusive need the system setup (daemon running)":
        "Wandschalter-Flicks: off | on | fast | exclusive; fast und exclusive brauchen die Systemeinrichtung (Dienst läuft)",
    "the one-time system setup: sudo on a terminal, otherwise the daemon asks via pkexec":
        "die einmalige Systemeinrichtung: im Terminal per sudo, sonst fragt der Dienst per pkexec",
    "flick mode {mode} needs the system setup first (sudo; Bluetooth restarts)":
        "Flick-Modus {mode} braucht erst die Systemeinrichtung (sudo; Bluetooth startet neu)",
    "system setup failed: flick mode unchanged": "Systemeinrichtung fehlgeschlagen: Flick-Modus unverändert",
    "cancelled": "abgebrochen",
    "not allowed, or no password prompt available": "nicht erlaubt, oder keine Passwortabfrage verfügbar",
    "failed ({last})": "fehlgeschlagen ({last})",
    "flick mode {mode} needs the system setup first: hauslicht setup-root":
        "Flick-Modus {mode} braucht erst die Systemeinrichtung: hauslicht setup-root",
    "System setup {why}.": "Systemeinrichtung {why}.",
    "Exclusive mode unavailable ({why}): working like Fast.":
        "Exklusiv-Modus nicht verfügbar ({why}): wirkt wie Schnell.",
    "list the Bluetooth adapters for flick mode exclusive (daemon running)":
        "die Bluetooth-Adapter für den Flick-Modus exclusive auflisten (Dienst läuft)",
    "mesh bulbs are adopted with `hauslicht adopt` while the service is stopped":
        "Mesh-Lampen werden mit `hauslicht adopt` aufgenommen, während der Dienst gestoppt ist",
    "unknown kind of bulb {kind}; known: {known}": "unbekannte Lampenart {kind}; bekannt: {known}",
    "{kind} needs {missing}: pip install it into the venv": "{kind} braucht {missing}: per pip in die venv installieren",
    "{label} is required": "{label} fehlt",
    "unknown profile {name}": "unbekanntes Profil {name}",
    "the default profile cannot be deleted or renamed": "das Standardprofil kann nicht gelöscht oder umbenannt werden",
    "a profile named {name} exists already": "ein Profil namens {name} gibt es schon",
    "{bid} is added already": "{bid} ist schon hinzugefügt",
    "no widget {w} (widgets: 1-{n})": "kein Widget {w} (Widgets: 1-{n})",
    "{bid} is not in use": "{bid} wird nicht verwendet",
    "{bid} is not in widget {w}": "{bid} ist nicht in Widget {w}",
    "widgets must be a number from 1 to 8": "widgets muss eine Zahl von 1 bis 8 sein",
    "use must be on or off": "use muss on oder off sein",
    "an icon is an icon name or an absolute file path": "ein Symbol ist ein Symbolname oder ein absoluter Dateipfad",
    "order needs every bulb id once: {known}": "order braucht jede Lampen-ID genau einmal: {known}",
    "Select all bulbs": "Alle Lampen auswählen",
    "Select bulb {n}": "Lampe {n} auswählen",
    "Switch on/off": "Ein-/Ausschalten",
    "Remote slider up": "Fernregler hoch",
    "Remote slider down": "Fernregler runter",
    "Brightness up (direct)": "Helligkeit hoch (direkt)",
    "Brightness down (direct)": "Helligkeit runter (direkt)",
    "All bulbs": "Alle Lampen",
    "on": "an",
    "off": "aus",
}


def _language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value and value not in ("C", "POSIX"):
            return value.split(":")[0][:2]
    return "en"


TRANSLATION = GERMAN if _language() == "de" else {}


def _(text: str) -> str:
    """User-facing string in the user's language (English and German so far)."""
    return TRANSLATION.get(text, text)


# ----------------------------------------------------------------- state


def save_state(path: Path, state: dict) -> None:
    """Atomic write (the same as btmesh's provisioning script does), private."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(state, indent=2))
    tmp.replace(path)


def read_state(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


@contextlib.contextmanager
def link_lock(state_path: Path):
    """One process at a time per state file: they share one mesh SEQ counter."""
    fd = os.open(lock_path(state_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        sys.exit(_("another hauslicht process holds the mesh link "
                   "(for adopt/setup: systemctl --user stop hauslicht first)"))
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
