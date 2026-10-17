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
