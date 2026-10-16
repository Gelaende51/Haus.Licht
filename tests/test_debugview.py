"""The debug window's Qt-free parts: line parsing, swatch colour, state updates."""


from haus import debugview as dv
from haus.debugview import ViewState, colour_text, parse_line, swatch_rgb


def test_importable_without_qt():
    assert callable(dv.run)


def test_parse_line():
    assert parse_line(b'{"event": "flick", "bulb": "mock:1"}\n') == {"event": "flick", "bulb": "mock:1"}
    assert parse_line("") is None
    assert parse_line("not json") is None
    assert parse_line('{"error": "x"}') is None
    assert parse_line("[1, 2]") is None


def test_swatch_scaling():
    assert swatch_rgb(False, 80, None) == (0, 0, 0)
    assert swatch_rgb(True, 100, None) == dv.WARM_WHITE
    assert swatch_rgb(True, 0, None) == (0, 0, 0)
    assert swatch_rgb(True, 50, {"rgb": [200, 100, 0]}) == (100, 50, 0)
    assert swatch_rgb(True, 100, {"rgb": "#ff8000"}) == (255, 128, 0)
    assert swatch_rgb(True, 150, {"rgb": [10, 10, 10]}) == (10, 10, 10)
    assert swatch_rgb(True, None, None) == (0x80, 0x80, 0x80)  # idle
    assert swatch_rgb(False, None, None) == (0, 0, 0)


def test_colour_text():
    assert colour_text(None) == "white"
    assert colour_text({"hs": [30, 40]}) == "hs 30°/40 %"
    assert colour_text({"ct": 3400}) == "ct 3400 K"
    assert colour_text({"rgb": [255, 128, 0]}) == "rgb #ff8000"


SNAP = {"event": "snapshot", "t": 1760600000.12,
        "bulbs_list": [{"id": "mock:2", "name": "Shelf", "power": False, "output": 0.0, "use": True},
                       {"id": "mock:1", "name": "Desk", "power": True, "output": 50.0, "use": True},
                       {"id": "mock:3", "name": "Idle", "power": True, "output": None, "use": False}],
        "intended": {"mock:1": {"power": True, "percent": 50.0, "color": None},
                     "mock:2": {"power": False, "percent": 10.0, "color": None},
                     "mock:3": {"power": True, "percent": None, "color": None}}}


def test_snapshot_then_events():
    s = ViewState()
    r = s.apply(SNAP)
    assert r["rebuild"] and s.order == ["mock:2", "mock:1", "mock:3"]
    assert s.bulbs["mock:1"]["name"] == "Desk" and s.bulbs["mock:1"]["percent"] == 50.0
    assert s.bulbs["mock:3"]["percent"] is None
    r = s.apply({"event": "intended", "t": 1760600001.5, "bulb": "mock:1", "power": True,
                 "percent": 62.0, "color": {"ct": 3400}})
    assert r == {"rebuild": False, "changed": ["mock:1"], "flick": None}
    assert s.bulbs["mock:1"]["percent"] == 62.0 and s.bulbs["mock:1"]["last"] == 1760600001.5
    r = s.apply({"event": "flick", "t": 1760600002.0, "bulb": "mock:2", "power": True})
    assert r["flick"] == "mock:2"
    assert s.bulbs["mock:2"]["power"] is False  # a device event does not change the intended state
    assert s.log[0][1] is True and " flick mock:2 power=True" in s.log[0][0]


def test_unknown_bulb_adds_tile_and_snapshot_rebuilds():
    s = ViewState()
    s.apply(SNAP)
    r = s.apply({"event": "intended", "t": 1.0, "bulb": "mock:9", "power": True, "percent": 5, "color": None})
    assert r["rebuild"] and s.order[-1] == "mock:9"
    s.apply(SNAP)
    assert "mock:9" not in s.bulbs


def test_log_is_capped_and_newest_first():
    s = ViewState()
    for i in range(dv.LOG_MAX + 20):
        s.apply({"event": "connected", "t": float(i), "bulb": "mock:1"})
    assert len(s.log) == dv.LOG_MAX
    assert s.log[0][0].endswith("connected mock:1")
    assert dv.clock(0.5).endswith(".500")
