"""Follow video (haus/videofollow.py and the core's side): synthetic frames
only, never a real portal or screen; the daemon tests swap the service for a
fake, or the portal for a fake and gst-launch for a small Python program."""

import asyncio
import json
import math
import os
import sys
import time

import pytest

from haus import audiofollow as audiomod
from haus import followrate
from haus import videofollow as videomod
from haus.common import UserError, save_state
from haus.daemon import Daemon
from test_core import FakeDesktop, cmd, mock, mock_state, settle

W, H = videomod.W, videomod.H


def frame(pixel) -> bytes:
    """A W×H frame; pixel(x, y) -> (r, g, b) 0-255."""
    return bytes(c for y in range(H) for x in range(W) for c in pixel(x, y))


def solid(r, g, b) -> bytes:
    return bytes((r, g, b)) * (W * H)


# --------------------------------------------------------------------- analysis

def test_region_bounds_halves_and_quarters():
    rows_top, rows_bottom = slice(0, 14), slice(13, 27)  # 27 rows: the middle one in both
    cols_left, cols_right = slice(0, 24), slice(24, 48)
    every_r, every_c = slice(0, 27), slice(0, 48)
    want = {"whole": (every_r, every_c), "left": (every_r, cols_left), "right": (every_r, cols_right),
            "top": (rows_top, every_c), "bottom": (rows_bottom, every_c),
            "top-left": (rows_top, cols_left), "top-right": (rows_top, cols_right),
            "bottom-left": (rows_bottom, cols_left), "bottom-right": (rows_bottom, cols_right)}
    assert set(want) == set(videomod.REGIONS)
    for region, bounds in want.items():
        assert videomod.region_bounds(region) == bounds, region
    assert videomod.region_bounds("top-left", 4, 4) == (slice(0, 2), slice(0, 2))


def test_every_region_takes_its_own_pixels():
    # red: the left half; green: rows 0-12 (above the shared middle row 13); blue: everywhere
    f = frame(lambda x, y: (255 if x < 24 else 0, 255 if y < 13 else 0, 255))
    means = videomod.frame_means(f)
    top_g = 13 / 14  # 14 rows in the top half, 13 of them green
    whole_g = 13 / 27
    want = {"whole": (0.5, whole_g), "left": (1.0, whole_g), "right": (0.0, whole_g),
            "top": (0.5, top_g), "bottom": (0.5, 0.0),
            "top-left": (1.0, top_g), "top-right": (0.0, top_g),
            "bottom-left": (1.0, 0.0), "bottom-right": (0.0, 0.0)}
    for region, (r, g) in want.items():
        mr, mg, mb = means[region]
        assert mr == pytest.approx(r) and mg == pytest.approx(g) and mb == pytest.approx(1.0), region


def test_mean_in_linear_light():
    # half black, half white: the mean light is half of white, which looks like sRGB 188, not 128
    f = frame(lambda x, y: (255, 255, 255) if x < 24 else (0, 0, 0))
    lum = videomod.luminance(videomod.frame_means(f)["whole"])
    assert lum == pytest.approx(0.5)
    grey = videomod.luminance(videomod.frame_means(solid(128, 128, 128))["whole"])
    assert grey == pytest.approx(0.2158, abs=0.001)
    with pytest.raises(ValueError):
        videomod.frame_means(b"\0" * 10)


def test_brightness_maps_luminance_onto_zero_to_max():
    assert videomod.brightness(1.0, 100) == 100.0
    assert videomod.brightness(1.0, 60) == 60.0
    assert videomod.brightness(0.5, 60) == 30.0
    assert videomod.brightness(0.0, 60) == 0.0
    assert videomod.brightness(2.0, 150) == 100.0  # clamped
    white = videomod.luminance(videomod.frame_means(solid(255, 255, 255))["whole"])
    assert videomod.brightness(white, 80) == 80.0


