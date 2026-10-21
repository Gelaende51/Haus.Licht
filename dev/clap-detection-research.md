# Double clap / double snap detection: research (2026-10-17)

Goal: a double clap or double finger snap toggles the lights. Always-on, 16 kHz
mono, on the KDE desktop (PipeWire with pulse compatibility), inside the Python
3.14 daemon. Existing software preferred.

## Candidates

| Candidate | License | Last release | Py 3.14 | Deps / size | CPU always-on | Claps/snaps vs speech, music, knocks, typing | Double pattern |
|---|---|---|---|---|---|---|---|
| **YAMNet** (AudioSet classifier, classes 57 *Finger snapping*, 58 *Clapping*, 62 Applause; also 353 Knock, 354 Tap, 378 Typing, 380 Computer keyboard) via **ai-edge-litert** or **onnxruntime** | model Apache-2.0 (tensorflow/models); litert Apache-2.0, onnxruntime MIT | litert 2.3.0 (2026-10-06), onnxruntime 1.31.0 (2026-10-09) | cp314 wheels, downloaded OK | numpy (17 MB wheel) + litert (22 MB) or onnxruntime (24 MB) + model ~4 MB (int8) / 14 MB (float) | MobileNet-v1, 3.7 M params, ~0.1 GMAC per 0.975 s window; run only when a transient gate fires, so near zero when quiet | Yes, that is what it is trained for, and it has explicit classes for the confusers (knock, tap, typing, keyboard) to compare against | Must be added (timing logic, ~20 lines) |
| **clap-detector** (TzurSoffer/clapDetection) | MIT | 2.0.7 (2025-09-27), repo pushed 2026-04 | pure-Python wheel, downloaded OK | numpy + scipy (35 MB) + **PyAudio**, imported at module top; PyAudio 0.2.14 has no cp314 wheel (needs portaudio19 headers and a compiler) | negligible (bandpass + peak threshold) | No: bandpass 100-4000 Hz plus dynamic volume threshold; any loud transient (knock, door, keyboard bang, music beat) counts; snaps are quieter than claps and likely missed at the default bias | Built in (single/double/n-clap patterns, max interval, reset time) |
| **aubio** onset detection | GPL-3.0 (compatible with AGPL-3.0: AGPL-3 §13 / GPL-3 §13 allow combining) | 0.4.9 (2019) on PyPI; upstream repo active 2026-04 but no release | sdist only, must compile; distro package python3-aubio exists | C lib + numpy, small | very low | No: onset = "something started"; needs own feature rules (crest factor, spectral centroid, decay time) | Must be added |
| **librosa** `onset_detect` | ISC | 1.0.0 (2026-08) | pure wheel, but pulls numba, scikit-learn, scipy, soundfile, pooch (~150 MB+) | moderate; designed for offline analysis, not streaming | No (same as aubio) | Must be added |
| **pi-clap** | MIT | 1.4.2 (2020) | pins PyAudio 0.2.11 and gpiozero (Raspberry Pi) | | low | No (amplitude threshold) | Built in, GPIO-oriented |
| **openWakeWord** | Apache-2.0 code, models CC BY-NC-SA | 0.6.0 (2024-02) | pure wheel; depends on tflite-runtime (no wheel above cp311) on Linux | | low | Speech wake words only; no clap/snap model; training a custom one needs synthetic speech | n/a |
| **Picovoice Porcupine** | proprietary SDK, AccessKey and online activation | 4.0.3 (2026-06) | pure wheel | | low | Speech wake words only, no non-speech sounds | n/a; also excluded on license/privacy grounds |
| **PANNs** (panns-inference) | MIT | 0.1.1 (2023) | needs PyTorch (~700 MB+) | heavy CNN14 | Yes (AudioSet classes) | Must be added. Same classes as YAMNet at 50x the install |
| **webrtcvad** | BSD | | | tiny | Speech/no-speech only. Useful at most as an extra "is this speech" veto, not needed with YAMNet | n/a |
| GStreamer / PipeWire plugins | | | | | No clap/onset element found in either; `level`/`spectrum` would only give energies (same quality as clap-detector) | n/a |

## Recommendation

**YAMNet (int8 TFLite, ~4 MB) behind a cheap transient gate, run with
ai-edge-litert (or onnxruntime with the ONNX export), plus our own double-hit
timing.** Reasons:

