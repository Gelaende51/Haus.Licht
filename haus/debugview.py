"""Haus.Licht debug window: each bulb's intended state, live from the daemon.

    hauslicht debug                  open the window (detached; needs PyQt6)
    python3 -m haus.debugview        the same, in the foreground

Connects to the daemon socket ($HAUSLICHT_SOCK, else
$XDG_RUNTIME_DIR/hauslicht.sock, as hauslicht-ctl does), sends `watch` and
draws every JSON line the moment it arrives (no polling; reconnects every
second while no daemon answers). One tile per bulb in the snapshot's order:
a swatch of what the daemon wants the bulb to show (black "off", the colour
scaled by the percent, grey "idle" for use=off bulbs), with name, level,
colour and the last event's time under it; a flick outlines the tile yellow
for 1.5 s. Below: the event log, newest first. The window remembers nothing.

The pure parts (parse_line, swatch_rgb, colour_text, ViewState) need no Qt,
so the tests run without PyQt6 or a display.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

WARM_WHITE = (0xff, 0xe8, 0xc0)
LOG_MAX = 300
FLICK_SECONDS = 1.5


def sock_path() -> Path:
    """The daemon socket, resolved as hauslicht-ctl does."""
    return Path(os.environ.get("HAUSLICHT_SOCK")
                or Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hauslicht.sock")


# ----------------------------------------------------------------- pure parts

def parse_line(line: bytes | str) -> dict | None:
    """One watch line as a dict with an "event" key; None for anything else."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("event"), str):
        return None
    return obj


