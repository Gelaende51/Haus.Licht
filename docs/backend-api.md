# Backend API

One module per kind of bulb in `haus/backends/`. The registry
(`haus/backends/__init__.py`) imports every module there on first use and
picks up each subclass of `Backend` that sets `kind`: adding a kind needs no
edit anywhere else. A module that fails to import is listed as unavailable,
so import optional libraries inside methods (or guard the top-level import,
as `mesh.py` does). `HAUSLICHT_BACKENDS=mock,mesh` limits which kinds load.

## Class attributes

| Attribute | Meaning |
|---|---|
| `kind` | id prefix: bulbs are `<kind>:<suffix>`, e.g. `mesh:0002` |
| `title` | shown when adding, e.g. "Bluetooth mesh (Ledvance Volkslicht, SIG mesh lights)" |
| `experimental` | shown as such in the widget |
| `requires` | pip names, e.g. `("aiohue",)`; missing ones are reported in `backends` |
| `modules` | import names of `requires` when they differ from the pip name (default: lower case, `-` → `_`, extras dropped) |
| `product` | short product name for default bulb names `<product> - <ID>` (default: `title` up to ` (`, e.g. "Mock bulb") |
| `bluetooth` | default False: True when devices are found over Bluetooth (mesh, huebt, ledble); `discover bluetooth` runs these kinds, and `backends` lists the flag |
| `autoconnect` | default True: devices from `discover` may be added by the daemon's autoconnect (`settings.autoconnect`). Mesh sets False: adopting provisions the bulb, so only by hand |
| `fields` | what `add` needs: list of `{key, label, label_de, type, required, default, choices, when, placeholder}`; `type` is `text`, `password`, `int`, `bool` or `choice`. Optional `when: {"<other key>": [values]}`: the Add form shows the field only while the other field's value (or its default) is in the list; a hidden field is neither required nor sent, and `check_fields` drops it (`field_shown` in `haus/backends/__init__.py`). Optional `placeholder`: the text field's placeholder text (e.g. the template placeholders). `Info.describe` passes both through |

## Methods

`bulb` is the core's bulb object: read `bulb.id`, `bulb.name`, `bulb.config`
(the json `config` dict; change it, then `ctx.save()`).

| Method | Contract |
|---|---|
| `caps(bulb) -> set` | subset of `onoff dim flick powerup ct hs` (`ct`: white colour temperature, `hs`: hue/saturation colour) |
| `min_lit(bulb) -> float` | lowest lit percent: 0.5 on the Volkslicht, 1 on most others. Switched on, the core never sends less unless the profile's `off_at_zero` is on |
| `step(bulb) -> float` | the device's native brightness step in percent, from its protocol: 1 for a whole-percent level (Ledvance mesh with scale 100, WiZ, Yeelight, Kasa, Tasmota, Govee, Shelly), 100/254 (Hue, Hue Bluetooth, Matter, zigbee2mqtt), 100/255 (Home Assistant, Bluetooth LED, Tuya "A", WLED, DMX), 0.1 (Tuya "B", 10–1000), 100/65535 (LIFX, spec mesh lightness), MQTT generic `{value}`: 100/scale. Unknown (ESPHome's float, MQTT `{percent}`): 1, the default. Exposed per bulb as `step` in `get` / `watch`, for the widget's slider. The mesh Volkslicht's 0.5 % is only its glow floor (`min_lit`), not a step |
| `product_name(config) -> str` | the name before ` - <ID>` of a new bulb with this config; default `product`. Mesh: "Hauslicht" for a Ledvance bulb (lightness scale 100, CID 0x052e), else "Mesh light" |
| `async start()` / `async stop()` | once the first bulb of the kind exists / at shutdown (after the core set OnPowerUp back to "on") |
| `async apply(bulb, percent)` | show percent, 0 = off. Called on every screen-brightness change; the core coalesces and never calls it concurrently within one backend, so rate-limit inside if the device needs it. Raise on failure (the core retries after 5 s, doubling up to 300 s); raise `Offline` when the device is known gone (no back-off; the core waits until `status` says online) |
| `async set_powerup(bulb, "off" \| "on" \| "restore") -> bool` | what the bulb does when it gets power; True once the device has it. Called only on change and after each reconnect; may be a flash write in the device |
| `status(bulb) -> dict` | `{connected, online}` (`online` None = not known yet); extra keys are kept (mesh adds `advert_age`). Must be cheap: the core reads it every round |
| `async discover(timeout) -> list` | `[{kind, address, name, config}]` of devices that could be added. Autoconnect adds them with `config` as given and remembers `address` (a stable one, a MAC or an id, keeps the ignore list working) |
| `async add(config) -> config` | validate or pair; raise `UserError` with a message for the user. Set `config["id"]` to a stable device-derived suffix if there is one, else the core numbers the bulb |
| `NeedsAccess(message, found)` | raised by `discover` / `add` when a device node is there but the user may not open it (`haus/backends/__init__.py`); `discover`'s `found` entries still count. The core marks it: `access` (the kinds) in the `discover` reply, `"access": true` beside an `add` error, and the widget offers "Grant access…" (the root setup's udev rule, v5) |