def test_smoothing_over_wall_time():
    a = videomod.Analyser(smooth=0.5)
    a.feed(solid(0, 0, 0), 10.0)
    assert a.luminance("whole") == 0.0
    a.feed(solid(255, 255, 255), 10.5)  # one time constant later: 1 - 1/e of the way
    assert a.luminance("whole") == pytest.approx(1 - math.exp(-1), abs=1e-6)
    a.feed(solid(255, 255, 255), 13.0)
    assert a.luminance("whole") > 0.99
    off = videomod.Analyser(smooth=0)
    off.feed(solid(0, 0, 0), 0.0)
    off.feed(solid(255, 255, 255), 0.2)
    assert off.luminance("whole") == pytest.approx(1.0)
    slow = videomod.Analyser(smooth=5)
    slow.feed(solid(0, 0, 0), 0.0)
    slow.feed(solid(255, 255, 255), 0.2)
    assert slow.luminance("whole") < 0.05


def test_hue_and_saturation_as_it_looks():
    h, s = videomod.hue_sat(videomod.frame_means(solid(255, 0, 0))["whole"])
    assert (h, s) == (0.0, 100.0)
    h, s = videomod.hue_sat(videomod.frame_means(solid(0, 0, 255))["whole"])
    assert h == 240.0 and s == 100.0
    h, s = videomod.hue_sat(videomod.frame_means(solid(255, 128, 0))["whole"])
    assert 28 <= h <= 32 and s == 100.0
    assert videomod.hue_sat(videomod.frame_means(solid(90, 90, 90))["whole"])[1] == 0.0


def test_settings_checks():
    assert videomod.check_region("Top-Left") == "top-left"
    assert videomod.check_region("up") == "top" and videomod.check_region("down") == "bottom"
    assert videomod.check_smooth("0.2") == 0.2 and videomod.check_smooth("1s") == 1.0
    assert videomod.check_max("70.0") == 70
    for bad in ("middle", ""):
        with pytest.raises(ValueError):
            videomod.check_region(bad)
    for bad in ("0.3", "x", "-1"):
        with pytest.raises(ValueError):
            videomod.check_smooth(bad)
    for bad in ("101", "-1", "nan"):
        with pytest.raises(ValueError):
            videomod.check_max(bad)


def test_rate_cap_and_deadband_are_shared_with_audio():
    for name in ("interval", "should_send", "color_jump", "DEFAULT_RATE", "MAX_RATE", "LEVEL_STEP", "HOLD_S"):
        assert getattr(audiomod, name) is getattr(followrate, name)
    assert followrate.interval(9) == pytest.approx(1 / 3)


def test_migration_renames_audio_rate_to_follow_rate():
    from haus.profiles import migrate, new_state
    state = new_state()
    s = state["settings"]
    s["audio_rate"] = s.pop("follow_rate") + 1
    state["bulbs"] = {"mock:1": {"kind": "mock", "audio": True, "video": True, "video_region": "sideways"},
                      "mock:2": {"kind": "mock", "video": True, "video_region": "left"}}
    migrate(state)
    assert s["follow_rate"] == 3 and "audio_rate" not in s
    assert "video" not in state["bulbs"]["mock:1"] and "video_region" not in state["bulbs"]["mock:1"]
    assert state["bulbs"]["mock:2"]["video"] is True and state["bulbs"]["mock:2"]["video_region"] == "left"


# --------------------------------------------------------------------- the core

