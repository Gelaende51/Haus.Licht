# Haus.Licht

**English** · [Deutsch](docs/i18n/README.de.md)

Let smart bulbs follow your laptop's screen brightness on Linux, with a tray
widget and a slider in KDE Plasma's brightness applet. Made for Ledvance
Volkslicht bulbs (Bluetooth SIG Mesh), with experimental backends for many
other kinds. Local only: no vendor app, account or cloud.

Forked from [Licht.Haus](https://github.com/Gelaende51/Licht.Haus).

- **AI disclosure**
  - Agent: Claude Code with Claude Opus 5.5 and Fable (Anthropic) wrote the
    code and docs and analysed how the official app sets brightness; about
    52.5 M tokens processed (mostly cached context; 2.5 M of them for the
    multi-bulb round with its backends), 0.13 M generated before that round.
  - Author: tested on the real bulbs, chose the features and the slider modes.

[License: AGPL-3.0](LICENSE) · [Notes and findings](dev/takeaways.md) ·
[Issues](https://github.com/Gelaende51/Haus.Licht/issues) ·
[Donate (carbon offset for the tokens)](https://ko-fi.com/vault51)

## Features

- **Several bulbs, several kinds:** each bulb has its own on/off state (kept
  across restarts) and its own **profile** (screen window, curve, level,
  follow-screen rule). Bulbs and profiles are managed in the widget's
  **Configure** dialog (*Bulbs*, *Profiles* pages); the popup's header picks
  which bulb (or *All bulbs*) the controls act on.
- **Follows the screen:** the bulbs follow the laptop's backlight (`hauslicht
  sync`, a systemd user service): *always*, only with Night Light, only in
  daylight, only in dark mode, only in light mode, or *never*.
- **Tray widget** (Plasma 6): a two-handle **screen window** (screen
  brightness at the left handle means off, at the right one full), a
  **curve**, a brightness slider (a manual setting until *Resume following*),
  connection state, **Flick** and **On/Off** switches. Middle click on the
  icon switches on/off, the wheel makes brighter/darker.
- **Keyboard slider as a remote:** Plasma's *Keyboard brightness* slider
  moves a widget control of your choice (right or left handle, curve,
  brightness, or nothing); needs `sudo install/setup-root.sh` once.
- **Turn off at 0 %** (optional): otherwise a 0.5 % glow stays at 0 %.
- **Lid events:** closing the lid switches the bulbs off, opening on (or only
  one of them, or never); off before the laptop sleeps.
- **Flick toggle** (mesh bulbs; switch *Flick*): flicking the wall switch off
  and on toggles the light at once, because the daemon keeps the bulb's
  power-on setting at the toggled state.
  - Pro: the wall switch toggles even while the laptop drives the bulb; the
    bulb boots straight into the new state.
  - Contra: flicks shorter than about 1–5 s are bridged by the bulb's supply
    and go unnoticed; after a long power cut while lit the light comes back
    dark until the laptop switches it on; one small settings write to the
    bulb per toggle. Off: the wall switch works like a normal switch.
- **Scripts:** `hauslicht on | off | dim 40 | status | slider 60 | nudge 5 |
  select ID | discover | add | remove`, and `hauslicht-ctl` with JSON
  ([protocol](docs/ctl-protocol.md)).

## Supported bulbs

| Kind | Library | Status | What you enter to add one |
|---|---|---|---|
| Ledvance Volkslicht, SIG mesh lights (`mesh`) | btmesh, bleak | **tested on hardware** | nothing: `hauslicht adopt` |
| Philips Hue Bridge (`hue`) | aiohttp | experimental (tested against fakes) | bridge host (press its link button first), light |
| Philips Hue Bluetooth (`huebt`) | HueBLE | experimental (tested against fakes) | Bluetooth address (search) |
| Home Assistant (`homeassistant`) | aiohttp | experimental (tested against fakes) | URL, access token, light entity |
| MQTT / zigbee2mqtt (`mqtt`) | aiomqtt | experimental (tested against fakes) | broker host, device or command topic |
| Matter server (`matter`) | aiohttp | experimental (tested against fakes) | server URL, node |
| WiZ (`wiz`) | pywizlight | experimental (tested against fakes) | host (search) |
| LIFX (`lifx`) | – (own LAN code) | experimental (tested against fakes) | host (search) |
| Yeelight (`yeelight`) | – (own LAN code) | experimental (tested against fakes) | host (search; enable LAN control) |
| Govee LAN API (`govee`) | – (own LAN code) | experimental (tested against fakes) | host (search; enable LAN control) |
| TP-Link Kasa / Tapo (`kasa`) | python-kasa | experimental (tested against fakes) | host (search); Tapo: TP-Link account e-mail and password |
| Tuya / Smart Life (`tuya`) | tinytuya | experimental (tested against fakes) | host, device ID, local key |
| Shelly (`shelly`) | aiohttp | experimental (tested against fakes) | host (search), password if set |
| Tasmota (`tasmota`) | aiohttp | experimental (tested against fakes) | host (search), user/password if set |
| ESPHome (`esphome`) | aioesphomeapi | experimental (tested against fakes) | host, encryption key |
| Triones / LEDBlue BLE (`ledble`) | led-ble | experimental (tested against fakes) | Bluetooth address (search) |

How each kind works and what it can do: [backend API](docs/backend-api.md),
[research notes](docs/backends-research.md).

## Install

Needs Linux with BlueZ, Python 3.12+, and for the widget and slider KDE
Plasma 6 with UPower.

```sh
git clone https://github.com/Gelaende51/Haus.Licht && cd Haus.Licht
sudo install/setup-root.sh             # once: keyboard slider, 1 s Bluetooth link timeout
install/install.sh                     # venv, commands, service, tray widget (mesh only)
install/install.sh --backend wiz,hue   # plus the libraries of these kinds
install/install.sh --all-backends      # plus every kind's libraries
systemctl --user enable --now hauslicht
```

`install/install.sh --dry-run …` prints what would be installed;
`hauslicht backends --requires KIND` lists a kind's pip packages.
Tests: `pip install -r requirements-dev.txt` (see the note on amqtt there).

## Adding bulbs

- **Most kinds:** widget → *Configure* → *Bulbs* → *Add a bulb*: choose the
  kind, *Search network* or enter the fields from the table, *Add*.
  From a terminal: `hauslicht discover [KIND]`, `hauslicht add KIND key=value…`.
- **Ledvance Volkslicht (mesh):** factory-reset the bulb (switch it on and off
  five times, 5 s each, then leave it on; it flashes). Within 8 minutes run
  `hauslicht adopt`. The bulb then leaves the Ledvance app. `hauslicht.json`
  holds the mesh keys: back it up, without it the bulbs can only be reset.

## Works with

- KDE Plasma 6 (brightness applet, PowerDevil, Night Light, colour scheme);
  without Plasma everything but widget and slider works.
- Home Assistant, zigbee2mqtt, Matter servers and Hue Bridges as bulb sources.
- BlueZ must scan Low Energy: `ControllerMode = dual` (or `le`) in
  `/etc/bluetooth/main.conf`.
- Conflicts with anything else that drives a fake or real keyboard backlight
  through UPower.

## Similar projects

- [ha-bluetooth-mesh](https://github.com/dasimon135/ha-bluetooth-mesh): Home
  Assistant integration on the same mesh stack (btmesh, MIT; this project
  reuses its provisioning script, see [LICENSE-btmesh](LICENSE-btmesh)). It
  does not know the Volkslicht's 0–100 brightness scale yet, and has no
  desktop integration.
- [LEDVANCE_BLE_MESH](https://github.com/Ultraworg/LEDVANCE_BLE_MESH): ESP32
  gateway for Ledvance mesh lamps, controlled from Home Assistant; needs extra
  hardware.
- BlueZ `bluetooth-meshd` / `mesh-cfgclient`: the official Linux mesh stack;
  it needs the Bluetooth adapter to itself.
