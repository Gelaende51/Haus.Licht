import copy

from haus.profiles import DEFAULT, migrate, new_state

FAKE_KEY = "00112233445566778899aabbccddeeff"

V1 = {  # the shape of the single-bulb hauslicht.json, with fake keys
    "netkey": FAKE_KEY, "appkey": FAKE_KEY, "iv_index": 0, "netkey_index": 0, "appkey_index": 0,
    "provisioner_addr": 1, "next_unicast": 3, "seq": 1234,
    "devices": {"0x0002": {"device_key": FAKE_KEY, "uuid": "ab" * 16, "address": "F0:D1:B8:00:00:01",
                           "models": [4096, 4864, 4098]}},
    "control_model": 4096,
    "sync": {"mode": "maximum", "slider": 0, "track": "always", "range_lo": 0.0, "range_hi": 200.0,
             "curve": -26.0, "level": 50.0, "lid": "both", "off_at_zero": False},
    "presets": {"evening": {"mode": "coupling", "track": "night", "off_at_zero": True, "lid_state": False,
                            "range_lo": 10.0, "range_hi": 90.0, "curve": 20.0, "level": 30.0, "slider": 40},
                "default": {"track": "never", "level": 70.0}},
}


def test_v1_becomes_one_dark_volkslicht_on_the_default_profile():
    state = copy.deepcopy(V1)
    assert migrate(state) is True
    assert state["version"] == 4
    assert state["bulbs"] == {"mesh:0002": {"kind": "mesh", "name": "Hauslicht - 0002", "profile": DEFAULT,
                                            "power": False, "use": True, "icon_on": "", "icon_off": "",
                                            "config": {"unicast": "0x0002", "scale": 100}}}
    assert state["profiles"][DEFAULT] == {"track": "always", "off_at_zero": False, "range_lo": 0.0,
                                          "range_hi": 200.0, "curve": -26.0, "level": 50.0, "hue": 30.0, "sat": 0.0, "follow_ct": False, "shift": 100.0}
    assert state["settings"] == {"mode": "maximum", "slider": 0, "lid": "both", "flick": "on", "flick_last": "on",
                                 "widgets": 1, "widget_bulbs": {"1": ["mesh:0002"]}, "selected": {"1": "all"},
                                 "autoconnect": False, "autoconnect_ignore": [],
                                 "widget_clap": {"1": "off"}, "clap_source": "", "clap_sensitivity": 50,
                                 "exclusive_adapter": "", "monitor_power": True,
                                 "audio_source": "", "audio_color": True, "follow_rate": 2,
                                 "video_max": 100, "video_smooth": 0.5, "video_token": ""}
    assert "sync" not in state and "presets" not in state


def test_presets_become_profiles_without_global_keys():
    state = copy.deepcopy(V1)
    migrate(state)
    assert state["profiles"]["evening"] == {"track": "night", "off_at_zero": True, "range_lo": 10.0,
                                            "range_hi": 90.0, "curve": 20.0, "level": 30.0, "hue": 30.0, "sat": 0.0, "follow_ct": False, "shift": 100.0}
    assert state["profiles"]["default (preset)"]["track"] == "never"
    assert state["profiles"]["default (preset)"]["level"] == 70.0


def test_mesh_network_state_is_untouched_and_migration_is_idempotent():
    state = copy.deepcopy(V1)
    migrate(state)
    for key in ("netkey", "appkey", "iv_index", "seq", "devices", "control_model", "provisioner_addr"):
        assert state[key] == V1[key]
    again = copy.deepcopy(state)
    assert migrate(again) is False
    assert again == state


def test_lid_state_and_bad_values():
    state = copy.deepcopy(V1)
    del state["sync"]["lid"]
    state["sync"]["lid_state"] = False
    state["sync"]["screen_state"] = True
    state["sync"]["track"] = "sometimes"
    migrate(state)
    assert state["settings"]["lid"] == "never"
    assert state["profiles"][DEFAULT]["track"] == "always"


