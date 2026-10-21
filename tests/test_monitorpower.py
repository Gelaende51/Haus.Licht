"""Follow monitor power (desktops): a fake /sys/class/drm tree and chassis_type.
This machine is a laptop, so none of it can be tried live."""

import asyncio

import pytest

from haus import daemon as daemonmod
from haus.common import save_state
from haus.daemon import Daemon
from haus.desktop import MonitorPower, is_laptop
from haus.profiles import migrate, new_state
from test_core import FakeDesktop, cmd, mock_state


def connector(drm, name, status="connected", dpms="On", enabled="enabled"):
    path = drm / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "status").write_text(status + "\n")
    (path / "dpms").write_text(dpms + "\n")
    (path / "enabled").write_text(enabled + "\n")


def two_polls(mp):
    """A disconnect counts on its second read: return the first event."""
    return mp.poll() or mp.poll()


# ---------------------------------------------------------------- machine type

def test_laptop_by_chassis_or_lid(tmp_path):
    chassis = tmp_path / "chassis_type"
    no_lid = tmp_path / "nolid"
    for value, laptop in (("10", True), ("9", True), ("31", True), ("3", False), ("6", False)):
        chassis.write_text(value + "\n")
        assert is_laptop(no_lid, chassis) is laptop
    lid = tmp_path / "lid"
    (lid / "LID0").mkdir(parents=True)
    chassis.write_text("3\n")
    assert is_laptop(lid, chassis) is True  # a lid makes a laptop whatever the chassis says
    assert is_laptop(no_lid, tmp_path / "missing") is False


def test_setting_default_and_validation():
    state = new_state()
    state["settings"].pop("monitor_power", None)  # a state from before the option
    migrate(state)
    assert state["settings"]["monitor_power"] is True
    assert state["settings"]["lid"] == "both"  # unchanged
    state["settings"]["monitor_power"] = "nonsense"
    migrate(state)
    assert state["settings"]["monitor_power"] is True


# ---------------------------------------------------------------- detection

def test_dp_disconnect_while_on_is_off_and_reconnect_on(tmp_path):
    connector(tmp_path, "card1-DP-1")
    mp = MonitorPower(tmp_path)
    assert mp.poll(baseline=True) is None
    connector(tmp_path, "card1-DP-1", status="disconnected")
    assert mp.poll() is None  # one read could be a link flap
    assert mp.poll() == ("off", "card1-DP-1")
    connector(tmp_path, "card1-DP-1")
    assert mp.poll() == ("on", "card1-DP-1")
    assert mp.status(False)["available"] is True


def test_flap_of_one_read_is_nothing(tmp_path):
    connector(tmp_path, "card1-DP-1")
    mp = MonitorPower(tmp_path)
    mp.poll(baseline=True)
    connector(tmp_path, "card1-DP-1", status="disconnected")
    assert mp.poll() is None
    connector(tmp_path, "card1-DP-1")
    assert mp.poll() is None and mp.poll() is None


def test_standby_is_no_change(tmp_path):
    connector(tmp_path, "card1-DP-1")
    mp = MonitorPower(tmp_path)
    mp.poll(baseline=True)
    connector(tmp_path, "card1-DP-1", dpms="Off")
    assert two_polls(mp) is None and mp.poll() is None
    connector(tmp_path, "card1-DP-1")
    assert two_polls(mp) is None


def test_disconnect_while_standby_is_no_change(tmp_path):
    connector(tmp_path, "card1-DP-1")
    mp = MonitorPower(tmp_path)
    mp.poll(baseline=True)
    connector(tmp_path, "card1-DP-1", dpms="Off")
    assert mp.poll() is None
    connector(tmp_path, "card1-DP-1", status="disconnected", dpms="Off")
    assert two_polls(mp) is None and mp.poll() is None
    connector(tmp_path, "card1-DP-1")  # waking from standby: no "on" either
    assert two_polls(mp) is None


def test_two_monitors_off_only_when_both_are(tmp_path):
    connector(tmp_path, "card1-DP-1")
    connector(tmp_path, "card1-DP-2")
    mp = MonitorPower(tmp_path)
    mp.poll(baseline=True)
    connector(tmp_path, "card1-DP-1", status="disconnected")
    assert two_polls(mp) is None and mp.poll() is None  # DP-2 still on
    connector(tmp_path, "card1-DP-2", status="disconnected")
    assert two_polls(mp) == ("off", "card1-DP-2")
    connector(tmp_path, "card1-DP-1")
    assert mp.poll() == ("on", "card1-DP-1")


def test_no_watchable_connector_is_not_available(tmp_path):
    connector(tmp_path, "card1-HDMI-A-1")
    mp = MonitorPower(tmp_path)
    mp.poll(baseline=True)
    st = mp.status(False)
    assert st["available"] is False and st["why"]
    assert st["monitors"][0]["watch"] == "none"
    empty = MonitorPower(tmp_path / "none")
    empty.poll(baseline=True)
    assert empty.status(False)["available"] is False and empty.status(False)["why"]
    assert mp.status(True)["available"] is False  # a laptop never


def test_builtin_panel_is_not_watched(tmp_path):
    connector(tmp_path, "card1-eDP-1")
    mp = MonitorPower(tmp_path)
    mp.poll(baseline=True)
    assert mp.status(False)["monitors"] == []


