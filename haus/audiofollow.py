"""Follow audio: the bulbs a user switched to it follow what the computer plays.

    source ── AudioCapture (clap.Capture: pw-record/parec, mono 16 kHz s16, a pipe)
       │       default: the monitor of the default output, as a passive stream
       ▼       (it never wakes a suspended sink: no data while nothing plays)
    Analyser, per 50 ms block:
       RMS dB ─▶ envelope (attack 50 ms, release 400 ms)
              ─▶ 0…1 between an adaptive floor and ceiling (the 10th and 95th
                 percentile of the last 10 s of sound), so quiet and loud music
                 both use the whole range; silence (below the floor for 3 s,
                 or no data for 3 s) holds the low end
       spectrum ─▶ spectral centroid ─▶ 0…1 on a log scale 100 Hz…4 kHz,
                   smoothed over 300 ms (only while there is sound)
    daemon (haus/daemon.py): level → the profile's range_lo…range_hi (never
       below the lowest lit step: the bulb is never switched off); tone → hue
       0–300° on colour bulbs, the bulb's own Kelvin range on white ones (warm
       low, cold high); sent at most `audio_rate` (1–3, default 2) times a
       second per bulb, and only past the deadband (should_send).

Nothing is written to disk and nothing leaves the computer. numpy is the
optional extra clap detection uses too (requirements-clap.txt); this module
imports it lazily.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import math
import shutil
import time
from collections import deque
from collections.abc import Callable

from . import clap as clapmod
from .clap import CaptureError

log = logging.getLogger("hauslicht")

RATE = clapmod.RATE  # 16 kHz: the centroid's band ends at 8 kHz, plenty
BLOCK = 800  # 50 ms
BLOCK_S = BLOCK / RATE
ATTACK_S, RELEASE_S = 0.05, 0.4  # the level's envelope
TONE_S = 0.3  # the centroid's smoothing
HISTORY_S = 10.0  # the floor and ceiling follow this much of sound
WARMUP_S = 1.0  # until this much sound was heard: the default floor and ceiling
FLOOR_PCT, CEIL_PCT = 10, 95
DEFAULT_FLOOR_DB, DEFAULT_CEIL_DB = -50.0, -15.0
MIN_SPAN_DB = 12.0  # a steady sound is not stretched over the whole range
SILENT_DB = -70.0  # below: digital silence or hiss, never part of the floor and ceiling
SILENCE_S = 3.0  # below the floor this long (or no data): silence
F_LO, F_HI = 100.0, 4000.0  # the centroid's log scale
BAND = (50.0, 8000.0)  # what the centroid is taken over
HUE_MAX = 300.0  # low red … high magenta (not back to red)

RATES = (1, 2, 3)
DEFAULT_RATE = 2  # Bluetooth mesh: each update burns a sequence number
MAX_RATE = 3  # hard cap for every kind: photosensitivity
LEVEL_STEP = 3.0  # percent points: the deadband
HUE_STEP = 8.0  # degrees
CT_STEP = 150  # Kelvin
SAT_STEP = 8.0
HOLD_S = 2.0  # after this long any change is sent


def _coef(tau: float) -> float:
    return 1.0 - math.exp(-BLOCK_S / tau)


A_ATTACK, A_RELEASE, A_TONE = _coef(ATTACK_S), _coef(RELEASE_S), _coef(TONE_S)


# --------------------------------------------------------------------- settings

def check_source(value) -> str:
    """A source name as `audio-sources` lists it (monitors too); "" or
    "default": the monitor of the default output."""
    value = str(value or "").strip()
    if value.lower() in ("default", "@default_monitor@"):
        return ""
    if any(c.isspace() for c in value) or len(value) > 200:
        raise ValueError("audio_source must be a source name (hauslicht-ctl audio-sources) or default")
    return value


def check_rate(value) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("audio_rate must be 1, 2 or 3") from None
    if not math.isfinite(v) or round(v) not in RATES:
        raise ValueError("audio_rate must be 1, 2 or 3")
    return int(round(v))


def missing() -> list[str]:
    """What the mode lacks: numpy and a recorder."""
    out = ["numpy"] if importlib.util.find_spec("numpy") is None else []
    if not (shutil.which("pw-record") or shutil.which("parec")):
        out.append("pw-record")
    return out


# --------------------------------------------------------------------- mapping

def level_percent(level: float | None, lo: float, hi: float) -> float:
    """0…1 (None: silence) onto the brightness range lo…hi (clamped to 0…100)."""
    lo, hi = (max(0.0, min(100.0, float(v))) for v in (lo, hi))
    hi = max(lo, hi)
    x = 0.0 if level is None else max(0.0, min(1.0, level))
    return round(lo + x * (hi - lo), 1)


def tone_hue(tone: float) -> float:
    return round(HUE_MAX * max(0.0, min(1.0, tone)), 1)


def tone_kelvin(tone: float, ct_range: tuple[float, float]) -> int:
    lo, hi = ct_range
    return round(lo + max(0.0, min(1.0, tone)) * (hi - lo))


def centroid_tone(hz: float) -> float:
    """A centroid in Hz onto 0…1, log scale F_LO…F_HI."""
    if hz <= F_LO:
        return 0.0
    return max(0.0, min(1.0, math.log(hz / F_LO) / math.log(F_HI / F_LO)))


def color_jump(new: dict | None, old: dict | None) -> bool:
    """A colour past the deadband: hue ≥ HUE_STEP, Kelvin ≥ CT_STEP, another mode."""
    if new is None:
        return False
    if old is None or new.get("mode") != old.get("mode"):
        return True
    if new["mode"] == "ct":
        return abs(new["kelvin"] - old["kelvin"]) >= CT_STEP
    dh = abs(new["hue"] - old["hue"]) % 360
    return min(dh, 360 - dh) >= HUE_STEP or abs(new["sat"] - old["sat"]) >= SAT_STEP


def interval(rate: int) -> float:
    """Seconds between two updates of one bulb: never under 1 / MAX_RATE."""
    return 1.0 / max(1, min(MAX_RATE, int(rate)))


def should_send(level: float, sent_level: float | None, color: dict | None, sent_color: dict | None,
                since_s: float, changed: bool) -> bool:
    """The deadband, after the rate cap allowed an update: brightness moved
    LEVEL_STEP, the colour jumped, or HOLD_S passed with any change at all."""
    if sent_level is None or abs(level - sent_level) >= LEVEL_STEP or color_jump(color, sent_color):
        return True
    return changed and since_s >= HOLD_S


# --------------------------------------------------------------------- analyser

class Analyser:
    """Fed float samples (−1…1, RATE); keeps `level` (0…1, None in silence)
    and `tone` (0…1, None until something sounded). Block time, not wall
    time: tests feed it synthetic signals as fast as they like."""

    def __init__(self) -> None:
        import numpy as np
        self.np = np
        self.rest = np.zeros(0, dtype=np.float32)
        self.hann = np.hanning(BLOCK)
        freqs = np.fft.rfftfreq(BLOCK, 1.0 / RATE)
        self.band = (freqs >= BAND[0]) & (freqs <= BAND[1])
        self.freqs = freqs[self.band]
        self.env_db: float | None = None
        self.history: deque[float] = deque(maxlen=round(HISTORY_S / BLOCK_S))
        self.floor_db, self.ceil_db = DEFAULT_FLOOR_DB, DEFAULT_CEIL_DB
        self.quiet_s = SILENCE_S  # silent until something sounds
        self.tone: float | None = None
        self.centroid: float | None = None
        self.block_db = -120.0
        self.blocks = 0

    @property
    def silent(self) -> bool:
        return self.quiet_s >= SILENCE_S

    @property
    def level(self) -> float | None:
        if self.silent or self.env_db is None:
            return None
        return max(0.0, min(1.0, (self.env_db - self.floor_db) / (self.ceil_db - self.floor_db)))

    def feed(self, samples) -> int:
        """Whole blocks of samples (the rest waits); returns how many."""
        np = self.np
        data = np.concatenate((self.rest, np.asarray(samples, dtype=np.float32)))
        whole = len(data) - len(data) % BLOCK
        self.rest = data[whole:]
        for blk in data[:whole].reshape(-1, BLOCK):
            self.block(blk.astype(np.float64))
        return whole // BLOCK

    def block(self, x) -> None:
        np = self.np
        self.blocks += 1
        d = clapmod.db(float(np.mean(x * x)))
        self.block_db = d
        env = d if self.env_db is None else self.env_db
        env += (A_ATTACK if d > env else A_RELEASE) * (d - env)
        self.env_db = env
        sounding = d >= SILENT_DB
        if sounding:
            self.history.append(env)
            if len(self.history) * BLOCK_S >= WARMUP_S:
                arr = np.fromiter(self.history, dtype=np.float64)
                floor, ceil = np.percentile(arr, (FLOOR_PCT, CEIL_PCT))
                self.floor_db, self.ceil_db = float(floor), float(max(ceil, floor + MIN_SPAN_DB))
        if d < max(self.floor_db, SILENT_DB):
            self.quiet_s += BLOCK_S
        else:
            self.quiet_s = 0.0
        if sounding and d >= self.floor_db:
            mag = np.abs(np.fft.rfft((x - x.mean()) * self.hann))[self.band]
            total = float(mag.sum())
            if total > 0:
                self.centroid = float((self.freqs * mag).sum() / total)
                t = centroid_tone(self.centroid)
                self.tone = t if self.tone is None else self.tone + A_TONE * (t - self.tone)


# --------------------------------------------------------------------- capture

class AudioCapture(clapmod.Capture):
    """clap.Capture with follow-audio's sources: "" the monitor of the default
    output (passive), a "….monitor" name that sink's monitor, else the source."""

    def __init__(self, on_samples: Callable, source: str = "") -> None:
        super().__init__(on_samples, source, monitor=not source, name="follow audio")

    def command(self) -> list[str]:
        props = f'{{ application.name = "Haus.Licht" media.name = "{self.name}"'
        pw = shutil.which("pw-record")
        src = self.source
        if pw is not None:
            cmd = [pw, "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--latency", "50ms"]
            if not src:
                return cmd + ["-P", props + " stream.capture.sink = true node.passive = true }", "-"]
            if src.endswith(".monitor"):
                return cmd + ["-P", props + " stream.capture.sink = true node.passive = true }",
                              "--target", src[:-len(".monitor")], "-"]
            return cmd + ["-P", props + " }", "--target", src, "-"]
        parec = shutil.which("parec")
        if parec is None:
            raise CaptureError("neither pw-record nor parec found")
        return [parec, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", "--latency-msec=50",
                "--client-name=Haus.Licht", f"--stream-name={self.name}", f"--device={src or '@DEFAULT_MONITOR@'}"]


