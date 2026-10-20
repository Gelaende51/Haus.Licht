"""Repeated warnings, logged once: the same message again (numbers aside)
within WINDOW_S is counted instead of logged, and a summary line follows when
the window is over (at the next record, or with the next repeat).

The night of 2026-10-20 logged "mesh: could not establish a GATT session" and
its companions some 250 times; the journal kept nothing else readable. Plain
info lines stay untouched (their timestamps time the flick path), except the
failure lines in INFO_REPEATS."""

from __future__ import annotations

import logging
import re
import time

WINDOW_S = 600.0
INFO_REPEATS = ("connect failed (", "boot check: no link")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_MAX_KEYS = 200


class RepeatFilter(logging.Filter):
    def __init__(self, handler: logging.Handler | None = None, window: float = WINDOW_S,
                 clock=time.monotonic) -> None:
        super().__init__()
        self.handler = handler
        self.window = window
        self.clock = clock
        self.seen: dict[tuple, list] = {}  # key -> [logged at, repeats held back, text, record]

    @staticmethod
    def watched(record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return record.levelno == logging.INFO and any(p in str(record.msg) for p in INFO_REPEATS)

    def _summary(self, entry: list) -> logging.LogRecord:
        _at, count, text, rec = entry
        minutes = max(1, round(self.window / 60))
        out = logging.LogRecord(rec.name, rec.levelno, rec.pathname, rec.lineno,
                                "repeated %d more times in %d min: %s", (count, minutes, text), None)
        out._repeat_summary = True
        return out

    def _flush(self, now: float, skip: tuple | None) -> None:
        """Summaries of windows that are over; forgets old keys."""
        for key, entry in list(self.seen.items()):
            if key == skip or now - entry[0] < self.window:
                continue
            del self.seen[key]
            if entry[1] and self.handler is not None:
                self.handler.handle(self._summary(entry))

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_repeat_summary", False):
            return True
        now = self.clock()
        key = None
        if self.watched(record):
            text = record.getMessage()
            key = (record.name, record.levelno, _NUMBER.sub("#", text))
        if self.seen:
            self._flush(now, key)
        if key is None:
            return True
        entry = self.seen.get(key)
        if entry is None:
            if len(self.seen) >= _MAX_KEYS:
                self.seen.pop(next(iter(self.seen)))
            self.seen[key] = [now, 0, text, record]
            return True
        if now - entry[0] < self.window:
            entry[1] += 1
            entry[2], entry[3] = text, record  # the summary quotes the latest
            return False
        if entry[1]:  # the window is over: this one goes out with the count
            record.msg = "%s (repeated %d more times in the %d min before)"
            record.args = (text, entry[1], max(1, round(self.window / 60)))
        self.seen[key] = [now, 0, text, record]
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Put a RepeatFilter on each handler of `logger` (default: the root)."""
    for handler in (logger or logging.getLogger()).handlers:
        if not any(isinstance(f, RepeatFilter) for f in handler.filters):
            handler.addFilter(RepeatFilter(handler))
