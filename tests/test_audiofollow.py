"""Follow audio (haus/audiofollow.py and the core's side): synthetic signals
only, never a real capture; the daemon tests swap the service for a fake."""

import asyncio
import json
import math
import sys
import time

import pytest

np = pytest.importorskip("numpy")

from haus import audiofollow as audiomod  # noqa: E402
from haus.common import UserError, save_state  # noqa: E402
from haus.daemon import Daemon  # noqa: E402
from test_core import FakeDesktop, cmd, mock, mock_state, settle  # noqa: E402

RATE = audiomod.RATE


def tone(hz: float, seconds: float, db: float = -20.0):
    """A sine at `db` dBFS RMS."""
    t = np.arange(round(seconds * RATE)) / RATE
    amp = math.sqrt(2) * 10 ** (db / 20)
    return (amp * np.sin(2 * math.pi * hz * t)).astype(np.float32)


def silence(seconds: float):
    return np.zeros(round(seconds * RATE), dtype=np.float32)


def bursts(base_db: float, seconds: float, hz: float = 440.0):
    """Music-like: 0.5 s loud (base + 20 dB) and 0.5 s quiet (base) in turn."""
    parts = []
    for i in range(round(seconds)):
        parts += [tone(hz, 0.5, base_db + 20), tone(hz, 0.5, base_db)]
    return np.concatenate(parts)


# --------------------------------------------------------------------- analyser

@pytest.mark.parametrize("base_db", [-45.0, -15.0])
def test_quiet_and_loud_music_both_use_the_whole_range(base_db):
    a = audiomod.Analyser()
    a.feed(bursts(base_db, 12))
    assert base_db - 1 <= a.floor_db <= base_db + 8  # the floor followed the music's level
    assert base_db + 15 <= a.ceil_db <= base_db + 21
    a.feed(tone(440, 0.5, base_db + 20))
    assert a.level >= 0.9
    a.feed(tone(440, 0.5, base_db))
    assert a.level <= 0.15


def test_fast_attack_slow_release():
    a = audiomod.Analyser()
    a.feed(tone(440, 2.0, -40))
    base = a.env_db
    a.feed(tone(440, 0.1, -25))  # 100 ms 15 dB louder: 50 ms attack, most of the way up
    up = a.env_db
    assert up - base >= 12.5
    a.feed(tone(440, 0.1, -40))  # 100 ms quiet again: 400 ms release, barely down
    assert up - a.env_db <= 4.0
    a.feed(tone(440, 1.5, -40))  # then down
    assert a.env_db - base <= 1.0


def test_a_steady_tone_does_not_jump_over_the_whole_range():
    a = audiomod.Analyser()
    a.feed(tone(440, 11, -30))
    assert a.ceil_db - a.floor_db >= audiomod.MIN_SPAN_DB


@pytest.mark.parametrize("hz,want", [(100, 0.0), (632, 0.5), (4000, 1.0)])
def test_centroid_on_a_log_scale(hz, want):
    a = audiomod.Analyser()
    a.feed(tone(hz, 3, -20))
    assert a.tone == pytest.approx(want, abs=0.08)


def test_a_sweep_moves_the_hue_and_the_white_up():
    a = audiomod.Analyser()
    hues, kelvins = [], []
    for hz in (120, 300, 800, 2000, 3800):
        a.feed(tone(hz, 1.5, -20))
        hues.append(audiomod.tone_hue(a.tone))
        kelvins.append(audiomod.tone_kelvin(a.tone, (2700, 6500)))
    assert hues == sorted(hues) and hues[0] < 30 and hues[-1] > 260 and max(hues) <= audiomod.HUE_MAX
    assert kelvins == sorted(kelvins) and kelvins[0] < 3200 and kelvins[-1] > 6000  # warm low, cold high


def test_tone_is_smoothed_over_about_300_ms():
    a = audiomod.Analyser()
    a.feed(tone(150, 2, -20))
    before = a.tone
    a.feed(tone(3000, 0.1, -20))  # 100 ms of a high note: part of the way only
    assert before < a.tone < 0.6


def test_silence_after_three_seconds_holds_the_low_end():
    a = audiomod.Analyser()
    a.feed(bursts(-30, 12))
    assert a.level is not None
    a.feed(silence(2.5))
    assert a.level is not None  # not yet
    a.feed(silence(0.6))
    assert a.silent and a.level is None
    assert audiomod.level_percent(a.level, 20.0, 80.0) == 20.0
    a.feed(bursts(-30, 1))
    assert not a.silent


def test_level_percent_maps_onto_the_range():
    assert audiomod.level_percent(0.5, 20, 80) == 50.0
    assert audiomod.level_percent(1.0, -50, 150) == 100.0  # the window's scale, clamped to 0-100
    assert audiomod.level_percent(0.0, -50, 150) == 0.0
    assert audiomod.level_percent(None, 30, 70) == 30.0


