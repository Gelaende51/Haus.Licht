"""A double clap or double finger snap toggles the lights (settings.clap).

    microphone ── Capture (pw-record/parec, mono 16 kHz s16, pipe only) ── 10 ms blocks
       │
       ▼  TransientGate: fast rise out of quiet, falls back within 100 ms
    onset ─▶ YAMNet (TFLite) on the 0.975 s window around it, only then
       │      accept Clapping (58) / Finger snapping (57) above a threshold
       │      unless Speech/Music/Knock/Tap/Typing/Keyboard wins
       ▼  DoubleHitDetector: two accepted hits 150-600 ms apart, quiet
    toggle    600 ms before and 300 ms after, 1 s cooldown; a hit that the
              default sink's monitor shows at the same time came from the
              speakers and is dropped

Nothing is written to disk and nothing leaves the computer: audio lives in a
2 s ring buffer only; logs carry times and class scores. The model file is
fetched once (pinned URL, sha256 checked) into ~/.cache/hauslicht/.

numpy and ai-edge-litert are an optional extra (requirements-clap.txt); this
module imports them lazily, so the daemon runs without them and reports the
feature unavailable. Capture is a class of its own so a later mode that
follows audio can share it. Research and decisions:
dev/clap-detection-research.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import math
import os
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .common import ROOT, _

log = logging.getLogger("hauslicht")

MODES = ("off", "clap", "snap", "both")
RATE = 16000
BLOCK = 160  # 10 ms
WINDOW = 15600  # YAMNet's input: 0.975 s
PRE = 8000  # of it before the onset (0.5 s): a pair 150-475 ms apart shares one window
RING = 2 * RATE

# MediaPipe's YAMNet audio classifier (TFLite, 4.1 MB, labels inside),
# Apache-2.0; see README "Third-party". Pinned: a new file is a code change.
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite"
MODEL_SHA256 = "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf"
MODEL_NAME = "yamnet.tflite"

SPEECH, MUSIC, SNAP, CLAP = 0, 132, 57, 58
CONFUSERS = {SPEECH: "Speech", MUSIC: "Music", 353: "Knock", 354: "Tap", 378: "Typing",
             379: "Typewriter", 380: "Computer keyboard"}
TARGETS = {"clap": (CLAP,), "snap": (SNAP,), "both": (SNAP, CLAP)}
LABELS = {SNAP: "Snap", CLAP: "Clap"}

ECHO_S = 0.1  # onsets closer than this to the previous one are the same hit
PAIR_MIN_S, PAIR_MAX_S = 0.15, 0.6  # the second hit after the first
QUIET_BEFORE_S = 0.6  # no onset this long before the first hit
QUIET_AFTER_S = 0.3  # nor this long after the second (applause, a knock series)
COOLDOWN_S = 1.0  # after a double
DECAY_S = 0.1  # a hit falls back by DECAY_DB within this; longer is speech, music, a door
DECAY_DB = 12.0
MONITOR_TOL_S = 0.15  # a hit the speakers played within this is theirs
RETRY_S = 10.0  # a capture that ended is restarted after this


def install_hint() -> str:
    return f"{sys.executable} -m pip install -r {ROOT / 'requirements-clap.txt'}"


def missing() -> list[str]:
    """What the feature lacks: pip packages and capture programs."""
    out = [name for name, mod in (("numpy", "numpy"), ("ai-edge-litert", "ai_edge_litert"))
           if importlib.util.find_spec(mod) is None]
    if not (shutil.which("pw-record") or shutil.which("parec")):
        out.append("pw-record")
    return out


def check_mode(value) -> str:
    value = str(value).strip().lower()
    if value not in MODES:
        raise ValueError(f"clap must be one of {', '.join(MODES)}")
    return value


def check_sensitivity(value) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("clap_sensitivity must be a number 0-100") from None
    if not math.isfinite(v):
        raise ValueError("clap_sensitivity must be a number 0-100")
    return int(round(max(0.0, min(100.0, v))))


def check_source(value) -> str:
    """A source name as pactl lists it; "" (or "default") is the default source."""
    value = str(value or "").strip()
    if value.lower() in ("default", "@default_source@"):
        return ""
    if any(c.isspace() for c in value) or len(value) > 200:
        raise ValueError("clap_source must be a source name (hauslicht-ctl clap-sources) or default")
    return value


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "hauslicht"


def db(power: float) -> float:
    return 10.0 * math.log10(max(power, 1e-12))


# --------------------------------------------------------------------- gate

class TransientGate:
    """Per 10 ms block power: a hit rises at least `k` over the noise floor
    (and above an absolute minimum) out of quiet blocks and falls DECAY_DB
    below its peak (or to within 6 dB of the floor) within DECAY_S. Cheap: a handful of float operations a
    block. The floor follows quiet blocks only, falling fast, rising slowly."""

    def __init__(self, sensitivity: int = 50, min_rms: float | None = None) -> None:
        self.floor = 1e-7
        self.recent: deque[float] = deque(maxlen=3)
        self.cand: tuple[int, float] | None = None  # (onset block, peak power)
        self.busy = False  # a sustained sound: wait until it falls
        self.fixed_min = min_rms
        self.set_sensitivity(sensitivity)

    def set_sensitivity(self, sensitivity: int) -> None:
        s = max(0, min(100, sensitivity)) / 100.0
        self.k = 10 ** ((18.0 - 10.0 * s) / 10.0)  # 18 dB over the floor (low) … 8 dB (high)
        rms = self.fixed_min if self.fixed_min is not None else 0.03 * 10 ** (-s)  # −30 … −50 dBFS
        self.min_power = rms * rms

    def threshold(self) -> float:
        return max(self.floor * self.k, self.min_power)

    def feed(self, power: float, index: int) -> int | None:
        """One block's mean square; returns the onset block of a hit that just ended."""
        thr = self.threshold()
        hit = None
        if self.cand is not None:
            onset, peak = self.cand
            peak = max(peak, power)
            if power * 10 ** (DECAY_DB / 10.0) <= peak or power <= self.floor * 4:  # or back near the floor
                hit, self.cand = onset, None
            elif (index - onset) * BLOCK >= DECAY_S * RATE:
                self.cand, self.busy = None, True
            else:
                self.cand = (onset, peak)
        elif self.busy:
            if power < thr / 2:
                self.busy = False
        elif power >= thr:
            if not self.recent or max(self.recent) <= max(thr / 4, self.floor * 4):  # out of quiet: a fast rise
                self.cand = (index, power)
            else:
                self.busy = True
        if self.cand is None and not self.busy and power < thr:
            rate = 0.2 if power < self.floor else 0.01
            self.floor = max(1e-10, self.floor + rate * (power - self.floor))
        self.recent.append(power)
        return hit