class FakeVideo:
    """The service without a portal or capture: the test sets the means."""

    def __init__(self) -> None:
        self.on, self.calls, self.tokens = False, [], []
        self.means: dict | None = None
        self.smooth = videomod.DEFAULT_SMOOTH

    def apply(self, on, token=""):
        if on != self.on:
            self.calls.append(on)
            if on:
                self.tokens.append(token)
        self.on = on

    def stop(self):
        self.on = False

    def set_smooth(self, s):
        self.smooth = s

    def show(self, r, g, b, region=None):
        rgb = tuple(videomod.LINEAR[c] for c in (r, g, b))
        if region is None or self.means is None:
            self.means = {k: rgb for k in videomod.REGIONS}
        else:
            self.means[region] = rgb

    def color(self, region):
        return None if self.means is None else self.means[region]

    def luminance(self, region):
        c = self.color(region)
        return None if c is None else videomod.luminance(c)

    def describe(self):
        return {"state": "capturing" if self.on else "off", "error": ""}


class FakeAudio:
    def __init__(self) -> None:
        self.on = False

    def apply(self, on, source):
        self.on = on

    def stop(self):
        self.on = False

    def level(self):
        return 0.5

    def tone(self):
        return None

    def describe(self):
        return {"state": "listening" if self.on else "off", "error": ""}


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(videomod, "missing", lambda: [])
    monkeypatch.setattr(audiomod, "missing", lambda: [])


def run_video(tmp_path, state, body, fake=True):
    path = tmp_path / "state.json"
    if state is not None:
        save_state(path, state)

    async def main():
        d = Daemon(path, tmp_path / "sock", desktop=FakeDesktop())
        d.audio = FakeAudio()
        if fake:
            d.video = FakeVideo()
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


def saved(tmp_path) -> dict:
    return json.loads((tmp_path / "state.json").read_text())


async def wait_for(cond, d, tries=150):
    for _ in range(tries):
        await asyncio.sleep(0.02)
        if cond():
            break
    await settle(d)


def test_on_off_region_and_settings(tmp_path):
    async def body(d):
        g = await cmd(d, "video", "on")
        assert g["video"] is True and d.video.on and g["video_region"] == "whole"
        assert g["video_max"] == 100 and g["video_smooth"] == 0.5 and g["follow_rate"] == 2
        assert saved(tmp_path)["bulbs"]["mock:1"]["video"] is True
        g = await cmd(d, "video-region", "mock:1", "left")
        assert g["video_region"] == "left" and g["bulbs_list"][0]["video_region"] == "left"
        await cmd(d, "video-max", "70")
        await cmd(d, "video-smooth", "2")
        assert d.video.smooth == 2.0
        g = await cmd(d, "follow-rate", "3")
        assert g["follow_rate"] == 3 == g["audio_rate"]
        await cmd(d, "audio-rate", "1")  # the old name
        assert d.settings["follow_rate"] == 1
        for bad in (("video-region", "mock:1", "middle"), ("video-max", "120"), ("video-smooth", "0.3")):
            with pytest.raises(UserError):
                await cmd(d, *bad)
        assert "video_token" not in json.dumps(await cmd(d, "get"))  # never in get
        g = await cmd(d, "video", "off")
        assert g["video"] is False and not d.video.on
        assert "video" not in saved(tmp_path)["bulbs"]["mock:1"]
        assert saved(tmp_path)["bulbs"]["mock:1"]["video_region"] == "left"  # kept for next time
    run_video(tmp_path, mock_state(mock_1=lit()), body)


def test_brightness_on_zero_to_max_never_below_lowest_lit(tmp_path):
    async def body(d):
        await cmd(d, "video-max", "60")
        await cmd(d, "video", "on")
        b = d.bulbs["mock:1"]
        d.video.show(255, 255, 255)
        await settle(d, 8)
        assert b.sent == 60.0
        d.video.show(0, 0, 0)
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert b.sent == d.min_lit(b) > 0 and b.power  # black: the lowest lit step, never off
        g = await cmd(d, "get")
        assert g["video_level"] == b.sent
    run_video(tmp_path, mock_state(mock_1=lit()), body)


