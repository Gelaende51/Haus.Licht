#!/usr/bin/python3
"""Debug aid: flash the screen white (on) or black (off) for 0.4 s.

The sync daemon runs it the moment it detects a wall-switch flick when
LICHTHAUS_FLASH is set, so detection and the bulb's reaction can be told apart.
"""
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)
w = QWidget()
w.setStyleSheet(f"background: {'white' if sys.argv[1:] == ['on'] else 'black'}")
w.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
w.showFullScreen()
QTimer.singleShot(400, app.quit)
app.exec()