# --------------------------------------------------------------------- detector

class Hit:
    __slots__ = ("sample", "t", "scores", "accepted", "label", "score", "mirrored", "window")

    def __init__(self, sample: int, t: float) -> None:
        self.sample, self.t = sample, t  # stream sample of the onset; its time on the caller's clock
        self.scores = None
        self.accepted: bool | None = None  # None: not classified yet
        self.label, self.score, self.mirrored = "", 0.0, False
        self.window = (sample - PRE, sample - PRE + WINDOW)


class DoubleHitDetector:
    """Feed it the microphone (float samples, −1…1) and the times the
    speakers played a transient; it returns the doubles it saw.

    classify(window: float32[WINDOW]) -> scores[521] (YAMNet's classes);
    labels: index -> name for reporting what won (optional)."""

    def __init__(self, classify: Callable, mode: str = "both", sensitivity: int = 50,
                 labels: list[str] | None = None) -> None:
        import numpy as np
        self.np = np
        self.classify, self.labels = classify, labels or []
        self.mode, self.sensitivity = mode, sensitivity
        self.gate = TransientGate(sensitivity)
        self.ring = np.zeros(RING, dtype=np.float32)
        self.n = 0  # samples seen
        self.rest = np.zeros(0, dtype=np.float32)
        self.hits: list[Hit] = []  # every onset of the last seconds, classified or not
        self.monitor: deque[float] = deque(maxlen=64)  # onset times the speakers played
        self.fired_at = -math.inf
        self.level_db = -120.0  # loudest 10 ms block of the last 100 ms
        self._levels: deque[float] = deque(maxlen=10)
        self.last: dict | None = None  # the last classified hit, for the meter
        self.classified = 0  # model runs (for the CPU figure)

    def set_options(self, mode: str, sensitivity: int) -> None:
        self.mode, self.sensitivity = mode, sensitivity
        self.gate.set_sensitivity(sensitivity)

    def theta(self) -> float:
        return 0.5 - 0.4 * max(0, min(100, self.sensitivity)) / 100.0  # 0.5 (low) … 0.1 (high)

    def judge(self, scores) -> tuple[bool, str, float]:
        targets = TARGETS.get(self.mode, TARGETS["both"])
        best = max(targets, key=lambda i: float(scores[i]))
        p = float(scores[best])
        winner = int(self.np.argmax(scores))
        confuser = max(float(scores[i]) for i in CONFUSERS)
        if p >= self.theta() and winner not in CONFUSERS and p > confuser:
            return True, LABELS[best], p
        name = CONFUSERS.get(winner) or LABELS.get(winner) or (
            self.labels[winner] if winner < len(self.labels) else str(winner))
        return False, name, float(scores[winner])

    def monitor_onset(self, t: float) -> None:
        self.monitor.append(t)

    def feed(self, samples, now: float | None = None) -> list[dict]:
        """samples: float32 −1…1; now: the time of the last sample on the
        clock the monitor times use (default: stream time, n / RATE)."""
        np = self.np
        data = np.concatenate((self.rest, np.asarray(samples, dtype=np.float32)))
        whole = len(data) - len(data) % BLOCK
        self.rest = data[whole:]
        n_end = self.n + whole
        if now is None:
            now = n_end / RATE
        if whole:
            blocks = data[:whole].reshape(-1, BLOCK)
            powers = (blocks.astype(np.float64) ** 2).mean(axis=1)
            for j, power in enumerate(powers):
                index = self.n // BLOCK + j
                self._levels.append(float(power))
                onset = self.gate.feed(float(power), index)
                if onset is not None:
                    self.onset(onset * BLOCK, now - (n_end - onset * BLOCK) / RATE)
            self.level_db = db(max(self._levels))
            pos = self.n % RING
            first = min(whole, RING - pos)
            self.ring[pos:pos + first] = data[:first]
            if whole > first:
                self.ring[:whole - first] = data[first:whole]
            self.n = n_end
        events = self.classify_ready()
        now_stream = self.n / RATE
        double = self.pair(now_stream, now)
        if double is not None:
            events.append(double)
        cutoff = self.n - 3 * RATE
        self.hits = [h for h in self.hits if h.sample >= cutoff]
        return events

    def onset(self, sample: int, t: float) -> None:
        if self.hits and (sample - self.hits[-1].sample) < ECHO_S * RATE:
            return  # the same hit's tail or echo
        self.hits.append(Hit(sample, t))

    def window(self, start: int):
        np = self.np
        idx = np.arange(start, start + WINDOW) % RING
        return self.ring[idx]

    def classify_ready(self) -> list[dict]:
        events = []
        for i, h in enumerate(self.hits):
            if h.accepted is not None or self.n < h.window[1]:
                continue
            shared = next((o for o in reversed(self.hits[:i]) if o.scores is not None
                           and o.window[0] + PRE // 2 <= h.sample <= o.window[1] - int(0.15 * RATE)), None)
            if h.window[0] < self.n - RING:
                h.accepted = False
                continue  # fell out of the buffer (should not happen)
            if shared is not None:
                h.scores = shared.scores
            else:
                h.scores = self.classify(self.window(h.window[0]))
                self.classified += 1
            h.accepted, h.label, h.score = self.judge(h.scores)
            if any(abs(m - h.t) <= MONITOR_TOL_S for m in self.monitor):
                h.mirrored, h.accepted = True, False
            self.last = {"label": h.label, "score": round(h.score, 3), "accepted": h.accepted,
                         "mirrored": h.mirrored, "at": h.t}
            log.debug("clap: hit %s %.2f%s%s", h.label, h.score, " accepted" if h.accepted else "",
                      " (speakers)" if h.mirrored else "")
            events.append({"event": "hit", **self.last})
        return events

    def pair(self, now_stream: float, now: float) -> dict | None:
        """Two consecutive accepted hits, PAIR_MIN-PAIR_MAX apart, quiet around."""
        hits = self.hits
        for i in range(len(hits) - 1):
            a, b = hits[i], hits[i + 1]
            if a.accepted is None or b.accepted is None:
                return None  # in order: wait for the classifier
            if not (a.accepted and b.accepted):
                continue
            dt = (b.sample - a.sample) / RATE
            if not PAIR_MIN_S <= dt <= PAIR_MAX_S:
                continue
            if a.sample / RATE - self.fired_at < COOLDOWN_S:
                continue
            if i > 0 and (a.sample - hits[i - 1].sample) / RATE < QUIET_BEFORE_S:
                continue
            if now_stream < b.sample / RATE + QUIET_AFTER_S:
                return None  # not decided yet
            if i + 2 < len(hits) and (hits[i + 2].sample - b.sample) / RATE <= QUIET_AFTER_S:
                continue
            self.fired_at = b.sample / RATE
            self.hits = hits[i + 2:]
            label = a.label if a.label == b.label else "Clap+Snap"
            return {"event": "double", "label": label, "score": round(min(a.score, b.score), 3),
                    "gap": round(dt, 3), "at": b.t}
        return None


# --------------------------------------------------------------------- capture

class CaptureError(Exception):
    pass


class Capture:
    """Mono 16 kHz signed 16-bit audio from one PipeWire/Pulse source, through
    a pipe (nothing on disk), handed to on_samples(float32 −1…1, monotonic
    time of the last sample) in chunks of about 50 ms.

    source: a source name ("" the default source); monitor=True records what
    the default sink plays instead, as a passive stream (it never wakes a
    suspended sink, so it gets data only while something plays). Shared by
    the clap detector and, later, a follow-audio mode."""

    def __init__(self, on_samples: Callable, source: str = "", monitor: bool = False,
                 name: str = "clap detection") -> None:
        self.on_samples, self.source, self.monitor, self.name = on_samples, source, monitor, name
        self.proc: asyncio.subprocess.Process | None = None

    def command(self) -> list[str]:
        props = f'{{ application.name = "Haus.Licht" media.name = "{self.name}"'
        pw = shutil.which("pw-record")
        if self.monitor:
            if pw is None:
                raise CaptureError("pw-record missing: speaker sounds are not filtered")
            return [pw, "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--latency", "50ms",
                    "-P", props + " stream.capture.sink = true node.passive = true }", "-"]
        if pw is not None:
            return [pw, "--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--latency", "50ms",
                    "-P", props + " }", *(["--target", self.source] if self.source else []), "-"]
        parec = shutil.which("parec")
        if parec is None:
            raise CaptureError("neither pw-record nor parec found")
        return [parec, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", "--latency-msec=50",
                "--client-name=Haus.Licht", f"--stream-name={self.name}",
                *([f"--device={self.source}"] if self.source else [])]

    async def run(self) -> None:
        """Until cancelled; CaptureError when the recorder ends by itself."""
        import numpy as np
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        rest = b""
        try:
            while True:
                chunk = await self.proc.stdout.read(4096)
                if not chunk:
                    break
                data = rest + chunk
                cut = len(data) - len(data) % 2
                rest = data[cut:]
                if cut:
                    samples = np.frombuffer(data[:cut], dtype="<i2").astype(np.float32) / 32768.0
                    self.on_samples(samples, time.monotonic())
            err = (await self.proc.stderr.read()).decode(errors="replace").strip().splitlines()
            await self.proc.wait()
            raise CaptureError(f"recorder ended (exit {self.proc.returncode}): {err[-1] if err else ''}".strip())
        finally:
            await self.close()

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()


async def list_sources() -> list[dict]:
    """Microphones (no sink monitors): [{name, description, default}], the default first."""
    async def pactl(*args) -> str:
        proc = await asyncio.create_subprocess_exec(
            "pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _err = await asyncio.wait_for(proc.communicate(), 5.0)
        return out.decode(errors="replace")
    try:
        default = (await pactl("get-default-source")).strip()
        items = json.loads(await pactl("-f", "json", "list", "sources") or "[]")
    except (OSError, TimeoutError, ValueError) as exc:
        raise CaptureError(f"pactl: {exc}") from None
    out = []
    for s in items:
        name = s.get("name", "")
        props = s.get("properties") or {}
        if not name or name.endswith(".monitor") or props.get("device.class") == "monitor":
            continue
        out.append({"name": name, "description": s.get("description") or name, "default": name == default})
    out.sort(key=lambda s: not s["default"])
    return out


# --------------------------------------------------------------------- model

class YamnetClassifier:
    """YAMNet on ai-edge-litert, one thread: ~5 ms per window here."""

    def __init__(self, path: Path) -> None:
        import numpy as np
        from ai_edge_litert.interpreter import Interpreter
        self.np = np
        self.it = Interpreter(model_path=str(path), num_threads=1)
        inp = self.it.get_input_details()[0]
        if list(inp["shape"]) != [WINDOW]:
            self.it.resize_tensor_input(inp["index"], [WINDOW])
        self.it.allocate_tensors()
        self.inp = inp["index"]
        self.out = self.it.get_output_details()[0]["index"]
        self.labels: list[str] = []
        with contextlib.suppress(zipfile.BadZipFile, KeyError, OSError):
            with zipfile.ZipFile(path) as z:  # the TFLite file carries its label list
                self.labels = z.read("yamnet_label_list.txt").decode().splitlines()

    def __call__(self, window):
        self.it.set_tensor(self.inp, self.np.ascontiguousarray(window, dtype=self.np.float32))
        self.it.invoke()
        return self.it.get_tensor(self.out)[0].copy()


def model_path() -> Path:
    return cache_dir() / MODEL_NAME


def model_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == MODEL_SHA256


def fetch_model(path: Path | None = None, url: str = MODEL_URL) -> Path:
    """Download once to the cache (blocking: run in a thread); sha256 checked."""
    path = path or model_path()
    if model_ok(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=30) as resp, tmp.open("wb") as f:
        while chunk := resp.read(1 << 16):
            h.update(chunk)
            f.write(chunk)
            if f.tell() > 64 << 20:
                raise CaptureError("model download too large")
    if h.hexdigest() != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise CaptureError(f"model checksum mismatch ({h.hexdigest()[:12]}…), not used")
    tmp.replace(path)
    log.info("clap: model saved to %s", path)
    return path


# --------------------------------------------------------------------- service

class ClapService:
    """The daemon's side: runs the detector while settings.clap is not off.
    on_double(event) is called in the event loop for each double."""

    def __init__(self, on_double: Callable[[dict], None]) -> None:
        self.on_double = on_double
        self.mode, self.source, self.sensitivity = "off", "", 50
        self.task: asyncio.Task | None = None
        self.state = "off"  # off | unavailable | downloading | starting | listening | error
        self.error = ""
        self.detector: DoubleHitDetector | None = None
        self.last_double: dict | None = None
        self.monitor_gate = TransientGate(100, min_rms=0.003)
        self._mon_n = 0
        self.started_at: float | None = None

    def apply(self, mode: str, source: str, sensitivity: int) -> None:
        """Settings changed (or the daemon started): restart only what must."""
        restart = (mode != "off") != (self.mode != "off") or source != self.source
        self.mode, self.source, self.sensitivity = mode, source, sensitivity
        if self.detector is not None:
            self.detector.set_options(mode, sensitivity)
        if mode == "off":
            self.stop()
            self.state, self.error = "off", ""
            return
        if restart or self.task is None or self.task.done():
            self.stop()
            self.state, self.error = "starting", ""
            self.task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.detector = None

    async def _run(self) -> None:
        lacking = missing()
        if lacking:
            self.state, self.error = "unavailable", ", ".join(lacking)
            log.warning("clap: unavailable, missing %s: %s", self.error, install_hint())
            return
        try:
            path = model_path()
            if not await asyncio.to_thread(model_ok, path):
                self.state = "downloading"
                path = await asyncio.to_thread(fetch_model, path)
            self.state = "starting"
            clf = await asyncio.to_thread(YamnetClassifier, path)
        except Exception as exc:  # no network, a bad file, a broken runtime: say so, keep the daemon
            self.state, self.error = "error", f"{type(exc).__name__}: {exc}"
            log.warning("clap: %s", self.error)
            return
        while True:
            self.detector = DoubleHitDetector(clf, self.mode, self.sensitivity, clf.labels)
            mic = Capture(self._mic, self.source)
            mon = Capture(self._mon, monitor=True, name="clap detection (speaker check)")
            self.state, self.error, self.started_at = "listening", "", time.monotonic()
            log.info("clap: listening (%s, source %s, sensitivity %d)", self.mode, self.source or "default",
                     self.sensitivity)
            monitor = asyncio.get_running_loop().create_task(self._monitor(mon))
            try:
                await mic.run()
            except CaptureError as exc:
                self.state, self.error = "error", str(exc)
                log.warning("clap: %s; again in %.0f s", exc, RETRY_S)
            finally:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitor
            await asyncio.sleep(RETRY_S)

    async def _monitor(self, mon: Capture) -> None:
        try:
            await mon.run()
        except CaptureError as exc:
            log.info("clap: speaker check off: %s", exc)

    def _mic(self, samples, now: float) -> None:
        det = self.detector
        if det is None:
            return
        for ev in det.feed(samples, now):
            if ev["event"] == "double":
                self.last_double = {**ev, "time": time.time()}
                log.info("clap: double %s (%.2f, %.0f ms apart)", ev["label"], ev["score"], ev["gap"] * 1000)
                try:
                    self.on_double(ev)
                except Exception as exc:  # never let the toggle stop the capture
                    log.warning("clap: toggle failed: %s", exc)

    def _mon(self, samples, now: float) -> None:
        import numpy as np
        whole = len(samples) - len(samples) % BLOCK
        if not whole or self.detector is None:
            return
        powers = (samples[:whole].astype(np.float64).reshape(-1, BLOCK) ** 2).mean(axis=1)
        n_end = self._mon_n + whole
        for j, power in enumerate(powers):
            index = self._mon_n // BLOCK + j
            onset = self.monitor_gate.feed(float(power), index)
            if onset is not None:
                self.detector.monitor_onset(now - (n_end - onset * BLOCK) / RATE)
        self._mon_n = n_end

    def describe(self) -> dict:
        lacking = missing()
        state = "unavailable" if lacking and self.mode != "off" else self.state
        return {"state": state, "available": not lacking, "missing": lacking,
                "install": install_hint() if lacking else "", "error": self.error}

    def meter(self) -> dict:
        det = self.detector
        now = time.monotonic()
        listening = det is not None and self.state == "listening"
        level_db = det.level_db if listening else None
        last = None
        if det is not None and det.last is not None:
            last = {k: v for k, v in det.last.items() if k != "at"}
            last["age"] = round(max(0.0, now - det.last["at"]), 1)
        double = None
        if self.last_double is not None:
            double = {"label": self.last_double["label"], "score": self.last_double["score"],
                      "age": round(max(0.0, time.time() - self.last_double["time"]), 1)}
        return {**self.describe(), "level_db": None if level_db is None else round(level_db, 1),
                "level": None if level_db is None else round(max(0.0, min(1.0, (level_db + 60.0) / 60.0)), 3),
                "last": last, "double": double, "model_runs": det.classified if det is not None else 0}
