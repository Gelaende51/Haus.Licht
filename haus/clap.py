"""A double clap or double finger snap toggles the lights (settings.widget_clap:
per widget, its selection).

    microphone ── Capture (pw-record/parec, mono 16 kHz s16, pipe only) ── 10 ms blocks
       │          DcBlocker: the microphone's DC offset off
       ▼  TransientGate: fast rise over the room's noise floor, out of quiet
       │  (or a sharp jump on the tail of the hit before), falls back within
       │  100 ms; reports the hit's shape (dB over floor, rise, decay)
    onset ─▶ YAMNet (TFLite) on the 0.975 s window around it, only then:
       │      the clap family's summed score (CLAP_CORE, SNAP_CORE, IMPULSE)
       │      against the bar, lower for a clap-like shape, unless
       │      Speech/Music/Typing scores more (VETOES)
       ▼  DoubleHitDetector: one accepted hit and a partner (a lower bar:
    toggle    only not clearly speech, music or typing) 120-800 ms apart,
              quiet 600 ms before and 300 ms after, 1 s cooldown; a hit that
              the default sink's monitor shows at the same time came from
              the speakers and is dropped. The last 30 hits stay in memory
              for `ctl clap-hits` (their numbers, never audio).

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
# The clap family: YAMNet classes a hand clap or a finger snap scores on, with
# weights (dev/clap-detection-research.md, "Clap family"). A hit is judged by
# their sum, not by the top-1 class: a clap that is not a textbook one spreads
# its score over several of them, and the room classes (Silence, Inside, small
# room) win the top spot.
CLAP_CORE = {CLAP: 1.0, 56: 1.0, 62: 1.0, 461: 1.0}  # Clapping, Hands, Applause, Slap/smack
SNAP_CORE = {SNAP: 1.0}  # Finger snapping
IMPULSE = {425: 0.5, 466: 0.5, 434: 0.5, 428: 0.5, 432: 0.5, 168: 0.5,  # Cap gun, Whip, Crack, Burst/pop,
           353: 0.25, 354: 0.25}  # Chop, Wood block; Knock, Tap (a door, a desk: less); not Clicking (mouse)
VETOES = (0, 1, 2, 3, MUSIC, 378, 379, 380)  # Speech (child, conversation, narration), Music, Typing, keyboards
NAMES = {SPEECH: "Speech", 1: "Child speech", 2: "Conversation", 3: "Narration", MUSIC: "Music",
         378: "Typing", 379: "Typewriter", 380: "Computer keyboard", CLAP: "Clapping", 56: "Hands",
         62: "Applause", 461: "Slap, smack", SNAP: "Finger snapping", 425: "Cap gun", 466: "Whip",
         434: "Crack", 428: "Burst, pop", 432: "Chop", 168: "Wood block", 485: "Clicking", 353: "Knock",
         354: "Tap", 494: "Silence", 500: "Inside, small room"}  # when the model file has no label list
KIND_MIN = 0.05  # a hit is a Clap or a Snap by its core classes from this score on; below, its partner's
SHAPE_FACTOR = 0.6  # the bar for a clap-like shape (fast rise, short decay)
PARTNER_FACTOR = 0.25  # the partner's bar (0 for a clap-like one: only a veto stops it)
CLAP_RISE_DB, CLAP_DECAY_MS = 15.0, 50  # clap-like: rises this much in a block, falls back within this
HISTORY = 30  # hits kept for ctl clap-hits

ECHO_S = 0.1  # onsets closer than this to the previous one are the same hit
PAIR_MIN_S, PAIR_MAX_S = 0.12, 0.8  # the second hit after the first
QUIET_BEFORE_S = 0.6  # no onset this long before the first hit
QUIET_AFTER_S = 0.3  # nor this long after the second (applause, a knock series)
COOLDOWN_S = 1.0  # after a double
DECAY_S = 0.1  # a hit falls back by DECAY_DB within this; longer is speech, music, a door
DECAY_DB = 12.0
JUMP_DB = 4.0  # a second hit on the first one's tail rises this much over the last 30 ms
FLOOR_CREEP_DB = 0.05  # the noise floor rises at most this per block through loud ones (5 dB/s)
DC_S = 0.5  # the DC offset is followed with this time constant
MONITOR_TOL_S = 0.15  # a hit the speakers played within this is theirs
MUTE_S = 3.0  # the source's mute switch is read this often while listening
RETRY_S = 10.0  # a capture that ended is restarted after this


def union_mode(modes) -> str:
    """What the detector listens for, given every widget's mode."""
    on = {m for m in modes if m in MODES[1:]}
    if not on:
        return "off"
    if "both" in on or on == {"clap", "snap"}:
        return "both"
    return on.pop()