def test_deadband():
    hs = {"mode": "hs", "hue": 100.0, "sat": 100.0}
    ct = {"mode": "ct", "kelvin": 4000}
    assert audiomod.should_send(50.0, None, None, None, 0.4, True)  # nothing sent yet
    assert not audiomod.should_send(52.0, 50.0, None, None, 0.6, True)
    assert audiomod.should_send(53.0, 50.0, None, None, 0.6, True)
    assert not audiomod.should_send(50.0, 50.0, {**hs, "hue": 107.0}, hs, 0.6, True)
    assert audiomod.should_send(50.0, 50.0, {**hs, "hue": 108.0}, hs, 0.6, True)
    assert audiomod.should_send(50.0, 50.0, {**hs, "hue": 355.0}, {**hs, "hue": 3.0}, 0.6, True)  # around 0
    assert not audiomod.should_send(50.0, 50.0, {**ct, "kelvin": 4149}, ct, 0.6, True)
    assert audiomod.should_send(50.0, 50.0, {**ct, "kelvin": 4150}, ct, 0.6, True)
    assert audiomod.should_send(51.0, 50.0, None, None, 2.0, True)  # 2 s with any change
    assert not audiomod.should_send(50.0, 50.0, None, None, 5.0, False)


def test_rate_cap_never_above_three():
    assert audiomod.interval(2) == 0.5
    assert audiomod.interval(3) == pytest.approx(1 / 3)
    assert audiomod.interval(10) == pytest.approx(1 / 3)
    assert audiomod.check_rate("2") == 2
    for bad in ("0", "4", "x", "nan"):
        with pytest.raises(ValueError):
            audiomod.check_rate(bad)


def test_capture_commands(monkeypatch):
    monkeypatch.setattr(audiomod.shutil, "which", lambda name: f"/usr/bin/{name}")
    got = lambda source: audiomod.AudioCapture(lambda *_: None, source).command()  # noqa: E731
    default = got("")
    assert "stream.capture.sink = true" in " ".join(default) and "--target" not in default
    assert "16000" in default and "1" in default
    mon = got("alsa_output.x.monitor")
    assert mon[mon.index("--target") + 1] == "alsa_output.x" and "stream.capture.sink = true" in " ".join(mon)
    mic = got("alsa_input.y")
    assert mic[mic.index("--target") + 1] == "alsa_input.y" and "capture.sink" not in " ".join(mic)
    monkeypatch.setattr(audiomod.shutil, "which", lambda name: "/usr/bin/parec" if name == "parec" else None)
    assert "--device=@DEFAULT_MONITOR@" in got("")


# --------------------------------------------------------------------- the core

class FakeAudio:
    """The service without a capture: the test sets level and tone."""

    def __init__(self) -> None:
        self.on, self.source, self.calls = False, "", []
        self.lvl: float | None = None
        self.tn: float | None = None

    def apply(self, on, source):
        if (on, source) != (self.on, self.source):
            self.calls.append((on, source))
        self.on, self.source = on, source

    def stop(self):
        self.on = False

    def level(self):
        return self.lvl

    def tone(self):
        return self.tn

    def describe(self):
        return {"state": "listening" if self.on else "off", "error": ""}


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(audiomod, "missing", lambda: [])


def run_audio(tmp_path, state, body, fake=True):
    path = tmp_path / "state.json"
    if state is not None:
        save_state(path, state)

    async def main():
        d = Daemon(path, tmp_path / "sock", desktop=FakeDesktop())
        if fake:
            d.audio = FakeAudio()
        await d.start()
        follow = asyncio.create_task(d.follow())
        try:
            await settle(d)
            return await body(d)
        finally:
            follow.cancel()
            await d.stop()

    return asyncio.run(main())


def lit(**config):
    return {"power": True, "config": config}


def sends(m, bid, since=0):
    return [c for c in m.calls[since:] if c[1] == bid and c[0] in ("apply", "fade", "color")]


def test_on_off_and_persistence(tmp_path):
    async def body(d):
        g = await cmd(d, "audio", "on")
        assert g["audio"] is True and d.audio.on
        assert g["audio_rate"] == 2 and g["audio_color"] is True and g["audio_source"] == ""
        assert json.loads((tmp_path / "state.json").read_text())["bulbs"]["mock:1"]["audio"] is True
        await cmd(d, "audio-rate", "3")
        await cmd(d, "audio-source", "alsa_input.y")
        assert d.audio.source == "alsa_input.y"
        with pytest.raises(UserError):
            await cmd(d, "audio-rate", "4")
    run_audio(tmp_path, mock_state(mock_1=lit()), body)

    async def again(d):
        assert d.audio.on and d.audio.source == "alsa_input.y"  # running again after the restart
        g = await cmd(d, "get")
        assert g["audio"] is True and g["audio_rate"] == 3
        g = await cmd(d, "audio", "off")
        assert g["audio"] is False and not d.audio.on
        assert "audio" not in json.loads((tmp_path / "state.json").read_text())["bulbs"]["mock:1"]
    run_audio(tmp_path, None, again)


