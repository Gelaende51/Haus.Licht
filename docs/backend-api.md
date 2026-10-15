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
| `fields` | what `add` needs: list of `{key, label, label_de, type, required, default, choices}`; `type` is `text`, `password`, `int`, `bool` or `choice` |

## Methods

`bulb` is the core's bulb object: read `bulb.id`, `bulb.name`, `bulb.config`
(the json `config` dict; change it, then `ctx.save()`).

| Method | Contract |
|---|---|
| `caps(bulb) -> set` | subset of `onoff dim flick powerup ct color` |
| `min_lit(bulb) -> float` | lowest lit percent: 0.5 on the Volkslicht, 1 on most others. Switched on, the core never sends less unless the profile's `off_at_zero` is on |
| `async start()` / `async stop()` | once the first bulb of the kind exists / at shutdown (after the core set OnPowerUp back to "on") |
| `async apply(bulb, percent)` | show percent, 0 = off. Called on every screen-brightness change; the core coalesces and never calls it concurrently within one backend, so rate-limit inside if the device needs it. Raise on failure (the core retries after 5 s, doubling up to 300 s); raise `Offline` when the device is known gone (no back-off; the core waits until `status` says online) |
| `async set_powerup(bulb, "off" \| "on" \| "restore") -> bool` | what the bulb does when it gets power; True once the device has it. Called only on change and after each reconnect; may be a flash write in the device |
| `status(bulb) -> dict` | `{connected, online}` (`online` None = not known yet); extra keys are kept (mesh adds `advert_age`). Must be cheap: the core reads it every round |
| `async discover(timeout) -> list` | `[{kind, address, name, config}]` of devices that could be added |
| `async add(config) -> config` | validate or pair; raise `UserError` with a message for the user. Set `config["id"]` to a stable device-derived suffix if there is one, else the core numbers the bulb |

Optional hooks (no-ops in the base class): `async remove(bulb)`,
`async suspend()` (after the core switched and re-armed the bulbs before
sleep), `async resume()` (after wake: links did not survive),
`async query(bulb) -> {on, percent}` (the `status` command).

## Callbacks: `self.ctx`

| Call | Use |
|---|---|
| `ctx.flick(bulb_id)` | the device saw a power cycle it is sure was a wall-switch flick. Flick mode on: the core toggles that bulb; off: switches it on |
| `ctx.power_returned(bulb_id, gap_s)` | the device came back after an outage `gap_s` seconds long. Under 15 s: a flick (as above); longer: on. `None`: first contact since the daemon started, power stays as saved |
| `ctx.changed()` | connected/online changed: the core re-reads `status` (a new connection re-arms OnPowerUp; a return re-sends the level) |
| `ctx.state`, `ctx.state_path`, `ctx.save()`, `ctx.bulbs()` | the whole hauslicht.json dict (a backend may keep backend-wide data under a top-level key of its own; mesh keeps btmesh's keys there), and this kind's bulbs |

## What the core does with it

Each bulb's want comes from its own profile, power and pause state, floored
by `min_lit`. With global `flick` on, the core keeps OnPowerUp of every bulb
with `powerup` armed to its flick target ("on" while a toggle would light it,
"off" while it would leave it dark); with `flick` off, every such bulb gets
"on" once. Before sleep and at shutdown: "on". See `haus/daemon.py`; the
mock backend (`haus/backends/mock.py`) is the smallest complete example.
