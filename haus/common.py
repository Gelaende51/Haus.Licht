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
    "desktops: lights off when you switch the monitor off (not on standby), on when it returns (daemon running)":
        "Desktop-PCs: Licht aus, wenn du den Monitor ausschaltest (nicht im Standby), "
        "wieder an, wenn er zurückkommt (Dienst läuft)",
    "a laptop: the lid switches the lights instead": "ein Laptop: hier schaltet stattdessen der Deckel das Licht",
    "no monitor connector found": "kein Monitoranschluss gefunden",
    "no DisplayPort monitor, and ddcutil is missing or may not use i2c: the monitor's power switch cannot be seen":
        "kein DisplayPort-Monitor, und ddcutil fehlt oder darf i2c nicht nutzen: "
        "der Netzschalter des Monitors ist nicht erkennbar",
    "no DisplayPort monitor and no DDC/CI answer: the monitor's power switch cannot be seen":
        "kein DisplayPort-Monitor und keine DDC/CI-Antwort: der Netzschalter des Monitors ist nicht erkennbar",
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
    "look for new bulbs, optionally of one kind or every Bluetooth kind (daemon running)":
        "nach neuen Lampen suchen, wahlweise nur einer Art oder aller Bluetooth-Arten (Dienst läuft)",
    "add a bulb: KIND key=value ... (daemon running)": "eine Lampe hinzufügen: ART Schlüssel=Wert ... (Dienst läuft)",
    "stop controlling a bulb (daemon running)": "eine Lampe nicht mehr steuern (Dienst läuft)",
    "add newly found devices by itself, never mesh bulbs; may take over a neighbour's lights (daemon running)":
        "neu gefundene Geräte selbst hinzufügen, nie Mesh-Lampen; kann Lampen der Nachbarn übernehmen (Dienst läuft)",
    "the microphone for clap detection: a source name or default (daemon running)":
        "das Mikrofon für die Klatscherkennung: ein Quellenname oder default (Dienst läuft)",
    "clap detection sensitivity, 0-100 (daemon running)": "Empfindlichkeit der Klatscherkennung, 0-100 (Dienst läuft)",
    "list the microphones (daemon running)": "die Mikrofone auflisten (Dienst läuft)",
    "the selection (or one bulb) follows what the computer plays: loudness to brightness, pitch to colour (daemon running)":
        "die Auswahl (oder eine Lampe) folgt dem, was der Computer abspielt: Lautstärke wird Helligkeit, "
        "Tonhöhe wird Farbe (Dienst läuft)",
    "what follow-audio listens to: a source name or default, the default output's monitor (daemon running)":
        "worauf „Audio folgen“ hört: ein Quellenname oder default, der Monitor der Standardausgabe (Dienst läuft)",
    "follow-audio: pitch colours the bulbs, on or off (daemon running)":
        "Audio folgen: die Tonhöhe färbt die Lampen, on oder off (Dienst läuft)",
    "follow-audio: updates per second per bulb, 1-3; Bluetooth mesh at most 2 recommended (daemon running)":
        "Audio folgen: Aktualisierungen pro Sekunde und Lampe, 1-3; für Bluetooth-Mesh höchstens 2 empfohlen (Dienst läuft)",
    "list the sources follow-audio can listen to, monitors included (daemon running)":
        "die Quellen auflisten, auf die „Audio folgen“ hören kann, Monitore eingeschlossen (Dienst läuft)",
    "follow-audio: capture state, level, pitch and the messages sent this run (daemon running)":
        "Audio folgen: Aufnahmezustand, Pegel, Tonhöhe und die in diesem Lauf gesendeten Nachrichten (Dienst läuft)",
    "follow audio is not installed ({missing}): {cmd}": "„Audio folgen“ ist nicht installiert ({missing}): {cmd}",
    "follow audio and video: updates per second per bulb, 1-3; Bluetooth mesh at most 2 recommended (daemon running)":
        "Audio und Video folgen: Aktualisierungen pro Sekunde und Lampe, 1-3; für Bluetooth-Mesh höchstens 2 "
        "empfohlen (Dienst läuft)",
    "the selection (or one bulb) follows its region of the screen: luminance to brightness, colour to hue (daemon running)":
        "die Auswahl (oder eine Lampe) folgt ihrem Bereich des Bildschirms: Leuchtdichte wird Helligkeit, "
        "Farbe wird Farbton (Dienst läuft)",
    "the part of the screen a bulb follows: whole, left, right, top, bottom, top-left, top-right, bottom-left or bottom-right (daemon running)":
        "der Teil des Bildschirms, dem eine Lampe folgt: whole (ganz), left (links), right (rechts), top (oben), "
        "bottom (unten), top-left, top-right, bottom-left oder bottom-right (Dienst läuft)",
    "follow-video: the brightness of the brightest screen, 0-100 (daemon running)":
        "Video folgen: die Helligkeit beim hellsten Bild, 0-100 (Dienst läuft)",
    "follow-video: time averaging in seconds, 0, 0.2, 0.5, 1, 2 or 5 (daemon running)":
        "Video folgen: zeitliche Mittelung in Sekunden, 0, 0.2, 0.5, 1, 2 oder 5 (Dienst läuft)",
    "follow-video: forget the screen permission; the next start asks again (daemon running)":
        "Video folgen: die Bildschirmfreigabe vergessen; der nächste Start fragt erneut (Dienst läuft)",
    "follow-video: capture state, frames and the messages sent this run (daemon running)":
        "Video folgen: Aufnahmezustand, Bilder und die in diesem Lauf gesendeten Nachrichten (Dienst läuft)",
    "follow video is not installed ({missing}): {cmd}": "„Video folgen“ ist nicht installiert ({missing}): {cmd}",
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
    "no daemon: OnPowerUp back to \"on\" so that the wall switch just switches on (the service's stop)":
        "ohne Dienst: OnPowerUp zurück auf \"on\", damit der Wandschalter nur einschaltet (beim Beenden des Dienstes)",
    "only the bulbs the daemon left armed (its marker); nothing at all when there are none":
        "nur die Lampen, die der Dienst scharf hinterlassen hat (seine Markierung); gar nichts, wenn keine",
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
    "{bid} is disconnected (held); connect it first: hauslicht connect":
        "{bid} ist getrennt (gehalten); erst verbinden: hauslicht connect",
    "hold the bulbs disconnected: OnPowerUp back to on and the link dropped, so the wall switch works as usual; "
    "power and level stay as they are (daemon running)":
        "die Lampen getrennt halten: OnPowerUp zurück auf on und die Verbindung getrennt, sodass der Wandschalter "
        "wie gewohnt wirkt; Ein/Aus und Helligkeit bleiben, wie sie sind (Dienst läuft)",
    "connect the bulbs: end a hold, or retry a failed link now without waiting (daemon running)":
        "die Lampen verbinden: das Getrennthalten beenden oder eine gescheiterte Verbindung sofort, ohne Warten, "
        "neu versuchen (Dienst läuft)",
    "{bid} is not in widget {w}": "{bid} ist nicht in Widget {w}",
    "widgets must be a number from 1 to 8": "widgets muss eine Zahl von 1 bis 8 sein",
    "use must be on or off": "use muss on oder off sein",
    "an icon is an icon name or an absolute file path": "ein Symbol ist ein Symbolname oder ein absoluter Dateipfad",
    "order needs every bulb id once: {known}": "order braucht jede Lampen-ID genau einmal: {known}",
    "blink a bulb slowly to find it, off: stop; it stops by itself after 2 minutes (daemon running)":
        "eine Lampe langsam blinken lassen, um sie zu finden, off: aufhören; endet von selbst nach 2 Minuten (Dienst läuft)",
    "test a bulb: brightest, a fade, colours, then on/off fast to slow, about 25 s; flashes quickly, "
    "not for people sensitive to flashing light; stop: end it (daemon running)":
        "eine Lampe testen: am hellsten, ein Abblenden, Farben, dann an/aus von schnell nach langsam, etwa 25 s; "
        "blitzt schnell, nicht für Menschen, die empfindlich auf Lichtblitze reagieren; stop: beenden (Dienst läuft)",
    "starting": "startet",
    "brightest": "am hellsten",
    "fade to the dimmest level": "Abblenden auf die dunkelste Stufe",
    "colours": "Farben",
    "on/off, fast to slow": "an/aus, von schnell nach langsam",
    "test stopped ({why})": "Test abgebrochen ({why})",
    "fastest on/off: {rate} per second": "schnellstes An/Aus: {rate} pro Sekunde",
    "{bid} is not reachable": "{bid} ist nicht erreichbar",
    "a test is running on {bid}": "auf {bid} läuft ein Test",
    "desktop notifications blink the bulbs by the profile's rules: on or off, default off (daemon running)":
        "Desktop-Benachrichtigungen lassen die Lampen nach den Regeln des Profils blinken: on oder off, "
        "Standard off (Dienst läuft)",
    "replace the notification rules of the selection's profile: one JSON list (daemon running)":
        "die Benachrichtigungsregeln des Profils der Auswahl ersetzen: eine JSON-Liste (Dienst läuft)",
    "run a notification rule's pattern now (daemon running)":
        "das Muster einer Benachrichtigungsregel jetzt abspielen (Dienst läuft)",
    "notification blinks: watching or not, Do Not Disturb, the last match (daemon running)":
        "Benachrichtigungs-Blinken: Überwachung an oder aus, Nicht stören, der letzte Treffer (Dienst läuft)",
    "rule {n} is not an object": "Regel {n} ist kein Objekt",
    "rule {n} needs a name (at most {max} characters)": "Regel {n} braucht einen Namen (höchstens {max} Zeichen)",
    "rule {name}: the {field} pattern is longer than {max} characters":
        "Regel {name}: das Muster für {field} ist länger als {max} Zeichen",
    "rule {name}: the {field} pattern is not a valid regular expression ({why})":
        "Regel {name}: das Muster für {field} ist kein gültiger regulärer Ausdruck ({why})",
    "rule {name}: pattern must be one of {choices}": "Regel {name}: das Muster muss eines von {choices} sein",
    "rule {name}: count must be a whole number from 1 to 10":
        "Regel {name}: die Anzahl muss eine ganze Zahl von 1 bis 10 sein",
    "rule {name}: intensity must be from 1 to 100": "Regel {name}: die Helligkeit muss zwischen 1 und 100 liegen",
    "rule {name}: colour must be #rrggbb or empty": "Regel {name}: die Farbe muss #rrggbb oder leer sein",
    "rule {name}: bulbs must be selection or all": "Regel {name}: Lampen muss selection oder all sein",
    "rule {name}: dark must be on or off": "Regel {name}: dark muss on oder off sein",
    "the rules must be a list": "die Regeln müssen eine Liste sein",
    "at most {max} rules": "höchstens {max} Regeln",
    "two rules are named {name}": "zwei Regeln heißen {name}",
    "the rules are not valid JSON ({why})": "die Regeln sind kein gültiges JSON ({why})",
    "no rule named {name} in profile {profile}": "keine Regel namens {name} im Profil {profile}",
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
    "No Bluetooth adapter is free to scan: flick mode Exclusive holds it. Set the flick mode to Fast to search.":
        "Kein Bluetooth-Adapter frei zum Suchen: der Umschaltmodus Exklusiv belegt ihn. Zum Suchen den Modus auf Schnell stellen.",
    "act in widget N's context: its bulbs, selection, profile and clap mode (default 1; daemon running)":
        "im Kontext von Widget N handeln: seine Lampen, Auswahl, sein Profil und Klatschmodus (Standard 1; Dienst läuft)",
    "print the daemon's reply as JSON instead of text": "die Antwort des Dienstes als JSON statt als Text ausgeben",
    "one of the commands below; hauslicht COMMAND --help shows its arguments":
        "einer der Befehle unten; hauslicht BEFEHL --help zeigt seine Argumente",
    "Switch and dim": "Schalten und dimmen",
    "Follow the screen: mode, profile, colour": "Dem Bildschirm folgen: Modus, Profil, Farbe",
    "Bulbs and widgets": "Lampen und Widgets",
    "Add and remove bulbs": "Lampen hinzufügen und entfernen",
    "Wall switch, lid and monitor": "Wandschalter, Deckel und Monitor",
    "Clap and follow audio": "Klatschen und Audio folgen",
    "Follow video": "Video folgen",
    "Notification blinks": "Benachrichtigungs-Blinken",
    "Service and setup": "Dienst und Einrichtung",
    "the widget's brightness slider, 0-100: the profile's level while the screen is ignored, "
    "otherwise a manual level until resume (daemon running)":
        "der Helligkeitsregler des Widgets, 0-100: die Stufe des Profils, solange der Bildschirm nicht zählt, "
        "sonst eine manuelle Stufe bis resume (Dienst läuft)",
    "the widget's state in short: selection, power, profile values, settings, bulbs (daemon running)":
        "der Zustand des Widgets in Kürze: Auswahl, an/aus, Profilwerte, Einstellungen, Lampen (Dienst läuft)",
    "print the daemon's events as they happen, until Ctrl+C (daemon running)":
        "die Ereignisse des Dienstes ausgeben, sobald sie geschehen, bis Strg+C (Dienst läuft)",
    "the screen window of the selection's profile: LO HI, -100 to 200 (daemon running)":
        "das Bildschirmfenster des Profils der Auswahl: UNTEN OBEN, -100 bis 200 (Dienst läuft)",
    "the curve of the selection's profile, -100 to 100 (daemon running)":
        "die Kurve des Profils der Auswahl, -100 bis 100 (Dienst läuft)",
    "the colour of the selection's profile: hue 0-360 (daemon running)":
        "die Farbe des Profils der Auswahl: Farbton 0-360 (Dienst läuft)",
    "the colour of the selection's profile: saturation 0-100, 0 is white (daemon running)":
        "die Farbe des Profils der Auswahl: Sättigung 0-100, 0 ist Weiß (Dienst läuft)",
    "the red shift with Night Light, 0-200: 100 as much as the screen, 0 none (daemon running)":
        "die Rotverschiebung mit dem Nachtlicht, 0-200: 100 so stark wie der Bildschirm, 0 keine (Dienst läuft)",
    "white bulbs follow Night Light's screen white: on or off (daemon running)":
        "weiße Lampen folgen dem Bildschirmweiß des Nachtlichts: on oder off (Dienst läuft)",
    "profiles: list | save NAME | apply NAME | rename OLD NEW | delete NAME (daemon running)":
        "Profile: list | save NAME | apply NAME | rename ALT NEU | delete NAME (Dienst läuft)",
    "set any key as hauslicht-ctl set does: KEY=VALUE ..., see docs/ctl-protocol.md (daemon running)":
        "einen beliebigen Schlüssel setzen wie hauslicht-ctl set: SCHLÜSSEL=WERT ..., siehe docs/ctl-protocol.md "
        "(Dienst läuft)",
    "every bulb with its use, widget, state, icons, audio/video and screen region (daemon running)":
        "jede Lampe mit Verwendung, Widget, Zustand, Symbolen, Audio/Video und Bildschirmbereich (Dienst läuft)",
    "set a bulb's name=, profile=, use=on|off, icon_on=, icon_off=, icon_disconnected= (daemon running)":
        "name=, profile=, use=on|off, icon_on=, icon_off=, icon_disconnected= einer Lampe setzen (Dienst läuft)",
    "the list order: every bulb id once (daemon running)":
        "die Reihenfolge der Liste: jede Lampen-ID genau einmal (Dienst läuft)",
    "how many widgets, 1-8 (daemon running)": "wie viele Widgets, 1-8 (Dienst läuft)",
    "the bulbs of the widget (-w N), kept in list order; none: no bulb (daemon running)":
        "die Lampen des Widgets (-w N), in Listenreihenfolge; none: keine Lampe (Dienst läuft)",
    "the Bluetooth adapter flick mode exclusive takes: hciN, its address or auto (daemon running)":
        "der Bluetooth-Adapter, den der Flick-Modus exclusive nimmt: hciN, seine Adresse oder auto (Dienst läuft)",
    "a double clap or snap toggles the widget's lights: off | clap | snap | both (daemon running)":
        "doppeltes Klatschen oder Fingerschnippen schaltet die Lampen des Widgets um: off | clap | snap | both "
        "(Dienst läuft)",
    "the notification rules of the selection's profile (daemon running)":
        "die Benachrichtigungsregeln des Profils der Auswahl (Dienst läuft)",
    "the system setup: missing, outdated or current, read anew (daemon running)":
        "die Systemeinrichtung: missing (fehlt), outdated (veraltet) oder current (aktuell), neu gelesen (Dienst läuft)",
    "open a window showing each bulb's intended state and the daemon's events, live (PyQt6)":
        "ein Fenster öffnen, das den Sollzustand jeder Lampe und die Ereignisse des Dienstes live zeigt (PyQt6)",
    "While the daemon runs it owns the Bluetooth link; the other commands then go to it over its socket. "
    "man hauslicht: files, environment, exit status, examples.":
        "Während der Dienst läuft, gehört ihm die Bluetooth-Verbindung; die anderen Befehle gehen dann über seinen "
        "Socket an ihn. man hauslicht: Dateien, Umgebung, Rückgabewerte, Beispiele.",
    "range needs both LO and HI, or neither": "range braucht UNTEN und OBEN, oder keines von beiden",
    "profile {action} needs a NAME": "profile {action} braucht einen NAMEN",
    "not in use": "nicht verwendet",
    "offline": "nicht erreichbar",
    "not connected": "nicht verbunden",
    "disconnected (held)": "getrennt (gehalten)",
    "in widget": "im Widget",
    "follows audio": "folgt Audio",
    "follows video": "folgt Video",
    "autoconnected": "automatisch verbunden",
    "no bulbs": "keine Lampen",
    "widget {w} of {n}, selection {sel} (profile {profile})": "Widget {w} von {n}, Auswahl {sel} (Profil {profile})",
    "profile {name}:": "Profil {name}:",
    "bulbs:": "Lampen:",
    "devices": "Geräte",
    "(taking it leaves BlueZ without an adapter)": "(nimmt BlueZ den letzten Adapter)",
    "nothing found": "nichts gefunden",
    "(added already)": "(schon hinzugefügt)",
    "ready": "bereit",
    "needs {missing}": "braucht {missing}",
    "required": "Pflichtfeld",
    "notification rules of profile {name}:": "Benachrichtigungsregeln des Profils {name}:",
    "every notification": "jede Benachrichtigung",
    "blinking: {bulbs}": "blinkt: {bulbs}",
    "added: {id}": "hinzugefügt: {id}",
    "widget {w}, {n} bulbs": "Widget {w}, {n} Lampen",
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


def armed_path(state_path: Path) -> Path:
    """Beside the state file: the bulbs whose OnPowerUp may be armed to
    anything but "on" (hauslicht.json -> hauslicht.armed.json). Kept apart
    so that arming never rewrites the file with the keys and the SEQ, and
    `disarm --if-armed` can tell "nothing to do" without reading that."""
    return state_path.with_suffix(".armed.json")


def read_armed(state_path: Path) -> dict[str, str]:
    """bulb id -> the OnPowerUp last sent to it ({} when none or unreadable)."""
    try:
        data = json.loads(armed_path(state_path).read_text())
    except (OSError, ValueError):
        return {}
    bulbs = data.get("bulbs") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in bulbs.items()} if isinstance(bulbs, dict) else {}


def write_armed(state_path: Path, armed: dict[str, str]) -> None:
    """Only under link_lock (the daemon, or `disarm`): one writer at a time."""
    save_state(armed_path(state_path), {"bulbs": dict(sorted(armed.items()))})


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