def test_capture_only_while_a_bulb_follows(tmp_path):
    async def body(d):
        assert not d.audio.on and d.audio.calls == []
        await cmd(d, "audio", "on", which="mock:1")
        assert d.audio.on
        await cmd(d, "bulb-set", "mock:1", "use=off")
        await settle(d)
        assert not d.audio.on
    run_audio(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


def test_level_follows_on_the_range_and_silence_holds_the_low_end(tmp_path):
    async def body(d):
        await cmd(d, "set", "range_lo=20")
        await cmd(d, "set", "range_hi=80")
        await cmd(d, "audio", "on")
        b = d.bulbs["mock:1"]
        d.audio.lvl = 0.5
        await settle(d, 8)
        assert b.sent == 50.0
        d.audio.lvl = None  # silence
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert b.sent == 20.0 and b.power  # held, never off
        await cmd(d, "set", "range_lo=0")
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert b.sent == d.min_lit(b) > 0  # the lowest lit step, not off
        g = await cmd(d, "get")
        assert g["audio_level"] == b.sent
    run_audio(tmp_path, mock_state(mock_1=lit()), body)


def test_pitch_colours_hs_and_ct_bulbs_not_plain_ones(tmp_path):
    async def body(d):
        await cmd(d, "audio", "on")
        d.audio.lvl, d.audio.tn = 0.8, 1.0
        await settle(d, 8)
        m = mock(d)
        assert m.colors["mock:1"] == {"mode": "hs", "hue": 300.0, "sat": 100.0}
        assert m.colors["mock:2"] == {"mode": "ct", "kelvin": 6500}
        assert "mock:3" not in m.colors
        await asyncio.sleep(0.6)
        d.audio.tn = 0.0
        await settle(d, 8)
        assert m.colors["mock:2"] == {"mode": "ct", "kelvin": 2700}  # warm low
        await cmd(d, "audio-color", "off")  # back to the profile's colour (hue 30, sat 0)
        await asyncio.sleep(0.6)
        await settle(d, 8)
        assert m.colors["mock:1"] == {"mode": "hs", "hue": 30.0, "sat": 0.0}
    run_audio(tmp_path, mock_state(mock_1=lit(color="hs"), mock_2=lit(color="ct"), mock_3=lit()), body)


@pytest.mark.parametrize("rate", [2, 3])
def test_never_more_updates_than_the_rate(tmp_path, rate):
    async def body(d):
        if rate != 2:
            await cmd(d, "audio-rate", str(rate))
        await cmd(d, "audio", "on")
        m = mock(d)
        stamps = []
        orig = m.apply_color

        async def color(bulb, c):
            stamps.append(("color", bulb.id, time.monotonic()))
            return await orig(bulb, c)

        async def fade(bulb, percent, seconds):
            stamps.append(("level", bulb.id, time.monotonic()))
            assert seconds == pytest.approx(1 / rate)  # the transition spans the interval
            m.applied[bulb.id] = percent
            m.calls.append(("fade", bulb.id, percent))
            return True
        m.apply_color, m.fade = color, fade
        t0 = time.monotonic()
        i = 0
        while time.monotonic() - t0 < 2.5:  # the loudest possible input: a jump every block
            i += 1
            d.audio.lvl, d.audio.tn = (1.0, 1.0) if i % 2 else (0.0, 0.0)
            d.on_audio_block()
            await asyncio.sleep(0.05)
        for bid in ("mock:1", "mock:2"):
            times = sorted({t for _w, b, t in stamps if b == bid})
            updates = [t for k, (t) in enumerate(times) if k == 0 or t - times[k - 1] > 0.05]  # level+colour: one
            gaps = [b - a for a, b in zip(updates, updates[1:])]
            assert all(gap >= 1 / rate - 0.01 for gap in gaps)
            for t in updates:
                assert sum(1 for u in updates if t <= u < t + 1.0) <= rate
            # and it kept up: a wake just under one interval after the last send
            # brings a send when the cap allows it (Daemon.serve_follow)
            assert len(updates) >= rate * 2 - 1
        g = await cmd(d, "get")
        assert g["audio_status"]["messages"] == len(stamps)
    run_audio(tmp_path, mock_state(mock_1=lit(color="hs", transition=True), mock_2=lit()), body)


def test_mesh_default_rate_is_two():
    from haus.profiles import new_state
    assert new_state()["settings"]["follow_rate"] == 2 == audiomod.DEFAULT_RATE  # audio_rate became follow_rate


def test_small_changes_wait_for_the_deadband(tmp_path):
    async def body(d):
        await cmd(d, "audio", "on")
        b = d.bulbs["mock:1"]
        d.audio.lvl = 0.5
        await settle(d, 6)
        n = len(sends(mock(d), "mock:1"))
        d.audio.lvl = 0.52  # 2 %: under the 3 % deadband
        await asyncio.sleep(0.8)
        await settle(d, 4)
        assert len(sends(mock(d), "mock:1")) == n and b.sent == 50.0
        await asyncio.sleep(1.5)  # 2 s since the last send: any change goes
        await settle(d, 4)
        assert b.sent == 52.0
        d.audio.lvl = 0.6
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert b.sent == 60.0
    run_audio(tmp_path, mock_state(mock_1=lit()), body)


@pytest.mark.parametrize("how", ["slider", "manual", "automatic", "dim", "wheel"])
def test_slider_and_manual_end_it(tmp_path, how):
    async def body(d):
        await cmd(d, "audio", "on")
        d.audio.lvl = 1.0
        await settle(d, 4)
        if how == "slider":
            await cmd(d, "bulb", "40")
        elif how == "manual":
            await cmd(d, "pause")
        elif how == "automatic":
            await cmd(d, "resume")
        elif how == "dim":
            await cmd(d, "dim", "30")
        else:
            await cmd(d, "mode", "off")
            await cmd(d, "nudge", "-5")
        g = await cmd(d, "get")
        assert g["audio"] is False and not d.audio.on
        assert not d.bulbs["mock:1"].audio
        if how == "slider":
            await settle(d, 4)
            assert d.bulbs["mock:1"].sent == 40.0
    run_audio(tmp_path, mock_state(mock_1=lit()), body)


def test_clap_still_toggles_power(tmp_path):
    async def body(d):
        await cmd(d, "audio", "on")
        d.audio.lvl = 1.0
        await settle(d, 4)
        d.set_power(d.selected(1), False, "double clap")
        await settle(d, 4)
        assert mock(d).applied["mock:1"] == 0.0 and d.bulbs["mock:1"].audio  # off, still following
        d.set_power(d.selected(1), True, "double clap")
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert mock(d).applied["mock:1"] == 100.0
    run_audio(tmp_path, mock_state(mock_1=lit()), body)


def test_identify_and_test_bulbs_are_left_alone(tmp_path, monkeypatch):
    from haus import actions
    monkeypatch.setattr(actions, "IDENTIFY_HALF_S", 0.02)

    async def body(d):
        await cmd(d, "audio", "on")
        m = mock(d)
        await cmd(d, "identify", "mock:1")
        i0 = len(m.calls)
        t0 = time.monotonic()
        k = 0
        while time.monotonic() - t0 < 1.5:
            k += 1
            d.audio.lvl, d.audio.tn = (0.9, 0.9) if k % 2 else (0.1, 0.1)
            d.on_audio_block()
            await asyncio.sleep(0.05)
        assert {c[0] for c in sends(m, "mock:1", i0)} <= {"apply"}  # the blinking only
        assert {c[2] for c in sends(m, "mock:1", i0)} <= {0.0, 100.0}
        assert sends(m, "mock:2", i0)  # the other bulb follows the audio
        await cmd(d, "identify", "mock:1", "off")
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert m.applied["mock:1"] == d.audio_want(d.bulbs["mock:1"])  # given back to the audio
    run_audio(tmp_path, mock_state(mock_1=lit(color="hs"), mock_2=lit(color="hs")), body)


def test_a_capture_failure_turns_it_off(tmp_path, monkeypatch):
    # the real service; the recorder is a program that exits at once
    monkeypatch.setattr(audiomod.AudioCapture, "command",
                        lambda self: [sys.executable, "-c", "import sys; sys.stderr.write('source gone')"])

    async def body(d):
        g = await cmd(d, "audio", "on")
        assert g["audio"] is True
        for _ in range(100):
            await asyncio.sleep(0.02)
            if d.audio.state == "error":
                break
        await settle(d)
        g = await cmd(d, "get")
        assert g["audio"] is False and not d.bulbs["mock:1"].audio
        assert g["audio_status"]["state"] == "error" and "source gone" in g["audio_status"]["error"]
        assert d.audio.task is None or d.audio.task.done()  # no retry loop
        await settle(d, 5)
        assert d.audio.task is None or d.audio.task.done()
    run_audio(tmp_path, mock_state(mock_1=lit()), body, fake=False)


def test_missing_parts_refuse(tmp_path, monkeypatch):
    async def body(d):
        monkeypatch.setattr(audiomod, "missing", lambda: ["pw-record"])
        with pytest.raises(UserError):
            await cmd(d, "audio", "on")
        assert not d.bulbs["mock:1"].audio
    run_audio(tmp_path, mock_state(mock_1=lit()), body)