def mode_matches(mode: str, label: str) -> bool:
    """A widget in `mode` answers a double of `label` (Clap, Snap, Clap+Snap)."""
    if mode == "both":
        return True
    return (mode, label) in (("clap", "Clap"), ("snap", "Snap"))


def install_hint() -> str:
    return f"{sys.executable} -m pip install -r {ROOT / 'requirements-clap.txt'}"


def missing() -> list[str]:
    """What the feature lacks: pip packages and capture programs."""
    out = [name for name, mod in (("numpy", "numpy"), ("ai-edge-litert", "ai_edge_litert"))
           if importlib.util.find_spec(mod) is None]
    if not (shutil.which("pw-record") or shutil.which("parec")):
        out.append("pw-record")
    return out


def bars(sensitivity: int) -> dict:
    """What a hit's family score must reach, by sensitivity 0 (low) … 100 (high).
    first: an accepted hit (0.5 … 0.1); ×SHAPE_FACTOR when clap-like.
    partner: the other hit of a double (×PARTNER_FACTOR; 0 when clap-like).
    veto: a partner is refused when Speech/Music/Typing scores at least this
    and more than its family (0.15 … 0.35: clearer at high sensitivity)."""
    s = max(0, min(100, sensitivity)) / 100.0
    first = 0.5 - 0.4 * s
    return {"first": first, "partner": first * PARTNER_FACTOR, "veto": 0.15 + 0.2 * s}


def clap_like(shape: dict | None) -> bool:
    return bool(shape) and shape["rise_db"] >= CLAP_RISE_DB and shape["decay_ms"] <= CLAP_DECAY_MS


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

class DcBlocker:
    """Removes the microphone's DC offset, block by block. Some built-in
    microphones deliver one: a laptop's read a steady +0.14 (−17 dBFS of DC
    under −61 dBFS of room noise), which made every block "loud" and hid
    every clap. The offset is followed slowly (DC_S), so a hit barely moves it."""

    def __init__(self) -> None:
        self.dc: float | None = None

    def __call__(self, blocks):
        """blocks: float64 [n, BLOCK]; returns them with the offset taken off (in place)."""
        rate = BLOCK / (DC_S * RATE)
        for blk in blocks:
            m = float(blk.mean())
            if self.dc is None:
                self.dc = m
            blk -= self.dc
            self.dc += rate * (m - self.dc)
        return blocks