def _rgb_of(color) -> tuple[int, int, int] | None:
    if not isinstance(color, dict):
        return None
    rgb = color.get("rgb")
    if isinstance(rgb, str) and rgb.startswith("#") and len(rgb) == 7:
        try:
            return tuple(int(rgb[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
        except ValueError:
            return None
    if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        try:
            return tuple(max(0, min(255, int(round(float(v))))) for v in rgb)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def swatch_rgb(power, percent, color=None) -> tuple[int, int, int]:
    """The swatch colour: black when off, else the colour (color.rgb, or warm
    white) scaled toward black by percent/100. An idle bulb (percent None)
    draws grey."""
    if not power:
        return (0, 0, 0)
    if percent is None:
        return (0x80, 0x80, 0x80)
    base = _rgb_of(color) or WARM_WHITE
    f = max(0.0, min(1.0, float(percent) / 100.0))
    return tuple(int(round(c * f)) for c in base)  # type: ignore[return-value]


def text_on(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever reads better on rgb."""
    r, g, b = rgb
    return (0, 0, 0) if 0.299 * r + 0.587 * g + 0.114 * b > 140 else (255, 255, 255)


def _num(v) -> str:
    return f"{float(v):g}"


def colour_text(color) -> str:
    """'hs 30°/40 %', 'ct 3400 K', 'rgb #ff8000' or 'white' (no colour)."""
    if not isinstance(color, dict) or not color:
        return "white"
    hs = color.get("hs")
    h, s = (hs if isinstance(hs, (list, tuple)) and len(hs) == 2
            else (color.get("hue", color.get("h")), color.get("sat", color.get("s"))))
    ct = color.get("ct", color.get("kelvin"))
    try:
        if h is not None and s is not None:
            return f"hs {_num(h)}°/{_num(s)} %"
        if ct is not None:
            return f"ct {_num(ct)} K"
    except (TypeError, ValueError):
        pass
    rgb = _rgb_of(color)
    if rgb:
        return "rgb #%02x%02x%02x" % rgb
    return json.dumps(color, separators=(",", ":"))


def level_text(power, percent) -> str:
    if percent is None:
        return "idle" + (" (on)" if power else " (off)")
    return f"{'on' if power else 'off'} {float(percent):g} %"


def clock(t) -> str:
    """HH:MM:SS.mmm of a Unix time (local)."""
    try:
        t = float(t)
    except (TypeError, ValueError):
        t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def event_details(ev: dict) -> str:
    skip = ("event", "t", "bulb")
    parts = []
    for k, v in ev.items():
        if k in skip:
            continue
        if k == "color":
            if v is not None:
                parts.append(f"color={colour_text(v)}")
            continue
        parts.append(f"{k}={json.dumps(v) if isinstance(v, (dict, list)) else v}")
    return " ".join(parts)


def log_line(ev: dict) -> str:
    return f"{clock(ev.get('t'))} {ev.get('event')} {ev.get('bulb', '-')} {event_details(ev)}".rstrip()


class ViewState:
    """What the window shows, updated by watch events; Qt-free."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.bulbs: dict[str, dict] = {}   # id -> name, power, percent, color, last
        self.log: list[tuple[str, bool]] = []  # (line, is_flick), newest first

    def _bulb(self, bid: str) -> dict:
        if bid not in self.bulbs:
            self.bulbs[bid] = {"name": bid, "power": False, "percent": None, "color": None, "last": None}
            self.order.append(bid)
        return self.bulbs[bid]

    def apply(self, ev: dict) -> dict:
        """Apply one event. Returns {"rebuild": bool, "changed": [ids], "flick": id|None}."""
        kind = ev.get("event")
        out = {"rebuild": False, "changed": [], "flick": None}
        if kind == "snapshot":
            self.order, self.bulbs = [], {}
            intended = ev.get("intended") or {}
            for b in ev.get("bulbs_list") or []:
                bid = b.get("id")
                if not bid:
                    continue
                e = self._bulb(bid)
                e["name"] = b.get("name") or bid
                want = intended.get(bid)
                if want is None:  # not in intended: fall back to what get says
                    want = {"power": b.get("power"), "percent": None if b.get("use") is False else b.get("output"),
                            "color": None}
                e.update(power=bool(want.get("power")), percent=want.get("percent"), color=want.get("color"))
                e["last"] = ev.get("t")
            for bid, want in intended.items():  # intended bulbs missing from bulbs_list
                if bid not in self.bulbs:
                    e = self._bulb(bid)
                    e.update(power=bool(want.get("power")), percent=want.get("percent"),
                             color=want.get("color"), last=ev.get("t"))
            out["rebuild"] = True
            self._log(f"{clock(ev.get('t'))} snapshot - {len(self.order)} bulbs", False)
            return out
        bid = ev.get("bulb")
        if bid:
            if bid not in self.bulbs:
                out["rebuild"] = True
            e = self._bulb(bid)
            if kind == "intended":
                e.update(power=bool(ev.get("power")), percent=ev.get("percent"), color=ev.get("color"))
            e["last"] = ev.get("t")
            out["changed"].append(bid)
            if kind == "flick":
                out["flick"] = bid
        self._log(log_line(ev), kind == "flick")
        return out

    def _log(self, line: str, flick: bool) -> None:
        self.log.insert(0, (line, flick))
        del self.log[LOG_MAX:]


# ----------------------------------------------------------------- Qt window

def run() -> int:  # pragma: no cover - needs a display
    import sys

    from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer
    from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen
    from PyQt6.QtNetwork import QLocalSocket
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QLayout, QPlainTextEdit,
                                 QScrollArea, QSizePolicy, QSplitter, QVBoxLayout, QWidget)

    class FlowLayout(QLayout):
        def __init__(self, parent=None, spacing=10):
            super().__init__(parent)
            self.items = []
            self.setSpacing(spacing)

        def addItem(self, item):
            self.items.append(item)

        def count(self):
            return len(self.items)

        def itemAt(self, i):
            return self.items[i] if 0 <= i < len(self.items) else None

        def takeAt(self, i):
            return self.items.pop(i) if 0 <= i < len(self.items) else None

        def expandingDirections(self):
            return Qt.Orientation(0)

        def hasHeightForWidth(self):
            return True

        def heightForWidth(self, w):
            return self._do(QRect(0, 0, w, 0), True)

        def setGeometry(self, rect):
            super().setGeometry(rect)
            self._do(rect, False)

        def sizeHint(self):
            return self.minimumSize()

        def minimumSize(self):
            s = QSize()
            for it in self.items:
                s = s.expandedTo(it.minimumSize())
            m = self.contentsMargins()
            return s + QSize(m.left() + m.right(), m.top() + m.bottom())

        def _do(self, rect, test):
            m = self.contentsMargins()
            r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
            x, y, line_h, sp = r.x(), r.y(), 0, self.spacing()
            for it in self.items:
                hint = it.sizeHint()
                if x + hint.width() > r.right() + 1 and line_h > 0:
                    x, y, line_h = r.x(), y + line_h + sp, 0
                if not test:
                    it.setGeometry(QRect(QPoint(x, y), hint))
                x += hint.width() + sp
                line_h = max(line_h, hint.height())
            return y + line_h - rect.y() + m.bottom()

    class Swatch(QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedSize(140, 140)
            self.rgb, self.label = (0, 0, 0), "off"

        def paintEvent(self, _ev):
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(*self.rgb))
            p.setPen(QColor(*text_on(self.rgb)))
            f = p.font()
            f.setPointSize(26 if self.label[0].isdigit() else 18)
            f.setBold(True)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.label)

    class Tile(QWidget):
        def __init__(self):
            super().__init__()
            self.setObjectName("tile")
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(6, 6, 6, 6)
            lay.setSpacing(2)
            self.swatch = Swatch()
            lay.addWidget(self.swatch, 0, Qt.AlignmentFlag.AlignHCenter)
            self.name, self.level, self.colour, self.when = QLabel(), QLabel(), QLabel(), QLabel()
            self.name.setStyleSheet("font-weight: bold")
            for lab in (self.name, self.level, self.colour, self.when):
                lab.setFixedWidth(140)
                lay.addWidget(lab)
            self.setFixedSize(self.sizeHint())
            self.flash(False)

        def show_state(self, e):
            power, pct = e["power"], e["percent"]
            self.swatch.rgb = swatch_rgb(power, pct, e["color"])
            self.swatch.label = ("idle" if pct is None else "off") if (pct is None or not power) \
                else f"{float(pct):g} %"
            self.swatch.update()
            self.name.setText(e["name"])
            self.name.setToolTip(e["name"])
            self.level.setText(level_text(power, pct))
            self.colour.setText(colour_text(e["color"]))
            self.when.setText(clock(e["last"]) if e["last"] is not None else "–")

        def flash(self, on):
            self.setStyleSheet("#tile { border: 3px solid %s; }" % ("#ffd400" if on else "transparent"))

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Haus.Licht debug")
            self.resize(820, 640)
            self.state = ViewState()
            self.tiles: dict[str, Tile] = {}
            self.timers: dict[str, QTimer] = {}
            root = QVBoxLayout(self)
            top = QHBoxLayout()
            self.status = QLabel("Connecting…")
            self.ontop = QCheckBox("Keep on top")
            self.ontop.setChecked(True)
            self.ontop.toggled.connect(self.set_on_top)
            top.addWidget(self.status, 1)
            top.addWidget(self.ontop)
            root.addLayout(top)
            self.grid_host = QWidget()
            self.flow = FlowLayout(self.grid_host)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(self.grid_host)
            self.logview = QPlainTextEdit()
            self.logview.setReadOnly(True)
            self.logview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            self.logview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            split = QSplitter(Qt.Orientation.Vertical)
            split.addWidget(scroll)
            split.addWidget(self.logview)
            split.setSizes([420, 200])
            root.addWidget(split, 1)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.sock = QLocalSocket(self)
            self.sock.connected.connect(self.on_connected)
            self.sock.readyRead.connect(self.on_ready)
            self.sock.disconnected.connect(self.on_lost)
            self.sock.errorOccurred.connect(lambda _e: self.on_lost())
            self.retry = QTimer(self)
            self.retry.setSingleShot(True)
            self.retry.setInterval(1000)
            self.retry.timeout.connect(self.connect_daemon)
            self.connect_daemon()

        def set_on_top(self, on):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            self.show()

        def connect_daemon(self):
            if self.sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                self.sock.abort()
            self.sock.connectToServer(str(sock_path()))

        def on_connected(self):
            self.status.setText(f"Connected  ({sock_path()})")
            self.sock.write(json.dumps({"cmd": "watch", "arg": None, "args": [], "which": None}).encode() + b"\n")

        def on_lost(self):
            self.status.setText("Daemon not running")
            if not self.retry.isActive():
                self.retry.start()

        def on_ready(self):
            while self.sock.canReadLine():
                ev = parse_line(bytes(self.sock.readLine()))
                if ev is None:
                    continue
                res = self.state.apply(ev)
                if res["rebuild"]:
                    self.rebuild()
                for bid in res["changed"]:
                    self.tiles[bid].show_state(self.state.bulbs[bid])
                if res["flick"]:
                    self.flick(res["flick"])
                self.render_log()

        def rebuild(self):
            for t in self.tiles.values():
                self.flow.removeWidget(t)
                t.deleteLater()
            self.tiles = {}
            for bid in self.state.order:
                t = Tile()
                self.tiles[bid] = t
                self.flow.addWidget(t)
                t.show_state(self.state.bulbs[bid])
            self.grid_host.updateGeometry()

        def flick(self, bid):
            tile = self.tiles.get(bid)
            if tile is None:
                return
            tile.flash(True)
            old = self.timers.pop(bid, None)
            if old:
                old.stop()
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self.tiles[bid].flash(False) if bid in self.tiles else None)
            timer.start(int(FLICK_SECONDS * 1000))
            self.timers[bid] = timer

        def render_log(self):
            import html
            rows = [("<b>%s</b>" if fl else "%s") % html.escape(line) for line, fl in self.state.log]
            self.logview.clear()
            self.logview.appendHtml("<br>".join(rows))
            self.logview.moveCursor(self.logview.textCursor().MoveOperation.Start)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Haus.Licht debug")
    w = Window()
    w.show()
    return app.exec()


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
