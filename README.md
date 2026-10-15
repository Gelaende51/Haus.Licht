# Licht.Haus

**English** · [Deutsch](docs/i18n/README.de.md)

Control Ledvance Volkslicht bulbs (Bluetooth SIG Mesh) from Linux and let them
follow your screen brightness, with a slider in KDE Plasma's brightness applet.
No Ledvance app, account or cloud.

- **AI disclosure**
  - Agent: Claude Code with Claude Opus 5.5 (Anthropic) wrote the code and docs
    and analysed how the official app sets brightness; about 50 M tokens
    processed (mostly cached context), 0.13 M generated.
  - Author: tested on the real bulbs, chose the features and the slider modes.

[License: AGPL-3.0](LICENSE) · [Notes and findings](dev/takeaways.md) ·
[Issues](https://github.com/Gelaende51/Licht.Haus/issues) ·
[Donate (carbon offset for the tokens)](https://ko-fi.com/vault51)

## Features

- **Screen sync:** the bulbs follow the laptop's backlight (`lichthaus sync`,
  a systemd user service).
- **Slider in Plasma's brightness applet:** shown as *Keyboard brightness*
  (laptops without a keyboard backlight have the row free). It has four modes,
  set with `lichthaus mode <mode>`:

  | Mode | The slider sets |
  |---|---|
  | `maximum` (default) | the ceiling: the bulbs follow the screen between 0 and the slider |
  | `minimum` | the floor: the bulbs follow the screen between the slider and 100 % |
  | `independent` | the bulbs directly, ignoring the screen |
  | `coupling` | the curve: above the middle the bulbs react strongly at medium screen brightness and little near the ends; below the middle the reverse |

  `lichthaus curve <mode>` prints the resulting table.
- **Tray widget** (Plasma): left click opens it, middle click switches the
  bulbs on/off. A two-handle **screen window** on a −100…200 % scale (0…100
  is the screen): screen brightness at the left handle means off, at the
  right one full, so −100…200 squeezes the bulbs into 33…67 % and 50…100
  keeps them off until the screen passes 50 %. A **curve** slider (0 linear,
  positive rises slowly for fine low steps). Sub menus for the keyboard
  slider (incl. *off*), following the screen (*always*, *only while Night
  Light is on*, *never*) and **presets** (save, apply, delete).
  `lichthaus-ctl get | set key=value | preset save|apply|delete NAME` does the
  same from scripts.
- **Screen state** and **lid state** (both on by default, separate
  checkboxes; `lichthaus screen-state|lid-state on|off`): while the screen is
  on the bulbs never drop below 0.5 %, even at screen brightness 0; blanked
  screen or closed lid switch them off, and they come back with it. A logind
  delay inhibitor switches them off before the laptop sleeps.
- **Fine dimming:** below 50 % the bulbs take 0.5 % steps, so `dim 0.5` sits
  between off and 1 % (the firmware accepts nothing finer).
- **Commands:** `lichthaus on | off | dim 40 | status [bulb]`, `slider 60`,
  `pause | resume`, `list`. A manual `on`/`off`/`dim` pauses the sync until
  `resume` or the next slider move.
- **Adopting bulbs:** `lichthaus adopt` takes a factory-fresh bulb into the
  laptop's own mesh network.

## Install

Needs Linux with BlueZ, Python 3.12+, and for the slider KDE Plasma 6 with
UPower.

```sh
git clone https://github.com/Gelaende51/Licht.Haus && cd Licht.Haus
sudo install/setup-root.sh   # once: uleds driver + access for the slider
install/install.sh           # venv, `lichthaus` command, service, tray widget
```

**Adopt a bulb:** factory-reset it (switch it on and off five times, 5 s
each, then leave it on; it flashes to confirm). Within 8 minutes run
`lichthaus adopt`, then `systemctl --user enable --now lichthaus`. A bulb
adopted here leaves the Ledvance app. `lichthaus.json` holds the mesh keys:
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