async def list_sources() -> list[dict]:
    """Every source, monitors included: [{name, description, monitor, default}];
    default: the default output's monitor or the default microphone."""
    try:
        sink = (await clapmod.pactl("get-default-sink")).strip()
        source = (await clapmod.pactl("get-default-source")).strip()
        items = json.loads(await clapmod.pactl("-f", "json", "list", "sources") or "[]")
    except (OSError, TimeoutError, ValueError) as exc:
        raise CaptureError(f"pactl: {exc}") from None
    out = []
    for s in items:
        name = s.get("name", "")
        if not name:
            continue
        props = s.get("properties") or {}
        monitor = name.endswith(".monitor") or props.get("device.class") == "monitor"
        out.append({"name": name, "description": s.get("description") or name, "monitor": monitor,
                    "default": name in (f"{sink}.monitor", source)})
    out.sort(key=lambda s: not s["monitor"])  # what you hear first
    return out


# --------------------------------------------------------------------- service

class AudioFollowService:
    """The capture and the analyser, running only while some bulb follows
    audio. on_block() is called after every chunk (the daemon wakes its
    workers when an update is due); on_failure(message) once when the capture
    cannot start or ends by itself: the daemon turns the mode off (no retry)."""

    def __init__(self, on_block: Callable[[], None], on_failure: Callable[[str], None]) -> None:
        self.on_block, self.on_failure = on_block, on_failure
        self.on, self.source = False, ""
        self.task: asyncio.Task | None = None
        self.state = "off"  # off | starting | listening | error
        self.error = ""
        self.analyser: Analyser | None = None
        self.last_data: float | None = None  # monotonic time of the last chunk
        self.started_at: float | None = None

    def apply(self, on: bool, source: str) -> None:
        """Start, stop or (a new source) restart; nothing when nothing changed."""
        restart = source != self.source
        self.on, self.source = on, source
        if not on:
            self.stop()
            if self.state != "error":  # the failure stays readable until the next start
                self.state = "off"
            return
        if restart or self.task is None or self.task.done():
            self.stop()
            self.state, self.error = "starting", ""
            self.task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
        self.analyser = None
        self.last_data = None

    def _fail(self, message: str) -> None:
        self.state, self.error = "error", message
        log.warning("follow audio: %s; turned off", message)
        try:
            self.on_failure(message)
        except Exception as exc:  # never let it take the loop down
            log.warning("follow audio: turning off failed: %s", exc)

    async def _run(self) -> None:
        lacking = missing()
        if lacking:
            self._fail(f"missing {', '.join(lacking)}: {clapmod.install_hint()}")
            return
        self.analyser = Analyser()
        self.state, self.started_at = "listening", time.monotonic()
        log.info("follow audio: listening to %s", self.source or "the default output")
        try:
            await AudioCapture(self._samples, self.source).run()
        except CaptureError as exc:
            self._fail(str(exc))
        except OSError as exc:  # the recorder could not be started
            self._fail(f"{type(exc).__name__}: {exc}")

    def _samples(self, samples, now: float) -> None:
        if self.analyser is None:
            return
        self.last_data = now
        self.analyser.feed(samples)
        try:
            self.on_block()
        except Exception as exc:
            log.warning("follow audio: %s", exc)

    def silent(self, now: float | None = None) -> bool:
        """Silence: the analyser says so, or no data for SILENCE_S (a passive
        monitor gets none while nothing plays)."""
        a = self.analyser
        if a is None or self.state != "listening":
            return True
        now = time.monotonic() if now is None else now
        last = self.last_data if self.last_data is not None else self.started_at or now
        return a.silent or now - last >= SILENCE_S

    def level(self) -> float | None:
        """0…1; None in silence (or not listening): hold the range's low end."""
        return None if self.silent() else self.analyser.level

    def tone(self) -> float | None:
        a = self.analyser
        return a.tone if a is not None and self.state == "listening" else None

    def describe(self) -> dict:
        a = self.analyser
        level = self.level()
        lacking = missing()
        return {"state": self.state, "error": self.error, "available": not lacking, "missing": lacking,
                "install": clapmod.install_hint() if lacking else "",
                "silent": self.silent(), "level": None if level is None else round(level, 3),
                "tone": None if self.tone() is None else round(self.tone(), 3),
                "centroid": None if a is None or a.centroid is None else round(a.centroid),
                "level_db": None if a is None else round(a.block_db, 1),
                "floor_db": None if a is None else round(a.floor_db, 1),
                "ceil_db": None if a is None else round(a.ceil_db, 1)}