- It is the only existing, permissively licensed, maintained option that
  actually *recognises* clapping and finger snapping as sound classes instead
  of "a loud short sound". The brief's hard cases (speech, music, door knocks,
  keyboard typing) are all AudioSet classes too, so a hit can require
  `max(snap, clap)` to beat `max(knock, tap, typing, keyboard, music)`.
- Both runtimes ship cp314 wheels today (verified by `pip download` into this
  venv's interpreter); no compiler, no PortAudio, no PyTorch.
- CPU: an energy/onset gate in numpy runs on every 10 ms block; the model runs
  only on the 0.975 s window around a transient, i.e. a few times a minute in
  a normal room.
- Cost: numpy + runtime is ~40 MB of wheels in a venv that currently has no
  numpy at all; that is the main price. Mark it an optional extra
  (`pip install .[clap]`) so the BLE core stays lean.
- Risk: YAMNet is trained on 0.96 s clips with clip-level labels; two claps
  inside one window look like one "Clapping" event, and a lone soft snap may
  score low. Thresholds need tuning on recordings from the actual room. The
  ai-edge-litert cp314 wheel is new (Nix marks its 3.14 build broken); keep
  onnxruntime as the drop-in alternative.

**Runner-up: clap-detector** (MIT, has double-clap patterns built in). Use
only its algorithm idea, not the package: it hard-imports PyAudio (no cp314
wheel) and is amplitude-only, so knocks and typing will trigger it. Its
detection logic is small enough to reimplement as the transient gate in front
of YAMNet.

aubio is the fallback if a no-ML path is wanted: GPL-3 is fine for an AGPL-3
project, but there has been no PyPI release since 2019 (source build only).

## Integration sketch

```
pw-record --rate 16000 --channels 1 --format s16 --raw -     (or: parec --rate=16000 --channels=1 --format=s16le --raw)
   │ 10 ms blocks (160 samples) from a thread / asyncio subprocess pipe
   ▼
transient gate: block energy > k × running noise floor (k≈8-10),
                rise within ≤ 5 ms, decay to floor within ≤ 80 ms
   │ candidate onset time t
   ▼
YAMNet on 0.975 s centred on t  →  p_snap, p_clap, p_confusers
   │ accept if max(p_snap, p_clap) ≥ θ (≈0.3, tune) and beats confusers
   ▼
double-hit: second accepted onset 150-600 ms after the first, no third
            onset within 600 ms after the second (a run of applause or
            a knock series is not a double), 2 s refractory after firing
   ▼
toggle all bulbs (same code path as the flick toggle / ctl "toggle")
```

- Two hits can share one YAMNet window; classify once and reuse the scores
  for both onsets when they fall into the same window.
- **Speaker rejection:** record the default sink's monitor in parallel
  (`parec -d @DEFAULT_MONITOR@` or `pw-record --target <sink>.monitor`) with
  the same gate. If the monitor shows a transient within ±30 ms of a mic
  onset (or its level is above a floor at all), drop the mic onset. Cheaper
  and more robust than echo cancellation; PipeWire's `module-echo-cancel`
  (WebRTC AEC) is the heavier alternative if music with claps still leaks.
- Service: runs in the user session (needs the user's PipeWire socket);
  default **off**, enabled in `hauslicht.json`; pause while the mic is
  muted (`pactl get-source-mute @DEFAULT_SOURCE@`).

## Privacy

- Audio is processed in memory in 10 ms blocks and the 1 s ring buffer only;
  nothing is written to disk or sent anywhere, no network at runtime (the
  model ships as a file, no download on start).
- Logs carry only event times and class scores, never audio. Optional
  recording for tuning must be an explicit, separate debug command.
- KDE shows the microphone-in-use indicator while the capture runs; document
  that this is expected and how to switch the feature off.
- Opt-in, not default.

Sources: PyPI JSON metadata (aubio, clap-detector, pi-clap, ai-edge-litert,
onnxruntime, librosa, openwakeword, pvporcupine, panns-inference,
tflite-runtime, pyaudio); github.com/TzurSoffer/clapDetection (source, MIT);
tensorflow/models research/audioset/yamnet/yamnet_class_map.csv;
huggingface.co/qualcomm/YamNet (size, params, formats);
aubio.org/manual/latest/python_module.html (source-only package).

## Tolerance (2026-10-21)

User report: "not sensitive enough … dissimilar claps seem to get ignored the
most". Sensitivity was already 100 (bar 0.1 on Clapping or Finger snapping
alone, the top-1 class deciding).

**What the journal showed** (10:50:55–10:51:36, the user's test):

- Every double fired. The "double Snap" lines looked unanswered only because
  the check grepped for `clap`, and their `power on/off: … (double snap
  (widget 1))` lines do not contain it. What the user saw instead: the mesh
  bulb applied some of them late (`screen on -> …` is logged when the
  backend has sent it): 10:51:01.9 off applied 10:51:03 after a link drop
  ("1 request(s) on the old link cancelled"); 10:51:15.5 on and 10:51:17.9 off
  both applied at 10:51:18, so the light did not visibly change. Each power
  change re-arms OnPowerUp, and the bulb then goes silent for 1–2 s on the
  advertising bearer. A delivery matter of the mesh backend, not of clap
  detection.
- Hit pairs logged ~125 ms apart with one shared score ("Silence 0.41" ×2 at
  10:50:49 and 10:50:55, "Snap 0.15 accepted" ×2 at 10:51:03): pairs below the
  old 150 ms minimum, or doubles whose window the model called Silence.
  (Log times are quantised by the 128 ms capture chunks.)
- Since 2026-10-20 16:40, the top-1 labels of gate hits: Silence 1540, Typing
  909, Inside, small room 608, Crumpling 243, Speech 119 … A soft or untypical
  clap in a mostly quiet second is "Silence" or "Inside, small room" to
  YAMNet; its clap evidence is spread thin.

**The model on synthetic hits** (noise bursts in a −60 dBFS room, the real
YAMNet file): a loud 8 ms burst → Cap gun 0.33, Gunshot 0.15, Hands 0.04,
Slap 0.03, Chop 0.06; a soft one → Cap gun 0.11 or Inside, small room 0.11;
a bright 4 ms "snap" → Inside, small room 0.11, nothing in the family above
0.03. Synthetic bursts are not claps, but it shows the impulsive classes
carry the evidence when Clapping does not.

**Clap family** (weights in `haus/clap.py`): Clapping, Hands, Applause,
Slap/smack, Finger snapping 1.0 (what a clap or snap is); Cap gun, Whip,
Crack, Burst/pop, Chop, Wood block 0.5 (impulsive sounds a hand makes too);
Knock, Tap 0.25 (a door or a desk: support, never enough alone at the
default sensitivity). Left out: Clicking (a mouse's double click is two
clicks 100–200 ms apart, quiet around: it would toggle), Crumpling (a long
rustle), the room classes (neutral). Vetoes: Speech, Child speech,
Conversation, Narration, Music, Typing, Typewriter, Computer keyboard.
Knock and Tap were vetoes before; a knock now only adds a little.

**Bars** (`bars(sensitivity)`, s = sensitivity/100):

| | s = 0 | 50 | 100 |
|---|---|---|---|
| first hit: family ≥ 0.5 − 0.4 s (and > every veto) | 0.50 | 0.30 | 0.10 |
| … clap-like shape (×0.6) | 0.30 | 0.18 | 0.06 |
| partner: ¼ of the first bar (clap-like: 0) | 0.125 | 0.075 | 0.025 |
| a veto refuses a partner from (and above its family) | 0.15 | 0.25 | 0.35 |

Clap-like: rises ≥ 15 dB over the 30 ms before within a block and falls
back within 50 ms. Pairing: one accepted hit and one partner, 120–800 ms
apart (was two accepted, 150–600 ms), still no other sharp sound 600 ms
before or 300 ms after, so typing bursts and applause never pair. Kind: Snap
when Finger snapping outscores the clap classes, Clap otherwise, none below
0.05 (it takes the partner's; two without a kind are a Clap).

**Gate**: after a hit, its reverberation kept the gate "busy" (the blocks
before were not quiet), so a softer second clap 150 ms later rising on the
tail never reached the model (pinned by a test that fails on the old gate).
Within 800 ms after a hit, a block that jumps 4 dB over the last 30 ms now
starts a hit too, and it ends once it falls back near where it started.

**Tuning with the user**: `hauslicht-ctl clap-hits` lists the last 30 hits
with the gate's numbers, the family score, the top 3 classes and why a hit
was not accepted; debug log lines carry the same. At sensitivity 100 the
bars are now far lower than before (the sum, the shape factor, a free
partner): if anything that is not a clap toggles, try 60–80 first and read
`clap-hits` for the hit that did it.

Still open: in a very live room (reverb only ~6 dB under the clap, RT60
0.8 s, synthetic) a clap does not fall back 12 dB within 100 ms and the gate
treats it as a sustained sound, before and after this change. If
`clap-hits` stays empty while the user claps, DECAY_S is the next knob.
