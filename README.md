# Haus.Licht

**English** · [Deutsch](docs/i18n/README.de.md)

Let smart bulbs follow your laptop's screen brightness on Linux, with a tray
widget and a slider in KDE Plasma's brightness applet. Made for Ledvance
Volkslicht bulbs (Bluetooth SIG Mesh), with experimental backends for many
other kinds. Local only: no vendor app, account or cloud.

Forked from [Licht.Haus](https://github.com/Gelaende51/Licht.Haus).

- **AI disclosure**
  - Agent: Claude Code with Claude Opus 5.5 (Anthropic) wrote the
    code and docs and analysed how the official app sets brightness; about
    52.5 M tokens processed (mostly cached context; 2.5 M of them for the
    multi-bulb round with its backends), 0.13 M generated before that round
    (as of 2026-10-16).
  - Author: tested on the real bulbs, chose the features and the slider modes.

[License: AGPL-3.0](LICENSE) · [Notes and findings](dev/takeaways.md) ·
[Issues](https://github.com/Gelaende51/Haus.Licht/issues) ·
[Donate (carbon offset for the tokens)](https://ko-fi.com/vault51)

## Features

**Privacy:** microphone, screen and notifications are read on this computer
only; nothing is stored or sent, and notification text is never logged.

### Follow the screen

- The bulbs follow the screen brightness (`hauslicht sync`, a systemd user
  service): *always*, only with Night Light, in daylight, in dark mode, in
  light mode, or *never*.
- **Screen window** (two handles: screen brightness at the left one means
  off, at the right one full) and a **curve** shape how the bulbs follow;
  colour bulbs can follow Night Light's red shift.
- **Several bulbs, several kinds:** each bulb has its own on/off state and
  its own **profile** (window, curve, level, colour, follow rule). Configure →
  *Bulbs* / *Profiles*; the popup's header picks the bulb (or *All bulbs*).
- **Manual / Automatic:** one button in the tray's heading (or the popup's
  header). Its icon shows the current mode; a click switches between holding
  the current brightness and following the screen. Moving the brightness
  slider holds a manual level until the monitor icon beside it is clicked.
- **Keyboard slider as a remote:** Plasma's *Keyboard brightness* slider moves
  a widget control of your choice (a window handle, the curve, the
  brightness); needs the root setup.
- **Turn off at 0 %** (optional): otherwise a 0.5 % glow stays.

### Wall switch, lid and power

- **Wall-switch flick** (mesh bulbs; switch *Flick*, mode on Configure →
  *Bulbs*): flicking the wall switch off and on toggles the light, because the
  daemon keeps the bulb's power-on setting at the toggled state.
  - *Off*: a normal switch. *On*: flicks toggle. *Fast*: like On, with
    Bluetooth tuned to reconnect faster. *Exclusive*: like Fast without a
    Bluetooth connection; Haus.Licht takes one Bluetooth adapter for itself
    (other Bluetooth devices on it stop working) and re-arms the bulb sooner.
  - Fast and Exclusive need the **root setup** (`sudo install/setup-root.sh`,
    or the widget asks for the administrator password): it loads the
    keyboard-slider driver, lets you open USB/serial lights, tunes BlueZ for
    fast reconnects and installs the helpers for the short link timeout and
    the exclusive adapter.
  - Hold the switch off for about 1.5 s or more: a shorter flick can be
    bridged by the bulb's supply. Off for up to about 10 s is a flick; after a
    longer power cut the light comes back lit.
- **Suspend and crashes are safe:** before sleep and on stop the bulbs' power-on
  setting goes back to "on", so the wall switch works like a normal switch
  while the laptop is away. After a crash or kill the service unit resets the
  bulbs that may still be armed (`hauslicht disarm --if-armed`).
- **Laptop lid:** closing switches the bulbs off, opening on (or only one of
  them, or never); off before the laptop sleeps.
- **Desktop monitor power** (desktops only, where it can be detected): the
  lights go off when every monitor is switched off (DisplayPort disconnect or
  DDC/CI power off) and back on when it returns. Standby and screen blanking
  never darken the room.

### Sound, video and notifications

- **Clap to toggle** (off by default; switch *Clap*): a double clap or
  finger snap toggles the widget's selection. Mode (clap, snap, both) per
  widget; microphone, sensitivity, test meter and recent sounds on Configure
  → *Sound*. A level gate listens; only a sharp sound goes to a small sound
  classifier (YAMNet) on this computer; sounds from the speakers are ignored.
- **Follow audio** (per bulb, Configure → *Sound*): loudness becomes
  brightness within the profile's range, pitch becomes colour on colour bulbs.
  Listens to what the computer plays (or any source).
- **Follow video** (per bulb, Configure → *Video*): each bulb follows a
  screen region (whole, a half or a quarter) up to a **max brightness**, with
  colour on colour bulbs and adjustable **time averaging**. The screen is read
  through the desktop's screen-sharing permission, asked once.
- Both are capped at 1–3 updates per second per bulb (default 2), never
  faster: rapid flashing is a photosensitivity risk.
- **Notification blink** (Configure → *Notifications*): rules per profile
  match app, title or text (regex) and blink, pulse or double-blink with a
  count, intensity and colour; lit bulbs only unless allowed. Do Not Disturb
  is respected.

### Widget and tools

- **Tray widget** (Plasma 6) with **Clap**, **Flick** and **On/Off**
  switches; middle click toggles, the wheel dims (scroll direction can be
  inverted). Extra widgets for the panel, each with its own bulbs.
- **Icon sets:** six sets for the tray and bulb icons, previewed in a
  drop-down on Configure → *Bulbs*, following the Plasma colour scheme;
  sources and licences in
  [plasmoid/haus.licht.tray/LICENSES.md](plasmoid/haus.licht.tray/LICENSES.md).
- **Identify and Test** (Configure → *Bulbs*, or `hauslicht identify` /
  `test`): Identify blinks a bulb slowly to find it (off after 2 min). Test
  runs brightest, a fade, colours and on/off from fast to slow.
  **Warning:** Test flashes quickly; not for people with photosensitive
  epilepsy.
- **Command line:** everything the widget does is also a `hauslicht` command
  (`hauslicht --help` groups them), e.g. `hauslicht on | off | dim 40 |
  status | flick exclusive | video on`; `hauslicht-ctl` speaks JSON
  ([protocol](docs/ctl-protocol.md)). `man hauslicht` and `man hauslicht-ctl`
  (also in German) after `install/install.sh`.
- **Tested on every push** (Python 3.12–3.14, man pages, QML lint), weekly
  in five distributions and with the widget in Plasma 6:
  [docs/ci.md](docs/ci.md).

## Supported bulbs

Status: the mesh kind is **tested on hardware**; every other kind is
experimental (tested against fakes). Colour: *hs* = hue/saturation, *white* =
colour temperature, each where the device reports it.

**Bluetooth**

| Kind | Library | Colour | What you enter to add one |
|---|---|---|---|
| Ledvance Volkslicht, SIG mesh lights (`mesh`) | btmesh, bleak | hs, white where the light has them | nothing: `hauslicht adopt` |
| Philips Hue Bluetooth (`huebt`) | HueBLE | hs, white | Bluetooth address (search) |
| Triones / LEDBlue BLE (`ledble`) | led-ble | hs | Bluetooth address (search) |

**Network**

| Kind | Library | Colour | What you enter to add one |
|---|---|---|---|
| Philips Hue Bridge (`hue`) | aiohttp | hs, white | bridge host (press its link button first), light |
| Home Assistant (`homeassistant`) | aiohttp | hs, white | URL, access token, light entity |
| MQTT / zigbee2mqtt (`mqtt`) | aiomqtt | hs, white (zigbee2mqtt only) | broker host, device or command topic |
| Matter server (`matter`) | aiohttp | hs, white | server URL, node |
| WiZ (`wiz`) | pywizlight | hs, white | host (search) |
| LIFX (`lifx`) | – (own LAN code) | hs, white | host (search) |
| Yeelight (`yeelight`) | – (own LAN code) | hs, white | host (search; enable LAN control) |
| Govee LAN API (`govee`) | – (own LAN code) | hs, white | host (search; enable LAN control) |
| TP-Link Kasa / Tapo (`kasa`) | python-kasa | hs, white | host (search); Tapo: TP-Link account e-mail and password |
| Tuya / Smart Life (`tuya`) | tinytuya | hs, white | host, device ID, local key |
| Shelly (`shelly`) | aiohttp | hs, white (Gen1 colour bulbs), white (Duo); Gen2+ brightness only | host (search), password if set |
| Tasmota (`tasmota`) | aiohttp | hs, white | host (search), user/password if set |
| ESPHome (`esphome`) | aioesphomeapi | hs, white | host, encryption key |
| WLED (`wled`) | aiohttp, zeroconf | hs; white on CCT/RGBW strips | host (search) |
| DMX fixtures: Art-Net, sACN, OLA, Enttec DMX USB Pro / Open DMX (`dmx`) | sacn, pyserial, aiohttp | by channel layout | output, host or port (search: Art-Net nodes, OLA, Enttec ports), universe, start address, channel layout (e.g. `dimmer,r,g,b,w`) |

**Local**

| Kind | Library | Colour | What you enter to add one |
|---|---|---|---|
| Linux LED class, `/sys/class/leds` (`sysled`) | – (sysfs, logind) | hs on multicolour LEDs | LED name (search; shows its trigger) |
| USB status lights: BlinkStick, Luxafor, blink(1), Kuando, Embrava, MuteMe … (`usblight`) | busylight-core | hs | device (search; access: root setup) |
| Serial LED controllers: Adalight sketches, WLED over USB (`serial`) | pyserial | hs | port (search lists, never opens), protocol, baud, LED count |

**Generic**

| Kind | Library | Colour | What you enter to add one |
|---|---|---|---|
| Generic light: shell-free command, HTTP, MQTT, serial or UDP templates (`generic`) | – (aiohttp, aiomqtt or pyserial by transport) | as the templates define | transport, its target, templates for on, off, brightness, colour, white (placeholders such as `{percent}`, `{level}`, `{r}`) |

White-only bulbs keep brightness only. How each kind works and what it can
do: [backend API](docs/backend-api.md),
[research notes](docs/backends-research.md).

## Install

Needs Linux with BlueZ, Python 3.12+, and for the widget and slider KDE
Plasma 6 with UPower.

```sh
git clone https://github.com/Gelaende51/Haus.Licht && cd Haus.Licht
sudo install/setup-root.sh             # once: keyboard slider, device access, Bluetooth tuning
install/install.sh                     # venv, commands, man pages, service, tray widget (mesh only)
install/install.sh --backend wiz,hue   # plus the libraries of these kinds
install/install.sh --all-backends      # plus every kind's libraries
systemctl --user enable --now hauslicht
```

- `install/install.sh --dry-run …` prints what would be installed;
  `hauslicht backends --requires KIND` lists a kind's pip packages.
- Clap to toggle and Follow audio: `.venv/bin/pip install -r
  requirements-clap.txt` (numpy, ai-edge-litert; the 4 MB sound model is
  fetched on first clap use). Both record with `pw-record` or `parec`.
- Follow video: GStreamer with its PipeWire plugin (`gst-launch-1.0`,
  `pipewiresrc`) and xdg-desktop-portal.
- Tests: `pip install -r requirements-dev.txt` (see the note on amqtt there);
  how CI runs them, also locally: [docs/ci.md](docs/ci.md).

## Adding bulbs

- **Most kinds:** widget → *Configure* → *Bulbs* → *Add a bulb*: choose the
  kind, *Search network* (or *Scan Bluetooth*, *Scan everything*) or enter the
  fields from the table, *Add*. From a terminal: `hauslicht discover [KIND]`,
  `hauslicht add KIND key=value…`.
- **Ledvance Volkslicht (mesh):** factory-reset the bulb (switch it on and off
  five times, 5 s each, then leave it on; it flashes). Within 8 minutes run
  `hauslicht adopt`. The bulb then leaves the Ledvance app. `hauslicht.json`
  holds the mesh keys: back it up, without it the bulbs can only be reset.

## Works with

- KDE Plasma 6 (brightness applet, PowerDevil, Night Light, colour scheme,
  global shortcuts, notifications); without Plasma everything but widget and
  slider works.
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

## Third-party

- **YAMNet** sound classifier (`yamnet.tflite` of MediaPipe's audio
  classifier, from tensorflow/models): Copyright Google LLC, Apache License
  2.0 (https://www.apache.org/licenses/LICENSE-2.0). Not shipped here:
  downloaded on first use of *Clap to toggle* from a pinned URL and checked
  against its sha256 (`haus/clap.py`).
- Run by **LiteRT** (`ai-edge-litert`, Apache-2.0) with **NumPy** (BSD-3-Clause).
