"""Double clap / snap: the transient gate, the double-hit timing, the speaker
check (synthetic signals, the classifier mocked), settings and ctl commands."""

import asyncio
import copy

import pytest

np = pytest.importorskip("numpy")

from haus import clap as clapmod  # noqa: E402
from haus.clap import CLAP, RATE, SNAP, DoubleHitDetector, TransientGate  # noqa: E402
from haus.common import UserError  # noqa: E402
from haus.profiles import migrate, new_state  # noqa: E402
from test_core import cmd, mock_state, run, settle  # noqa: E402

RNG = np.random.default_rng(7)


def room(seconds: float, rms: float = 0.002):
    return (RNG.standard_normal(int(seconds * RATE)) * rms).astype(np.float32)


def add_clap(sig, at: float, amp: float = 0.5):
    """A clap: a noise burst decaying with 8 ms time constant."""
    n = int(0.06 * RATE)
    t = np.arange(n) / RATE
    start = int(at * RATE)
    sig[start:start + n] += (RNG.standard_normal(n) * amp * np.exp(-t / 0.008)).astype(np.float32)
    return sig


class FakeYamnet:
    def __init__(self, scores=None) -> None:
        self.calls = 0
        self.scores = scores or {CLAP: 0.8}

    def __call__(self, window):
        assert window.shape == (clapmod.WINDOW,)
        self.calls += 1
        out = np.zeros(521, dtype=np.float32)
        for i, v in self.scores.items():
            out[i] = v
        return out


def detect(sig, clf=None, mode="both", sensitivity=50, monitor=(), chunk=800):
    det = DoubleHitDetector(clf or FakeYamnet(), mode, sensitivity)
    for t in monitor:
        det.monitor_onset(t)
    events = []
    for i in range(0, len(sig), chunk):
        events += det.feed(sig[i:i + chunk])
    return det, events


def doubles(events):
    return [e for e in events if e["event"] == "double"]


# ------------------------------------------------------------------ gate

