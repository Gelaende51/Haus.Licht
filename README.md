# Haus.Licht

**English** · [Deutsch](docs/i18n/README.de.md)

Control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux and let them
follow your screen brightness, with a slider in KDE Plasma's brightness applet.
No Ledvance app, account or cloud.

Forked from [Licht.Haus](https://github.com/Gelaende51/Licht.Haus).

- **AI disclosure**
  - Agent: Claude Code with Claude Opus 5.5 (Anthropic) wrote the code and docs
    and analysed how the official app sets brightness; about 50 M tokens
    processed (mostly cached context), 0.13 M generated.
  - Author: tested on the real bulbs, chose the features and the slider modes.

[License: AGPL-3.0](LICENSE) · [Notes and findings](dev/takeaways.md) ·
[Issues](https://github.com/Gelaende51/Haus.Licht/issues) ·
[Donate (carbon offset for the tokens)](https://ko-fi.com/vault51)

## Features

- **Screen sync:** the bulbs follow the laptop's backlight (`hauslicht sync`,
  a systemd user service).
- **Slider in Plasma's brightness applet:** shown as *Keyboard brightness*
  (laptops without a keyboard backlight have the row free). It has four modes,
  set with `hauslicht mode <mode>`:

  | Mode | The slider sets |
  |---|---|
  | `maximum` (default) | the ceiling: the bulbs follow the screen between 0 and the slider |
  | `minimum` | the floor: the bulbs follow the screen between the slider and 100 % |
  | `independent` | the bulbs directly, ignoring the screen |
  | `coupling` | the curve: above the middle the bulbs react strongly at medium screen brightness and little near the ends; below the middle the reverse |

- **Tray widget** (Plasma): left click opens it, middle click switches the
  bulbs on/off. A two-handle **screen window** on a −100…200 % scale (0…100
  is the screen): screen brightness at the left handle means off, at the
  right one full, so −100…200 squeezes the bulbs into 33…67 % and 50…100
  keeps them off until the screen passes 50 %. A **curve** slider (0 linear,
  positive rises slowly for fine low steps). Sub menus for the keyboard
  slider (incl. *off*), following the screen (*always*, at *night* or by
  *day* per Night Light, with a *dark* or *light* colour scheme, *never*) and
  **profiles** (save, apply, delete).
  `hauslicht-ctl get | set key=value | profile save|apply|rename|delete NAME`
  does the same from scripts ([protocol](docs/ctl-protocol.md)).
- **Several bulbs, profiles:** each bulb has its own power (kept across
  restarts) and a profile (screen window, curve, level, follow-screen rule).
  `hauslicht select ID|all` picks what the slider, scroll and on/off act on;
  `all` works on the profile `default`. Kinds of bulbs are backends, one
  module each ([backend API](docs/backend-api.md)); `hauslicht backends`
  lists them, `discover`, `add KIND key=value…` and `remove ID` manage bulbs.
- **Keyboard slider as a remote:** the mode picks which widget control the
  brightness applet's *Keyboard brightness* slider moves: right window handle
  (`maximum`, default), left handle (`minimum`), curve (`coupling`),
  brightness (`independent`, screen ignored) or nothing (`off`). Keyboard up
  always means brighter bulbs, so handles and curve move left as it rises.
- **Brightness slider** in the widget shows what the bulbs show; it sets the
  level when the screen is ignored, otherwise it is a manual setting until
  *Resume*. The header shows whether the bulb is connected.
- **Wall switch:** flicking the light off and on toggles the bulbs; a longer
  outage (over ~20 s) switches them on. A flick has to be long enough to
  reset the bulb (its supply bridges short gaps); the daemon then notices
  the bulb's first advert off its 10 s rhythm and reconnects at once.
  The daemon also keeps the bulb's power-up behaviour (Generic OnPowerUp)
  set to the toggled state (dark while lit, on while dark), so after a
  flick the bulb boots straight into it; while the daemon is stopped or the
  laptop sleeps, the bulb comes on with the wall switch as usual.
  `hauslicht flick off` turns flick mode off: the wall switch then just
  switches the bulbs on, as with any bulb.
  `install/setup-root.sh` adds a tiny root helper that shortens the BLE link
  timeout to 1 s, so the toggle lands about 1.5-5 s after the flick.
- **Turn off at 0 %** (off by default; `hauslicht off-at-zero on|off`):
  while switched on the bulbs never drop below 0.5 %, in every mode, even at
  screen brightness 0 or level 0; turned on, a computed 0 % leaves them dark.
  A blanked or switched-off screen does not touch the bulbs: they keep the
  brightness last read while it was on.
- **Lid** (`hauslicht lid close|open|both|never`, default `both`): closing
  the lid switches the bulbs off, opening switches them on, like the middle
  click; in between on/off work as usual. With `close` or `both` a logind
  delay inhibitor also switches them off before the laptop sleeps.
- **Fine dimming:** below 50 % the bulbs take 0.5 % steps, so `dim 0.5` sits
  between off and 1 % (the firmware accepts nothing finer).
- **Commands:** `hauslicht on | off | dim 40 | status [bulb]`, `slider 60`,
  `nudge 5 | nudge -5` (scroll on the tray icon: moves the slider, in mode
  `off` the brightness), `pause | resume`, `list`. A manual `on`/`off`/`dim` pauses the sync until
  `resume` or the next slider move.
- **Adopting bulbs:** `hauslicht adopt` takes a factory-fresh bulb into the
  laptop's own mesh network.

## Install

Needs Linux with BlueZ, Python 3.12+, and for the slider KDE Plasma 6 with
UPower.

```sh
git clone https://github.com/Gelaende51/Haus.Licht && cd Haus.Licht
sudo install/setup-root.sh   # once: uleds driver + access for the slider
install/install.sh           # venv, `hauslicht` command, service, tray widget
```

**Adopt a bulb:** factory-reset it (switch it on and off five times, 5 s
each, then leave it on; it flashes to confirm). Within 8 minutes run
`hauslicht adopt`, then `systemctl --user enable --now hauslicht`. A bulb
adopted here leaves the Ledvance app. `hauslicht.json` holds the mesh keys:
back it up, without it the bulbs can only be reset.

## Works with

- KDE Plasma 6 (brightness applet, PowerDevil); without Plasma everything but
  the slider works.
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