Colour (only for bulbs with the `ct` or `hs` cap):

| Method | Contract |
|---|---|
| `async fade(bulb, percent, seconds) -> bool` | optional: go to `percent` over `seconds` with the device's own transition, in one message (the core's `test`). False: no transition, the core steps with `apply` instead. Raise like `apply`. Default: False. The mesh backend sends one Lightness / Level Set with that Transition Time |
| `ct_range(bulb) -> (min_k, max_k)` | the white range of a `ct` bulb; default `(2700, 6500)` |
| `async apply_color(bulb, color) -> bool` | show `{"mode": "hs", "hue": 0–360, "sat": 0–100}` or `{"mode": "ct", "kelvin": int}` (within `ct_range`). True once the device has it; False: the core retries after 60 s. Raise like `apply`. Default: no-op, False |

Optional hooks (no-ops in the base class): `async remove(bulb)`,
`async suspend()` (after the core switched and re-armed the bulbs before
sleep), `async resume()` (after wake: links did not survive),
`async query(bulb) -> {on, percent}` (the `status` command),
`async idle(idle)` (below).

`idle(True)`: every bulb of the kind is idle (its use is off: the core sends
it nothing, and its OnPowerUp "on" is confirmed, or it is known to be without
power). The backend may drop its links and stop scanning or polling; the
core calls it once, after the last arming. `idle(False)`: a bulb is driven
again or needs arming; the core calls it before the next `apply` /
`set_powerup`, which may then connect again. Repeated calls with the same
value can be ignored. Mesh closes its proxy link and stops its Bluetooth
scanner (no reconnects, no flick watch; `apply`, `set_powerup` and `query`
raise `Offline` until `idle(False)`), and starts the scanner again on
`idle(False)`.

## Callbacks: `self.ctx`

| Call | Use |
|---|---|
| `ctx.flick(bulb_id)` | the device saw a power cycle it is sure was a wall-switch flick. Flick mode on: the core toggles that bulb; off: switches it on |
| `ctx.power_returned(bulb_id, gap_s)` | the device came back after an outage `gap_s` seconds long (drop -> back as measured). Up to `QUICK_GAP_S` (20 s, `haus/backends/__init__.py`, derivation there): a flick (as above, a toggle of the power before the outage); longer: a power-off, back on whatever it was. `None`: first contact since the daemon started, power stays as saved |
| `ctx.changed()` | connected/online changed: the core re-reads `status` (a new connection re-arms OnPowerUp; a return re-sends the level) |
| `ctx.state`, `ctx.state_path`, `ctx.save()`, `ctx.bulbs()` | the whole hauslicht.json dict (a backend may keep backend-wide data under a top-level key of its own; mesh keeps btmesh's keys there), and this kind's bulbs |

## What the core does with it

Colour: the core sends `apply_color` after `apply`, only while the bulb is
lit (powered and above 0 %), and only when the wanted colour moved (hue or
saturation by 1, Kelvin by 25) since the last one the bulb took; dark, idle
(`use` off) or unreachable bulbs get none, and the colour is sent again on
switching on, after a reconnect, a return and a resume. What it wants comes
from the bulb's profile (`haus/profiles.py`, `color_want`): with `follow_ct`
on and KWin Night Light readable, the screen's white (`currentTemperature`)
shifted from 6500 K in mired and scaled by `shift` (100 = the same shift, 0
none, 200 double), clamped to `ct_range`; a `ct` bulb gets it as `ct`, an
`hs`-only bulb as the hue/saturation of that blackbody colour (1000–6500 K,
after Tanner Helland's RGB fit, converted to HSV). Otherwise an `hs` bulb
gets the profile's `hue`/`sat`, and a `ct`-only bulb's white is left alone.

Each bulb's want comes from its own profile, power and pause state, floored
by `min_lit`. With global `flick` on, the core keeps OnPowerUp of every bulb
with `powerup` armed to its flick target ("on" while a toggle would light it,
"off" while it would leave it dark); with `flick` off, every such bulb gets
"on" once. Before sleep and at shutdown: "on". A bulb armed to anything
else stays listed in `hauslicht.armed.json` until an "on" is confirmed; after
an unclean exit the unit's `ExecStopPost` (`hauslicht disarm --if-armed`,
`haus/disarm.py`) starts the backend on its own and calls `set_powerup(bulb,
"on")` for those bulbs (mesh: the one-shot link). See `haus/daemon.py`; the
mock backend (`haus/backends/mock.py`) is the smallest complete example.

## Directly attached lights

- `sysled`: Linux LED class (`/sys/class/leds`, `SYSFS` in the module). Writes
  `brightness` when the user may, else logind's `Session.SetBrightness("leds", …)`
  on the user's graphical session; `max_brightness` 1 is `onoff` only (step 100);
  multicolor gets `hs` only with a writable `multi_intensity`. Never the daemon's
  own `hauslicht::` uleds LED.
- `usblight`: HID status lights through busylight-core; brightness scales the
  colour (white by default); id from the serial, else vid:pid:path; unplugged is
  `Offline`, a probe re-opens it. Kuando's keepalive is the library's.
- `serial`: `adalight` frames (repeated every second while driven) or WLED JSON
  lines; the port by its `/dev/serial/by-id/` name; `discover` never opens a port
  (DTR resets an Arduino), `add` and the bulb do, and keep it open.

None of them autoconnects, has `powerup` or `flick`, or switches anything at
daemon stop.

## Network lights: colour

Caps follow what each device reports, stored in the bulb's config at add
(`color`: the caps, `kelvin`: the white range) by `_bulbkit.learn_color`; a
config from before has brightness only until the backend learns them once
(first contact or poll). A colour never carries power or a new level, and
each backend sends it only while it last lit the bulb.

| Kind | Caps from | Range | Sent as |
|---|---|---|---|
| `wiz` | pywizlight bulb type (RGB: hs+ct, TW: ct, DW: none) | the bulb's cctRange / whiteRange | setPilot, PilotBuilder r/g/b(/w) or `temp`, with the current `dimming` |
| `lifx` | GetVersion product id in products.json (table in `lifx.py`) | the product's | SetColor with the bulb's brightness |
| `yeelight` | discovery `support` (set_hsv, set_ct_abx), else get_prop `ct`/`hue` "" | 1700-6500 (colour), 2700-6500 (white) | set_hsv / set_ct_abx (music mode when on) |
| `kasa` | Light module features `hsv`, `color_temp` | the `color_temp` feature's | set_hsv / set_color_temp (IOT: with the level, as python-kasa switches on) |
| `hue` | light resource `color`, `color_temperature` | mirek_schema | CLIP v2 `color.xy` (from hue/sat) / `color_temperature.mirek` |
| `shelly` | Gen1 /status light `mode` (colour bulbs: hs+ct), `temp` (Duo: ct); Gen2+: none | 3000-6500 / 2700-6500 | `/light/N?mode=color&red&green&blue&white=0&gain` / `mode=white&temp&brightness` (Duo: `temp`) |
| `tasmota` | Status 11 `HSBColor`, `CT` | 153-500 mired | `HSBColor h,s,<level>` / `CT <mired>` |
| `govee` | devStatus `color`, `colorTemInKelvin` | 2000-9000 | `colorwc`, checked with devStatus |

## WLED and DMX

- `wled`: WLED's JSON API over HTTP (`/json/info`, `/json/state`; `tt` for
  `fade`), found by mDNS `_wled._tcp`, id the MAC; `hs` with RGB, `ct` only
  with a CCT or RGBW strip; unreachable is `Offline`; autoconnects.