def gate_onsets(sig, sensitivity=50):
    gate = TransientGate(sensitivity)
    blocks = sig[:len(sig) // 160 * 160].reshape(-1, 160).astype(np.float64)
    return [o for i, p in enumerate((blocks ** 2).mean(axis=1)) if (o := gate.feed(float(p), i)) is not None]


def test_gate_fires_once_per_clap_and_never_on_room_noise():
    assert gate_onsets(room(3.0)) == []
    onsets = gate_onsets(add_clap(room(3.0), 1.0))
    assert len(onsets) == 1 and abs(onsets[0] * 160 / RATE - 1.0) <= 0.011


def test_gate_ignores_a_sustained_sound():
    sig = room(3.0)
    t = np.arange(int(0.4 * RATE)) / RATE
    sig[RATE:RATE + len(t)] += (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)  # a vowel, a note
    assert gate_onsets(sig) == []


def test_gate_sensitivity_decides_a_soft_snap():
    sig = add_clap(room(3.0), 1.0, amp=0.02)
    assert gate_onsets(sig, sensitivity=0) == []
    assert len(gate_onsets(sig, sensitivity=100)) == 1


# ------------------------------------------------------------------ double-hit timing

def test_a_single_hit_is_no_double():
    det, events = detect(add_clap(room(3.0), 1.0))
    assert doubles(events) == []
    assert [e["label"] for e in events if e["event"] == "hit"] == ["Clap"]


def test_two_hits_300_ms_apart_are_a_double_classified_once():
    clf = FakeYamnet()
    sig = add_clap(add_clap(room(3.0), 1.0), 1.3)
    det, events = detect(sig, clf)
    (d,) = doubles(events)
    assert d["label"] == "Clap" and abs(d["gap"] - 0.3) < 0.02
    assert clf.calls == 1  # the second hit lies in the first one's window


def test_two_hits_900_ms_apart_are_not():
    det, events = detect(add_clap(add_clap(room(3.5), 1.0), 1.9))
    assert doubles(events) == []
    assert len([e for e in events if e["event"] == "hit"]) == 2


def test_a_hit_mirrored_in_the_monitor_is_dropped():
    sig = add_clap(add_clap(room(3.0), 1.0), 1.3)
    det, events = detect(sig, monitor=[1.31])  # the speakers played the second one
    assert doubles(events) == []
    assert any(e["event"] == "hit" and e["mirrored"] for e in events)


def test_a_third_hit_soon_after_is_applause_not_a_double():
    sig = room(3.5)
    for at in (1.0, 1.25, 1.5):
        add_clap(sig, at)
    assert doubles(detect(sig)[1]) == []


def test_cooldown_after_a_double():
    sig = room(4.0)
    for at in (1.0, 1.3, 2.05, 2.35):  # the second pair starts 750 ms after the first ended
        add_clap(sig, at)
    assert len(doubles(detect(sig)[1])) == 1


@pytest.mark.parametrize("scores, mode", [
    ({353: 0.7, CLAP: 0.4}, "both"),   # Knock wins
    ({0: 0.6, CLAP: 0.5}, "both"),     # Speech wins
    ({132: 0.9, SNAP: 0.3}, "both"),   # Music wins
    ({CLAP: 0.2}, "both"),             # below the threshold at sensitivity 50
    ({CLAP: 0.8}, "snap"),             # a clap while only snaps count
])
def test_classifier_vetoes(scores, mode):
    sig = add_clap(add_clap(room(3.0), 1.0), 1.3)
    assert doubles(detect(sig, FakeYamnet(scores), mode=mode)[1]) == []


def test_snaps_count_in_snap_and_both():
    sig = add_clap(add_clap(room(3.0), 1.0, 0.1), 1.35, 0.1)
    for mode in ("snap", "both"):
        (d,) = doubles(detect(sig, FakeYamnet({SNAP: 0.6}), mode=mode)[1])
        assert d["label"] == "Snap"


# ------------------------------------------------------------------ model file

def test_a_model_with_the_wrong_checksum_is_not_kept(tmp_path):
    src = tmp_path / "src.tflite"
    src.write_bytes(b"not the model")
    dest = tmp_path / "cache" / "yamnet.tflite"
    with pytest.raises(clapmod.CaptureError):
        clapmod.fetch_model(dest, url=src.as_uri())
    assert not dest.exists() and not dest.with_suffix(".part").exists()


# ------------------------------------------------------------------ settings

def test_settings_migration_fills_and_checks_the_clap_keys():
    state = new_state()
    s = state["settings"]
    assert (s["clap"], s["clap_last"], s["clap_source"], s["clap_sensitivity"]) == ("off", "both", "", 50)
    s.update(clap="loud", clap_last="off", clap_source="a b", clap_sensitivity=250)
    migrate(state)
    assert (s["clap"], s["clap_last"], s["clap_source"], s["clap_sensitivity"]) == ("off", "both", "", 100)
    s.update(clap="snap")
    migrate(state)
    assert s["clap_last"] == "snap"
    again = copy.deepcopy(state)
    assert migrate(again) is False
    for key in ("clap", "clap_last", "clap_source", "clap_sensitivity"):  # an old file without them
        del state["settings"][key]
    migrate(state)
    assert state["settings"]["clap"] == "off"


# ------------------------------------------------------------------ ctl

@pytest.fixture
def no_capture(monkeypatch):
    """The service starts nothing: no model download, no microphone."""
    applied = []

    def apply(self, mode, source, sensitivity):
        applied.append((mode, source, sensitivity))
        self.mode, self.source, self.sensitivity = mode, source, sensitivity
        self.state = "off" if mode == "off" else "listening"

    monkeypatch.setattr(clapmod.ClapService, "apply", apply)
    monkeypatch.setattr(clapmod, "missing", lambda: [])
    return applied


def test_ctl_clap_commands(tmp_path, monkeypatch, no_capture):
    async def sources():
        return [{"name": "mic1", "description": "Digital Microphone", "default": True}]
    monkeypatch.setattr(clapmod, "list_sources", sources)

    async def body(d):
        g = await cmd(d, "get")
        assert g["clap"] == "off" and g["clap_status"]["available"] is True
        g = await cmd(d, "clap", "snap")
        assert g["clap"] == "snap" and g["clap_last"] == "snap"
        g = await cmd(d, "set", "clap=off")
        assert g["clap"] == "off" and g["clap_last"] == "snap"
        g = await cmd(d, "set", "clap_sensitivity=80")
        assert g["clap_sensitivity"] == 80
        g = await cmd(d, "clap-source", "mic1")
        assert g["clap_source"] == "mic1"
        g = await cmd(d, "set", "clap_source=default")
        assert g["clap_source"] == ""
        with pytest.raises(UserError):
            await cmd(d, "clap", "loud")
        assert (await cmd(d, "clap-sources"))["sources"][0]["name"] == "mic1"
        meter = await cmd(d, "clap-meter")
        assert meter["clap"] == "off" and meter["level"] is None and meter["last"] is None
        assert no_capture[0] == ("off", "", 50) and no_capture[-1] == ("off", "", 80)
        saved = d.state_path.read_text()
        assert '"clap_sensitivity": 80' in saved
    run(tmp_path, mock_state(mock_1={}), body)


def test_clap_is_refused_without_the_extra(tmp_path, monkeypatch, no_capture):
    monkeypatch.setattr(clapmod, "missing", lambda: ["numpy", "ai-edge-litert"])

    async def body(d):
        with pytest.raises(UserError, match="requirements-clap.txt"):
            await cmd(d, "clap", "both")
        assert d.settings["clap"] == "off"
        g = await cmd(d, "clap", "off")  # off is always fine
        assert g["clap_status"]["available"] is False and "requirements-clap.txt" in g["clap_status"]["install"]
    run(tmp_path, mock_state(mock_1={}), body)


def test_a_double_toggles_widget_1s_selection(tmp_path, no_capture):
    async def body(d):
        await cmd(d, "select", "mock:1")
        d.on_clap({"label": "Clap", "score": 0.8})
        await settle(d)
        assert d.bulbs["mock:1"].power is True and d.bulbs["mock:2"].power is False
        d.on_clap({"label": "Snap", "score": 0.7})
        assert d.bulbs["mock:1"].power is False
        await cmd(d, "select", "all")
        d.on_clap({"label": "Clap", "score": 0.8})  # none on: all on
        assert d.bulbs["mock:1"].power and d.bulbs["mock:2"].power
        d.sleeping = True
        d.on_clap({"label": "Clap", "score": 0.8})  # asleep: hands off
        assert d.bulbs["mock:1"].power
        d.sleeping = False
    run(tmp_path, mock_state(mock_1={}, mock_2={}), body)


def test_the_service_reports_unavailable_without_the_extra(monkeypatch):
    monkeypatch.setattr(clapmod, "missing", lambda: ["numpy"])

    async def main():
        svc = clapmod.ClapService(lambda ev: None)
        svc.apply("both", "", 50)
        await asyncio.sleep(0.05)
        return svc.describe()
    st = asyncio.run(main())
    assert st["state"] == "unavailable" and st["available"] is False and st["missing"] == ["numpy"]
