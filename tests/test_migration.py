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
    assert state["version"] == 2
    assert state["bulbs"] == {"mesh:0002": {"kind": "mesh", "name": "Volkslicht", "profile": DEFAULT,
                                            "power": False,
                                            "config": {"unicast": "0x0002", "scale": 100}}}
    assert state["profiles"][DEFAULT] == {"track": "always", "off_at_zero": False, "range_lo": 0.0,
                                          "range_hi": 200.0, "curve": -26.0, "level": 50.0}
    assert state["settings"] == {"mode": "maximum", "slider": 0, "lid": "both", "flick": True,
                                 "selected": "all"}
    assert "sync" not in state and "presets" not in state


def test_presets_become_profiles_without_global_keys():
    state = copy.deepcopy(V1)
    migrate(state)
    assert state["profiles"]["evening"] == {"track": "night", "off_at_zero": True, "range_lo": 10.0,
                                            "range_hi": 90.0, "curve": 20.0, "level": 30.0}
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
    assert state["settings"]["selected"] == "all"


def test_new_state():
    s = new_state()
    assert s["bulbs"] == {} and DEFAULT in s["profiles"] and s["settings"]["flick"] is True