- `dmx`: one bulb per fixture (output, universe, start address, channel
  layout); fixtures on one output and universe share one frame and sender;
  Art-Net / sACN resent every second until `idle(True)`; serial `discover`
  never opens a port; no autoconnect.

Neither has `powerup` or `flick`, nor sends anything at daemon stop.

## Generic lights

- `generic`: a light defined entirely in the Add dialog. `transport` is
  `command`, `http`, `mqtt`, `serial` or `udp` (the transport's own fields use
  `when`); the templates `on`, `off`, `brightness`, `colour` and `white` are
  that transport's message for each action, filled by `str.format_map` with
  numbers only: `{percent}` 0–100, `{level}` 0–255, `{hue}` 0–360, `{sat}`
  0–100, `{kelvin}`, `{r} {g} {b}` 0–255 (the colour at full brightness),
  `{r_dim} {g_dim} {b_dim}` (scaled by brightness), `{on}` 1/0; `{{ }}` are
  literal braces. `add` rejects unknown placeholders and malformed templates.
- Caps follow the templates: `dim` with brightness, `hs` with colour, `ct`
  with white (range: `min_kelvin`/`max_kelvin`); `onoff` always (an on or a
  brightness template is required). An empty off template is brightness 0.
  On a switch from dark to lit, the on template goes first, then brightness.
- `command`: the template is split once (`shlex`), each argument filled, and
  run without a shell (`create_subprocess_exec`), 5 s timeout; a non-zero exit
  raises with stderr (~200 characters). `http`: GET/POST/PUT, a URL template,
  `Name: value` headers (several separated by `\n`); POST/PUT send the
  template as the body, GET appends it to the URL; 5 s timeout, non-2xx
  raises. `mqtt`: broker, topic template, the template is the payload;
  `mqtt.BrokerLink`, one connection per broker. `serial`: `serial.py`'s port
  helpers, the template is a line with `\n \r \t \xNN` escapes; the port opens
  on first use and stays open; a vanished port is `Offline`. `udp`: host,
  port, payload with the same escapes.
- No discover, no autoconnect, no `powerup` or `flick`; unreachable raises
  (the core retries). `details` shows the transport and the target.