# ---------------------------------------------------------------- DDC/CI

class FakeDdc:
    def __init__(self):
        self.d6 = "x01"
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "detect":
            return ("Display 1\n   I2C bus:  /dev/i2c-4\n   DRM connector:    card1-HDMI-A-1\n"
                    "   Monitor:          DEL:DELL U2415:XYZ\n\n")
        return f"VCP D6 SNC {self.d6}\n"


def test_ddc_05_is_off_and_04_ignored(tmp_path):
    connector(tmp_path, "card1-HDMI-A-1")  # HDMI stays connected when switched off
    ddc = FakeDdc()
    mp = MonitorPower(tmp_path, ddc=ddc)
    mp.poll(baseline=True)
    assert mp.ddc_apply(mp.ddc_read()) is None  # 01: on, as it was
    assert mp.status(False)["available"] is True
    ddc.d6 = "x04"  # standby
    assert mp.ddc_apply(mp.ddc_read()) is None
    ddc.d6 = "x05"  # power button
    assert mp.ddc_apply(mp.ddc_read()) == ("off", "card1-HDMI-A-1")
    ddc.d6 = "x01"
    assert mp.ddc_apply(mp.ddc_read()) == ("on", "card1-HDMI-A-1")
    assert sum(1 for c in ddc.calls if c[0] == "detect") == 1  # detect only when the connectors change


def test_ddc_05_while_dpms_off_is_ignored(tmp_path):
    connector(tmp_path, "card1-HDMI-A-1")
    ddc = FakeDdc()
    mp = MonitorPower(tmp_path, ddc=ddc)
    mp.poll(baseline=True)
    mp.ddc_apply(mp.ddc_read())
    connector(tmp_path, "card1-HDMI-A-1", dpms="Off")
    mp.poll()
    ddc.d6 = "x05"
    assert mp.ddc_apply(mp.ddc_read()) is None


def test_ddc_polls_rarely():
    mp = MonitorPower(ddc=FakeDdc())
    assert mp.ddc_due(100.0) and not mp.ddc_due(105.0) and mp.ddc_due(110.0)
    assert not MonitorPower(ddc=None).ddc_due(100.0)


# ---------------------------------------------------------------- the core

class DesktopPC(FakeDesktop):
    def __init__(self, drm) -> None:
        super().__init__()
        self.laptop = False
        self.monitor_power = MonitorPower(drm)


def run_with(tmp_path, desktop, state, body, monkeypatch):
    monkeypatch.setattr(daemonmod, "MONITOR_POLL_S", 3600.0)  # the test polls by hand
    path = tmp_path / "state.json"
    save_state(path, state)

    async def main():
        d = Daemon(path, tmp_path / "sock", desktop=desktop)
        await d.start()
        try:
            return await body(d)
        finally:
            await d.stop()

    return asyncio.run(main())


def test_core_switches_off_and_back_on(tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    connector(drm, "card1-DP-1")

    async def body(d):
        g = await cmd(d, "get")
        assert g["laptop"] is False and g["monitor_power"]["available"] is True and g["monitor_power"]["follow"]
        connector(drm, "card1-DP-1", status="disconnected")
        d.check_monitor_power()
        d.check_monitor_power()
        assert not any(b.power for b in d.bulbs.values())
        connector(drm, "card1-DP-1")
        d.check_monitor_power()
        assert all(b.power for b in d.bulbs.values())
        await cmd(d, "monitor-power", "off")
        assert d.settings["monitor_power"] is False
        connector(drm, "card1-DP-1", status="disconnected")
        d.check_monitor_power()
        d.check_monitor_power()
        assert all(b.power for b in d.bulbs.values())  # option off: no switch

    run_with(tmp_path, DesktopPC(drm), mock_state(mock_1={"power": True}, mock_2={"power": True}), body,
             monkeypatch)


def test_core_standby_keeps_the_lights(tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    connector(drm, "card1-DP-1")

    async def body(d):
        connector(drm, "card1-DP-1", dpms="Off")
        d.check_monitor_power()
        connector(drm, "card1-DP-1", status="disconnected", dpms="Off")
        d.check_monitor_power()
        d.check_monitor_power()
        assert all(b.power for b in d.bulbs.values())

    run_with(tmp_path, DesktopPC(drm), mock_state(mock_1={"power": True}), body, monkeypatch)


def test_core_laptop_unchanged(tmp_path, monkeypatch):
    async def body(d):
        g = await cmd(d, "get")
        assert g["laptop"] is True and g["lid"] == "both"
        assert g["monitor_power"]["available"] is False and g["monitor_power"]["why"]
        assert d.monitor_watcher is None
        d.check_monitor_power()  # nothing to watch
        assert all(b.power for b in d.bulbs.values())

    desktop = FakeDesktop()
    desktop.laptop = True
    run_with(tmp_path, desktop, mock_state(mock_1={"power": True}), body, monkeypatch)


def test_monitor_power_value_checked(tmp_path, monkeypatch):
    async def body(d):
        with pytest.raises(Exception):
            await cmd(d, "set", "monitor_power=maybe")

    run_with(tmp_path, FakeDesktop(), mock_state(mock_1={}), body, monkeypatch)
