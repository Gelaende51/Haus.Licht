"""Repeated warnings are logged once, then summarised."""

import logging

from haus.logrepeat import RepeatFilter


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


def setup():
    clock = Clock()
    out: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            out.append(record.getMessage())

    handler = Collect()
    handler.addFilter(RepeatFilter(handler, window=600, clock=clock))
    log = logging.getLogger("test-logrepeat")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, clock, out


def test_repeats_are_held_back_and_summarised():
    log, clock, out = setup()
    for backoff in (5, 10, 20, 40):
        log.warning("mesh: could not establish a GATT session; retrying in %d s", backoff)
        clock.t += 30
    assert out == ["mesh: could not establish a GATT session; retrying in 5 s"]
    clock.t += 600
    log.info("connecting to proxy")  # any record after the window brings the summary
    assert out[1:] == ["repeated 3 more times in 10 min: mesh: could not establish a GATT session; retrying in 40 s",
                       "connecting to proxy"]


def test_a_repeat_after_the_window_carries_the_count():
    log, clock, out = setup()
    log.warning("scanner start failed (1/3): busy")
    clock.t += 10
    log.warning("scanner start failed (2/3): busy")
    clock.t += 700
    log.warning("scanner start failed (1/3): busy")
    assert out == ["scanner start failed (1/3): busy",
                   "scanner start failed (1/3): busy (repeated 1 more times in the 10 min before)"]


def test_plain_info_and_other_messages_pass():
    log, clock, out = setup()
    for _ in range(3):
        log.info("connecting to proxy F0:D1")
        log.info("connect failed (%s); waiting for a new advert", "TimeoutError")
    log.warning("something else")
    assert out == ["connecting to proxy F0:D1"] * 1 + ["connect failed (TimeoutError); waiting for a new advert"] \
        + ["connecting to proxy F0:D1"] * 2 + ["something else"]