class TransientGate:
    """Per 10 ms block power (offset removed): a hit rises at least `k` dB
    over the noise floor (and above an absolute minimum) within one block,
    out of a quiet stretch, and falls DECAY_DB below its peak (or back near
    the floor) within DECAY_S. Cheap: a handful of float operations a block.

    Within PAIR_MAX_S after a hit, its tail does not count as "not quiet": a
    block that jumps JUMP_DB over the last 30 ms is a hit too (it falls back
    once it is near where it started). Without this a softer second clap on
    the reverberating first one was held "busy" and never reached the model.
    `shape` describes the last hit: dB over the floor, rise, decay in ms.

    The floor (in dB) is a running estimate of the room: it starts at the first
    block, falls fast to anything quieter, follows quiet blocks within a few
    hundred ms and creeps up through louder ones (FLOOR_CREEP_DB a block), so a
    fan, a hum or a noisy microphone becomes the floor within seconds and no
    sound, however long, can hold the gate shut."""

    def __init__(self, sensitivity: int = 50, min_rms: float | None = None) -> None:
        self.floor_db: float | None = None
        self.recent: deque[float] = deque(maxlen=3)  # the previous blocks' dB
        self.cand: tuple[int, float, float, bool] | None = None  # (onset block, peak dB, base dB, jumped)
        self.busy = False  # a sustained sound: wait until it falls
        self.tail_until = -1  # the last block of the previous hit's tail
        self.shape: dict = {}
        self.fixed_min = min_rms
        self.set_sensitivity(sensitivity)

    def set_sensitivity(self, sensitivity: int) -> None:
        s = max(0, min(100, sensitivity)) / 100.0
        self.k_db = 18.0 - 10.0 * s  # over the floor: 18 dB (low) … 8 dB (high)
        rms = self.fixed_min if self.fixed_min is not None else 0.03 * 10 ** (-s)  # −30 … −50 dBFS
        self.min_db = db(rms * rms)

    def threshold_db(self) -> float:
        return max((self.floor_db if self.floor_db is not None else -120.0) + self.k_db, self.min_db)

    def feed(self, power: float, index: int) -> int | None:
        """One block's mean square; returns the onset block of a hit that just ended."""
        p = db(power)
        if self.floor_db is None:
            self.floor_db = p
        floor = self.floor_db
        thr = self.threshold_db()
        hit = None
        base = max(self.recent) if self.recent else floor
        jump = index <= self.tail_until and p >= thr and p >= base + JUMP_DB  # on the last hit's tail
        if self.cand is not None:
            onset, peak, cbase, jumped = self.cand
            peak = max(peak, p)
            if p <= peak - DECAY_DB or p <= floor + 6.0 or (jumped and p <= cbase + 3.0):  # fell back: a short hit
                hit, self.cand = onset, None
                self.shape = {"over_db": round(peak - floor, 1), "rise_db": round(peak - cbase, 1),
                              "decay_ms": (index - onset) * BLOCK * 1000 // RATE}
                self.tail_until = index + int(PAIR_MAX_S * RATE) // BLOCK
            elif (index - onset) * BLOCK >= DECAY_S * RATE:
                self.cand, self.busy = None, True  # rings on: speech, music, a door
            else:
                self.cand = (onset, peak, cbase, jumped)
        elif self.busy:
            if jump:
                self.cand, self.busy = (index, p, base, True), False
            elif p < thr - 3.0:
                self.busy = False
        elif p >= thr:
            if not self.recent or base <= max(thr - 6.0, floor + 6.0):  # out of quiet: a fast rise
                self.cand = (index, p, base, False)
            elif jump:
                self.cand = (index, p, base, True)
            else:
                self.busy = True
        if p < floor:
            self.floor_db = floor + 0.3 * (p - floor)
        elif self.cand is None and not self.busy and p < thr:
            self.floor_db = floor + 0.05 * (p - floor)
        else:
            self.floor_db = floor + min(FLOOR_CREEP_DB, 0.05 * (p - floor))
        self.recent.append(p)
        return hit


# --------------------------------------------------------------------- detector

class Hit:
    __slots__ = ("sample", "t", "scores", "accepted", "partner", "label", "score", "mirrored", "window",
                 "shape", "family", "kind", "record")

    def __init__(self, sample: int, t: float, shape: dict | None = None) -> None:
        self.sample, self.t = sample, t  # stream sample of the onset; its time on the caller's clock
        self.scores = None
        self.accepted: bool | None = None  # None: not classified yet; True: passed the first bar
        self.partner = False  # good enough as the other hit of a double
        self.label, self.score, self.mirrored = "", 0.0, False
        self.window = (sample - PRE, sample - PRE + WINDOW)
        self.shape = shape or {}
        self.family, self.kind = 0.0, ""  # kind: Clap, Snap or "" (the partner's)
        self.record: dict | None = None  # its entry in the detector's history


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
        self.dc = DcBlocker()
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
        self.history: deque[dict] = deque(maxlen=HISTORY)  # the last hits' numbers (ctl clap-hits)

    def set_options(self, mode: str, sensitivity: int) -> None:
        self.mode, self.sensitivity = mode, sensitivity
        self.gate.set_sensitivity(sensitivity)

    def theta(self) -> float:
        return bars(self.sensitivity)["first"]

    def name(self, i: int) -> str:
        return self.labels[i] if i < len(self.labels) else NAMES.get(i, str(i))

    def judge(self, scores, shape: dict | None = None) -> dict:
        """One hit's verdict from the model's scores and the gate's shape:
        family (summed, weighted clap-family score), kind (Clap, Snap, ""),
        accepted (the first bar), partner (the lower bar), why (not accepted),
        top (the 3 best classes), label and score (for the meter and the log)."""
        f = lambda table: sum(w * float(scores[i]) for i, w in table.items())  # noqa: E731
        clap, snap = f(CLAP_CORE), f(SNAP_CORE)
        family = clap + snap + f(IMPULSE)
        kind = ("Snap" if snap > clap else "Clap") if max(clap, snap) >= KIND_MIN else ""
        vi = max(VETOES, key=lambda i: float(scores[i]))
        veto = float(scores[vi])
        b = bars(self.sensitivity)
        like = clap_like(shape)
        first = b["first"] * (SHAPE_FACTOR if like else 1.0)
        accepted = family >= first and family > veto
        clearly = veto >= b["veto"] and veto > family
        partner = accepted or (family >= (0.0 if like else b["partner"]) and not clearly)
        if accepted:
            why = ""
        elif veto >= family and veto > 0.0:
            why = f"{self.name(vi)} {veto:.2f} beats {family:.2f}"
        else:
            why = f"family {family:.2f} < {first:.2f}"
        top = [int(i) for i in self.np.argsort(scores)[::-1][:3]]
        if partner:
            label, score = kind or "Clap", family
        else:
            label, score = self.name(top[0]), float(scores[top[0]])
        return {"family": family, "kind": kind, "accepted": accepted, "partner": partner, "why": why,
                "top": [[self.name(i), round(float(scores[i]), 3)] for i in top], "label": label,
                "score": score, "like": like}

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
            blocks = self.dc(data[:whole].astype(np.float64).reshape(-1, BLOCK))
            powers = (blocks ** 2).mean(axis=1)
            for j, power in enumerate(powers):
                index = self.n // BLOCK + j
                self._levels.append(float(power))
                onset = self.gate.feed(float(power), index)
                if onset is not None:
                    self.onset(onset * BLOCK, now - (n_end - onset * BLOCK) / RATE, dict(self.gate.shape))
            self.level_db = db(max(self._levels))
            clean = blocks.reshape(-1).astype(np.float32)  # the model hears it without the offset too
            pos = self.n % RING
            first = min(whole, RING - pos)
            self.ring[pos:pos + first] = clean[:first]
            if whole > first:
                self.ring[:whole - first] = clean[first:whole]
            self.n = n_end
        events = self.classify_ready()
        now_stream = self.n / RATE
        double = self.pair(now_stream, now)
        if double is not None:
            events.append(double)
        cutoff = self.n - 3 * RATE
        self.hits = [h for h in self.hits if h.sample >= cutoff]
        return events

    def onset(self, sample: int, t: float, shape: dict | None = None) -> None:
        if self.hits and (sample - self.hits[-1].sample) < ECHO_S * RATE:
            return  # the same hit's tail or echo
        self.hits.append(Hit(sample, t, shape))

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
            v = self.judge(h.scores, h.shape)
            h.accepted, h.partner, h.label, h.score = v["accepted"], v["partner"], v["label"], v["score"]
            h.family, h.kind = v["family"], v["kind"]
            why = v["why"]
            if any(abs(m - h.t) <= MONITOR_TOL_S for m in self.monitor):
                h.mirrored, h.accepted, h.partner, why = True, False, False, "speakers"
            self.last = {"label": h.label, "score": round(h.score, 3), "accepted": h.accepted,
                         "mirrored": h.mirrored, "at": h.t}
            sh = h.shape
            h.record = {"at": h.t, "over_db": sh.get("over_db"), "rise_db": sh.get("rise_db"),
                        "decay_ms": sh.get("decay_ms"), "clap_like": v["like"], "family": round(h.family, 3),
                        "kind": h.kind, "top": v["top"], "accepted": h.accepted, "partner": h.partner,
                        "why": why, "paired": False}
            self.history.append(h.record)
            log.debug("clap: hit %s %.2f%s | +%s dB, rise %s dB, %s ms%s, family %.2f, top %s%s",
                      h.label, h.score, " accepted" if h.accepted else " partner" if h.partner else "",
                      sh.get("over_db"), sh.get("rise_db"), sh.get("decay_ms"), " (clap-like)" if v["like"] else "",
                      h.family, ", ".join(f"{n} {p:.2f}" for n, p in v["top"]), f" | {why}" if why else "")
            events.append({"event": "hit", **self.last})
        return events

    def pair(self, now_stream: float, now: float) -> dict | None:
        """Two consecutive hits, one accepted and the other at least a
        partner (they may differ in loudness and kind), PAIR_MIN-PAIR_MAX
        apart, quiet around."""
        hits = self.hits
        for i in range(len(hits) - 1):
            a, b = hits[i], hits[i + 1]
            if a.accepted is None or b.accepted is None:
                return None  # in order: wait for the classifier
            if not ((a.accepted and b.partner) or (b.accepted and a.partner)):
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
            label = pair_label(a.kind, b.kind)
            if self.mode in ("clap", "snap") and not mode_matches(self.mode, label):
                continue  # nobody listens for this kind
            self.fired_at = b.sample / RATE
            self.hits = hits[i + 2:]
            for h in (a, b):
                if h.record is not None:
                    h.record["paired"] = True
            return {"event": "double", "label": label, "score": round(max(a.family, b.family), 3),
                    "gap": round(dt, 3), "at": b.t}
        return None


def pair_label(ka: str, kb: str) -> str:
    """Clap, Snap or Clap+Snap; a hit of no clear kind takes its partner's,
    and two such are a Clap (impulsive sounds lean that way)."""
    ka, kb = ka or kb, kb or ka
    if not ka:
        return "Clap"
    return ka if ka == kb else "Clap+Snap"


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
    the clap detector and follow-audio (haus/audiofollow.py, which only
    replaces command())."""

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


async def pactl(*args) -> str:
    """pactl's stdout (5 s at most); OSError / TimeoutError as they come.
    Shared with haus/audiofollow.py's source list."""
    proc = await asyncio.create_subprocess_exec(
        "pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _err = await asyncio.wait_for(proc.communicate(), 5.0)
    return out.decode(errors="replace")


async def list_sources() -> list[dict]:
    """Microphones (no sink monitors): [{name, description, default}], the default first."""
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
        out.append({"name": name, "description": s.get("description") or name, "default": name == default,
                    "muted": s.get("mute") is True})
    out.sort(key=lambda s: not s["default"])
    return out


async def source_muted(source: str = "") -> bool | None:
    """The source's mute switch ("" the default source); None when pactl
    cannot tell (no pactl, no such source)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "get-source-mute", source or "@DEFAULT_SOURCE@", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"})
        out, _err = await asyncio.wait_for(proc.communicate(), 5.0)
    except (OSError, TimeoutError):
        return None
    word = out.decode(errors="replace").strip().rpartition(":")[2].strip().lower()
    return True if word == "yes" else False if word == "no" else None


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
    """The daemon's side: runs the detector while any widget's mode is not off
    (mode: union_mode of them).
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
        self.monitor_dc = DcBlocker()
        self._mon_n = 0
        self.started_at: float | None = None
        self.muted: bool | None = None  # the source's mute switch; None: unknown or not listening
        self.mute_task: asyncio.Task | None = None

    def apply(self, mode: str, source: str, sensitivity: int) -> None:
        """Settings changed (or the daemon started): restart only what must.
        mode: what any widget listens for (union_mode), off when none does."""
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
            loop = asyncio.get_running_loop()
            self.task = loop.create_task(self._run())
            self.mute_task = loop.create_task(self._watch_mute())

    def stop(self) -> None:
        for task in (self.task, self.mute_task):
            if task is not None:
                task.cancel()
        self.task = self.mute_task = None
        self.detector = None
        self.muted = None

    async def _watch_mute(self) -> None:
        """The chosen source's mute switch, every MUTE_S: a muted microphone
        hears nothing, and the widget says so."""
        while True:
            self.muted = await source_muted(self.source)
            await asyncio.sleep(MUTE_S)

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
        powers = (self.monitor_dc(samples[:whole].astype(np.float64).reshape(-1, BLOCK)) ** 2).mean(axis=1)
        n_end = self._mon_n + whole
        for j, power in enumerate(powers):
            index = self._mon_n // BLOCK + j
            onset = self.monitor_gate.feed(float(power), index)
            if onset is not None:
                self.detector.monitor_onset(now - (n_end - onset * BLOCK) / RATE)
        self._mon_n = n_end

    def hits(self) -> list[dict]:
        """The last HISTORY hits, oldest first (ctl clap-hits): when, the gate's
        numbers, the family score, the top 3 classes, the verdict. Memory only."""
        det = self.detector
        if det is None:
            return []
        mono, wall = time.monotonic(), time.time()
        out = []
        for r in det.history:
            at = wall - (mono - r["at"])
            out.append({"time": time.strftime("%H:%M:%S", time.localtime(at)) + f".{int(at * 1000) % 1000:03d}",
                        "age": round(max(0.0, mono - r["at"]), 1), **{k: v for k, v in r.items() if k != "at"}})
        return out

    def describe(self) -> dict:
        lacking = missing()
        state = "unavailable" if lacking and self.mode != "off" else self.state
        return {"state": state, "available": not lacking, "missing": lacking,
                "install": install_hint() if lacking else "", "error": self.error, "muted": self.muted}

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
        floor_db = det.gate.floor_db if listening else None
        return {**self.describe(), "level_db": None if level_db is None else round(level_db, 1),
                "level": None if level_db is None else round(max(0.0, min(1.0, (level_db + 60.0) / 60.0)), 3),
                "floor_db": None if floor_db is None else round(floor_db, 1),
                "last": last, "double": double, "model_runs": det.classified if det is not None else 0}
