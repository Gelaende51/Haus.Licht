#!/usr/bin/env python3
"""lichthaus-tray: system tray menu for the Licht.Haus sync daemon.

Talks to `lichthaus sync` over its Unix socket only, so it needs PyQt6 but
neither btmesh nor the project's venv. Brightness itself stays on the
"Keyboard brightness" slider of Plasma's brightness applet.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

SOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "lichthaus.sock"
POLL_MS = 10_000

GERMAN = {
    "Licht.Haus": "Licht.Haus",
    "Follow the screen": "Dem Bildschirm folgen",
    "Always": "Immer",
    "Only while Night Light is on": "Nur bei aktivem Nachtlicht",
    "Never (slider sets the bulbs)": "Nie (Regler steuert die Lampen)",
    "Slider sets": "Der Regler bestimmt",
    "Maximum: bulbs follow the screen up to the slider": "Maximum: Lampen folgen dem Bildschirm bis zum Regler",
    "Minimum: bulbs follow the screen from the slider up": "Minimum: Lampen folgen dem Bildschirm ab dem Regler",
    "Independent: the slider alone": "Unabhängig: nur der Regler",
    "Coupling: how strongly bulbs react to the screen": "Kopplung: wie stark die Lampen auf den Bildschirm reagieren",
    "Bulbs on": "Lampen ein",
    "Bulbs off": "Lampen aus",
    "Resume following": "Wieder folgen",
    "Quit": "Beenden",
    "service not running": "Dienst läuft nicht",
    "slider {slider} %": "Regler {slider} %",
    "following the screen": "folgt dem Bildschirm",
    "paused (manual command)": "angehalten (manueller Befehl)",
    "not following": "folgt nicht",
    "Night Light on": "Nachtlicht an",
}


def _language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value and value not in ("C", "POSIX"):
            return value.split(":")[0][:2]
    return "en"


TRANSLATION = GERMAN if _language() == "de" else {}


def _(text: str) -> str:
    return TRANSLATION.get(text, text)


def ask(cmd: str, arg: str | None = None, timeout: float = 30.0) -> dict | None:
    """One request to the daemon; None when it is not running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(SOCK_PATH))
            s.sendall(json.dumps({"cmd": cmd, "arg": arg, "which": None}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)
    except (OSError, ValueError):
        return None


class Tray(QObject):
    answered = pyqtSignal(object)  # daemon state (dict) or None, from worker threads

    def __init__(self) -> None:
        super().__init__()
        icon = next((QIcon.fromTheme(n) for n in ("lightbulb", "light-bulb", "brightness-high",
                                                   "preferences-desktop-display")
                     if QIcon.hasThemeIcon(n)), QIcon.fromTheme("brightness-high"))
        self.tray = QSystemTrayIcon(icon)
        self.menu = QMenu()
        self.status = self.menu.addAction(_("Licht.Haus"))
        self.status.setEnabled(False)

        self.menu.addSection(_("Follow the screen"))
        self.track = self._radio_group({
            "always": _("Always"),
            "night": _("Only while Night Light is on"),
            "never": _("Never (slider sets the bulbs)"),
        }, "track")
        self.menu.addSection(_("Slider sets"))
        self.mode = self._radio_group({
            "maximum": _("Maximum: bulbs follow the screen up to the slider"),
            "minimum": _("Minimum: bulbs follow the screen from the slider up"),
            "independent": _("Independent: the slider alone"),
            "coupling": _("Coupling: how strongly bulbs react to the screen"),
        }, "mode")

        self.menu.addSeparator()
        self.resume = self.menu.addAction(_("Resume following"))
        self.resume.triggered.connect(lambda: self.send("resume"))
        self.menu.addAction(_("Bulbs on")).triggered.connect(lambda: self.send("on"))
        self.menu.addAction(_("Bulbs off")).triggered.connect(lambda: self.send("off"))
        self.menu.addSeparator()
        self.menu.addAction(QIcon.fromTheme("application-exit"), _("Quit")).triggered.connect(
            QApplication.quit)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.menu.aboutToShow.connect(self.refresh)
        self.answered.connect(self.show_state)
        self.timer = QTimer(self, interval=POLL_MS, timeout=self.refresh)
        self.timer.start()
        self.tray.show()
        self.refresh()

    def _radio_group(self, choices: dict[str, str], cmd: str) -> dict[str, QAction]:
        group = QActionGroup(self.menu)
        actions = {}
        for key, label in choices.items():
            action = self.menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked, k=key: self.send(cmd, k))
            group.addAction(action)
            actions[key] = action
        return actions

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.menu.popup(self.tray.geometry().center())

    def send(self, cmd: str, arg: str | None = None) -> None:
        """Run a request off the UI thread, then refresh the state."""
        def work() -> None:
            ask(cmd, arg)
            self.answered.emit(self._state())
        threading.Thread(target=work, daemon=True).start()

    def _state(self) -> dict | None:
        reply = ask("mode", timeout=5.0)
        return reply["results"][0] if reply and reply.get("results") else None

    def refresh(self) -> None:
        threading.Thread(target=lambda: self.answered.emit(self._state()), daemon=True).start()

    def show_state(self, state: dict | None) -> None:
        running = state is not None
        for action in [*self.track.values(), *self.mode.values()]:
            action.setEnabled(running)
        if not running:
            self.status.setText(f"Licht.Haus: {_('service not running')}")
            self.tray.setToolTip(self.status.text())
            self.resume.setVisible(False)
            return
        self.track[state["track"]].setChecked(True)
        self.mode[state["mode"]].setChecked(True)
        if not state["following"]:
            doing = _("paused (manual command)")
        elif state["tracking"]:
            doing = _("following the screen")
        else:
            doing = _("not following")
        parts = [_("slider {slider} %").format(slider=state["slider"]), doing]
        if state.get("night"):
            parts.append(_("Night Light on"))
        self.status.setText("Licht.Haus: " + ", ".join(parts))
        self.tray.setToolTip(self.status.text())
        self.resume.setVisible(not state["following"])


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("lichthaus-tray")
    app.setDesktopFileName("lichthaus-tray")
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    _tray = Tray()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