def test_each_bulb_follows_its_region(tmp_path):
    async def body(d):
        await cmd(d, "video", "on")
        await cmd(d, "video-region", "mock:1", "left")
        await cmd(d, "video-region", "mock:2", "right")
        d.video.show(0, 0, 0)
        d.video.show(255, 255, 255, "left")
        await settle(d, 8)
        assert d.bulbs["mock:1"].sent == 100.0
        assert d.bulbs["mock:2"].sent == d.min_lit(d.bulbs["mock:2"])
    run_video(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


def test_hue_on_colour_bulbs_brightness_only_on_white_and_plain(tmp_path):
    async def body(d):
        await cmd(d, "video", "on")
        d.video.show(0, 0, 255)
        await settle(d, 8)
        m = mock(d)
        assert m.colors["mock:1"] == {"mode": "hs", "hue": 240.0, "sat": 100.0}
        assert "mock:2" not in m.colors and "mock:3" not in m.colors  # white-only and plain: left alone
        for bid in ("mock:2", "mock:3"):
            assert d.bulbs[bid].sent == pytest.approx(videomod.brightness(videomod.LINEAR[255] * 0.0722, 100))
        g = await cmd(d, "get")
        assert g["bulbs_list"][0]["video"] is True
    run_video(tmp_path, mock_state(mock_1=lit(color="hs"), mock_2=lit(color="ct"), mock_3=lit()), body)


def test_never_more_updates_than_the_rate(tmp_path):
    async def body(d):
        await cmd(d, "video", "on")
        m = mock(d)
        stamps = []
        orig = m.apply_color

        async def color(bulb, c):
            stamps.append(("color", bulb.id, time.monotonic()))
            return await orig(bulb, c)

        async def fade(bulb, percent, seconds):
            stamps.append(("level", bulb.id, time.monotonic()))
            m.applied[bulb.id] = percent
            m.calls.append(("fade", bulb.id, percent))
            return True
        m.apply_color, m.fade = color, fade
        t0 = time.monotonic()
        i = 0
        while time.monotonic() - t0 < 2.5:  # a strobe on screen: a jump every frame
            i += 1
            d.video.show(*((255, 0, 0) if i % 2 else (0, 0, 255)))
            d.on_video_frame()
            await asyncio.sleep(0.05)
        times = sorted({t for _w, b, t in stamps if b == "mock:1"})
        updates = [t for k, t in enumerate(times) if k == 0 or t - times[k - 1] > 0.05]
        gaps = [b - a for a, b in zip(updates, updates[1:])]
        assert all(gap >= 0.5 - 0.01 for gap in gaps)  # follow_rate 2
        # and it kept up: a wake just under one interval after the last send
        # brings a send when the cap allows it (Daemon.serve_follow)
        assert len(updates) >= 3
        g = await cmd(d, "get")
        assert g["video_status"]["messages"] == len(stamps) and g["audio_status"]["messages"] == 0
    run_video(tmp_path, mock_state(mock_1=lit(color="hs", transition=True)), body)


def test_audio_and_video_are_exclusive_per_bulb(tmp_path):
    async def body(d):
        await cmd(d, "audio", "on", which="mock:1")
        await cmd(d, "audio", "on", which="mock:2")
        assert d.audio.on
        g = await cmd(d, "video", "on", which="mock:1")
        b1, b2 = d.bulbs["mock:1"], d.bulbs["mock:2"]
        assert b1.video and not b1.audio and b2.audio and not b2.video
        assert d.audio.on and d.video.on
        await cmd(d, "video", "on", which="mock:2")
        assert not d.audio.on and d.video.on  # nothing follows audio any more: the capture stops
        await cmd(d, "audio", "on", which="mock:1")
        assert b1.audio and not b1.video and d.audio.on and d.video.on
        entries = saved(tmp_path)["bulbs"]
        assert entries["mock:1"].get("audio") is True and "video" not in entries["mock:1"]
        g = await cmd(d, "get")
        assert g["bulbs_list"][0]["audio"] is True and g["bulbs_list"][0]["video"] is False
    run_video(tmp_path, mock_state(mock_1=lit(), mock_2=lit()), body)


@pytest.mark.parametrize("how", ["slider", "manual", "automatic", "dim", "use-off"])
def test_slider_and_manual_end_it(tmp_path, how):
    async def body(d):
        await cmd(d, "video", "on")
        d.video.show(255, 255, 255)
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
            await cmd(d, "bulb-set", "mock:1", "use=off")
        await settle(d, 4)
        assert not d.bulbs["mock:1"].video and not d.video.on
        if how == "slider":
            assert d.bulbs["mock:1"].sent == 40.0
    run_video(tmp_path, mock_state(mock_1=lit()), body)


def test_clap_power_and_identify(tmp_path, monkeypatch):
    from haus import actions
    monkeypatch.setattr(actions, "IDENTIFY_HALF_S", 0.02)

    async def body(d):
        await cmd(d, "video", "on")
        d.video.show(255, 255, 255)
        await settle(d, 4)
        d.set_power(d.selected(1), False, "double clap")
        await settle(d, 4)
        assert mock(d).applied["mock:1"] == 0.0 and d.bulbs["mock:1"].video  # off, still following
        d.set_power(d.selected(1), True, "double clap")
        await asyncio.sleep(0.6)
        await settle(d, 4)
        assert mock(d).applied["mock:1"] == 100.0
        m = mock(d)
        await cmd(d, "identify", "mock:1")
        i0 = len(m.calls)
        for k in range(20):
            d.video.show(*((255, 255, 255) if k % 2 else (40, 40, 40)))
            d.on_video_frame()
            await asyncio.sleep(0.05)
        assert {c[2] for c in m.calls[i0:] if c[1] == "mock:1" and c[0] in ("apply", "fade")} <= {0.0, 100.0}
        await cmd(d, "identify", "mock:1", "off")
    run_video(tmp_path, mock_state(mock_1=lit()), body)


# ------------------------------------------------- the real service, fake portal

class FakeCast:
    """Stands in for videomod.ScreenCast: a pipe as the PipeWire fd."""
    opened: list = []
    refuse = False

    def __init__(self):
        self.closed = asyncio.Event()
        self.asking = False
        self.fds = []

    async def open(self, token=""):
        FakeCast.opened.append(token)
        if FakeCast.refuse:
            raise videomod.VideoError("screen sharing was refused")
        r, w = os.pipe()
        os.close(w)
        return r, 42, "token-new"

    def close(self):
        self.closed.set()


@pytest.fixture
def fake_cast(monkeypatch):
    FakeCast.opened, FakeCast.refuse = [], False
    monkeypatch.setattr(videomod, "ScreenCast", FakeCast)
    return FakeCast


def program(code: str):
    return lambda self: [sys.executable, "-c", code]


def test_frames_from_the_capture_reach_the_bulbs(tmp_path, monkeypatch, fake_cast):
    # gst-launch's place: a program that writes white frames at 5 per second
    monkeypatch.setattr(videomod.VideoCapture, "command", program(
        "import sys, time\n"
        f"f = bytes([255]) * {videomod.FRAME}\n"
        "for _ in range(100):\n"
        "    sys.stdout.buffer.write(f); sys.stdout.flush(); time.sleep(0.2)\n"))

    async def body(d):
        await cmd(d, "video-smooth", "0")
        await cmd(d, "video", "on")
        await wait_for(lambda: d.video.state == "capturing" and d.video.analyser and d.video.analyser.frames, d)
        await asyncio.sleep(0.6)  # past the rate cap of the update sent before the first frame
        await settle(d, 6)
        assert d.bulbs["mock:1"].sent == 100.0
        assert d.settings["video_token"] == "token-new" and fake_cast.opened == [""]
        assert saved(tmp_path)["settings"]["video_token"] == "token-new"
        g = await cmd(d, "video-status")
        assert g["state"] == "capturing" and g["frames"] >= 1 and g["permission"] is True
        await cmd(d, "video", "off")
        assert d.video.state == "off" and d.video.capture is None
    run_video(tmp_path, mock_state(mock_1=lit()), body, fake=False)


def test_a_capture_failure_turns_it_off(tmp_path, monkeypatch, fake_cast):
    monkeypatch.setattr(videomod.VideoCapture, "command",
                        program("import sys; sys.stderr.write('stream gone')"))

    async def body(d):
        g = await cmd(d, "video", "on")
        assert g["video"] is True
        await wait_for(lambda: d.video.state == "error", d)
        g = await cmd(d, "get")
        assert g["video"] is False and not d.bulbs["mock:1"].video
        assert g["video_status"]["state"] == "error" and "stream gone" in g["video_status"]["error"]
        await settle(d, 5)
        assert d.video.task is None or d.video.task.done()  # no retry loop
        assert fake_cast.opened == [""]
    run_video(tmp_path, mock_state(mock_1=lit()), body, fake=False)


def test_a_refused_dialog_turns_it_off(tmp_path, fake_cast):
    fake_cast.refuse = True

    async def body(d):
        await cmd(d, "video", "on")
        await wait_for(lambda: d.video.state == "error", d)
        g = await cmd(d, "get")
        assert g["video"] is False and "refused" in g["video_status"]["error"]
        assert d.settings["video_token"] == ""
    run_video(tmp_path, mock_state(mock_1=lit()), body, fake=False)


def test_restart_without_a_usable_token_stays_off_with_a_hint(tmp_path, monkeypatch, fake_cast):
    usable = {}

    async def token_usable(token):
        return usable.get(token, False)
    monkeypatch.setattr(videomod, "token_usable", token_usable)
    state = mock_state(mock_1=lit())
    state["bulbs"]["mock:1"]["video"] = True

    async def no_token(d):
        g = await cmd(d, "get")
        assert g["video"] is False and not d.bulbs["mock:1"].video
        assert g["video_status"]["hint"] == videomod.NEEDS_PERMISSION
        assert fake_cast.opened == []  # no dialog unasked
        assert "video" not in saved(tmp_path)["bulbs"]["mock:1"]
    run_video(tmp_path, state, no_token, fake=False)

    state["settings"]["video_token"] = "token-old"  # the permission store forgot it
    run_video(tmp_path, state, no_token, fake=False)

    usable["token-old"] = True  # still granted: it resumes with the token

    async def resumes(d):
        assert d.bulbs["mock:1"].video and d.video.on
        assert d.video.tokens == ["token-old"]
        assert (await cmd(d, "get"))["video_status"]["hint"] == ""
    run_video(tmp_path, state, resumes)


def test_forget_clears_the_token(tmp_path, monkeypatch):
    forgotten = []

    async def forget(token):
        forgotten.append(token)
    monkeypatch.setattr(videomod, "forget_token", forget)

    async def body(d):
        d.settings["video_token"] = "token-1"
        g = await cmd(d, "video-forget")
        assert g["video_status"]["permission"] is False and forgotten == ["token-1"]
        assert saved(tmp_path)["settings"]["video_token"] == ""
    run_video(tmp_path, mock_state(mock_1=lit()), body)


def test_missing_parts_refuse(tmp_path, monkeypatch):
    async def body(d):
        monkeypatch.setattr(videomod, "missing", lambda: ["pipewiresrc"])
        with pytest.raises(UserError):
            await cmd(d, "video", "on")
        assert not d.bulbs["mock:1"].video
    run_video(tmp_path, mock_state(mock_1=lit()), body)


def test_gst_command():
    c = videomod.VideoCapture(7, 99, lambda *_: None).command()
    text = " ".join(c)
    assert "pipewiresrc fd=7 path=99" in text and f"max-rate={videomod.FPS}" in text
    assert f"width={W},height={H}" in text and "format=RGB" in text and "fdsink fd=1" in text