def test_removed_mesh_bulbs_stay_removed_and_unknown_profiles_fall_back():
    state = copy.deepcopy(V1)
    migrate(state)
    state["bulbs"].pop("mesh:0002")
    state["bulbs"]["mock:1"] = {"kind": "mock", "name": "m", "profile": "gone", "power": True, "config": {}}
    state["settings"]["selected"] = "mesh:0002"
    migrate(state)
    assert "mesh:0002" not in state["bulbs"]
    assert state["bulbs"]["mock:1"]["profile"] == DEFAULT
    assert state["settings"]["selected"] == {"1": "all"}


def test_new_state():
    s = new_state()
    assert s["bulbs"] == {} and DEFAULT in s["profiles"] and s["settings"]["flick"] == "on"


V2 = {  # schema 2: one widget, the selection a string, bulbs without use or icons
    "version": 2, "netkey": FAKE_KEY, "devices": {"0x0002": {"device_key": FAKE_KEY}},
    "bulbs": {"mesh:0002": {"kind": "mesh", "name": "Volkslicht", "profile": DEFAULT, "power": True,
                            "config": {"unicast": "0x0002", "scale": 100}},
              "mock:1": {"kind": "mock", "name": "Desk", "profile": DEFAULT, "power": False, "config": {}}},
    "profiles": {DEFAULT: {"track": "always", "off_at_zero": False, "range_lo": 0.0, "range_hi": 100.0,
                           "curve": 0.0, "level": 50.0}},
    "settings": {"mode": "maximum", "slider": 40, "lid": "both", "flick": True, "selected": "mock:1"},
}


def test_v2_string_selection_becomes_widget_1_and_every_bulb_joins_widget_1():
    state = copy.deepcopy(V2)
    assert migrate(state) is True
    assert state["version"] == 4
    settings = state["settings"]
    assert settings["widgets"] == 1 and settings["selected"] == {"1": "mock:1"}
    assert settings["widget_bulbs"] == {"1": ["mesh:0002", "mock:1"]}
    assert list(state["bulbs"]) == ["mesh:0002", "mock:1"]  # the list order is kept
    for entry in state["bulbs"].values():
        assert entry["use"] is True and entry["icon_on"] == "" and entry["icon_off"] == ""
    assert state["bulbs"]["mesh:0002"]["power"] is True  # not re-created from `devices` as in schema 1
    again = copy.deepcopy(state)
    assert migrate(again) is False


def test_widget_lists_and_selections_are_cleaned():
    state = copy.deepcopy(V2)
    state["settings"].update(widgets=2, selected={"1": "mock:1", "2": "mock:1", "5": "mesh:0002"},
                             widget_bulbs={"1": ["mock:1", "mesh:0002", "mock:1", "mock:9"],
                                           "2": "nonsense", "3": ["mock:1"]})
    state["bulbs"]["mesh:0002"].update(use=False, icon_on="lightbulb", icon_off="relative/path.svg")
    migrate(state)
    assert state["settings"]["widget_bulbs"] == {"1": ["mesh:0002", "mock:1"], "2": []}
    assert state["bulbs"]["mesh:0002"]["use"] is False and state["bulbs"]["mock:1"]["use"] is True
    assert state["bulbs"]["mesh:0002"]["icon_on"] == "lightbulb"
    assert state["bulbs"]["mesh:0002"]["icon_off"] == ""
    assert state["settings"]["selected"] == {"1": "mock:1", "2": "all"}  # mock:1 is not in widget 2
    state["settings"]["selected"] = {"1": "mesh:0002"}
    migrate(state)
    assert state["settings"]["selected"] == {"1": "all", "2": "all"}  # not in use: no selection


def test_colour_keys_are_filled_and_checked():
    state = new_state()
    state["profiles"]["warm"] = {"track": "never", "hue": 500, "sat": "x", "follow_ct": "on", "shift": -5}
    migrate(state)
    assert {k: state["profiles"][DEFAULT][k] for k in ("hue", "sat", "follow_ct", "shift")} == \
        {"hue": 30.0, "sat": 0.0, "follow_ct": False, "shift": 100.0}
    assert {k: state["profiles"]["warm"][k] for k in ("hue", "sat", "follow_ct", "shift")} == \
        {"hue": 360.0, "sat": 0.0, "follow_ct": True, "shift": 0.0}
