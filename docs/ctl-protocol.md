# ctl protocol

The daemon (`hauslicht sync`) listens on a Unix socket:
`$HAUSLICHT_SOCK`, else `$XDG_RUNTIME_DIR/hauslicht.sock`. One request per
connection: a JSON line in, a JSON line out (`watch` keeps the connection
open, see below).

```
{"cmd": "set", "arg": "range_hi=150", "args": ["range_hi=150"], "which": null}
→ {"results": [ {…} ]}          or          {"error": "message"}
```

`args` is the argument list (names with spaces stay whole); without it the
daemon splits `arg` at spaces. `which` (a bulb id, a mesh unicast like
`0x0002`, or `all`) overrides the selection for `on off toggle pause resume
dim status raw`.

**Widgets.** There are `widgets` widgets (1–8, `set widgets=N`). Each has
its own bulb list (`widget-bulbs`) and its own selection. A command may
start with `@N` (`{"cmd": "@2", "args": ["select", "mock:1"]}`,
`hauslicht-ctl @2 select mock:1`); without it, widget 1. "The selection"
below means widget N's: `all` = the used bulbs in widget N (profile
`default`), or one bulb of it. The keyboard slider (LED, `slider`, the
`mode` binding) belongs to widget 1. Widget lists only choose what the
controls act on: a used bulb in no widget is still driven by its profile. `hauslicht-ctl CMD ARGS…` sends exactly this and prints
`results[0]` as one JSON line (exit 1 and `{"error": …}` on refusal or when
no daemon answers). Error messages are meant for the user (German when the
daemon runs in a German locale).

## Commands

Most reply with the `get` object; the exceptions are noted.

| Command | Arguments | Does |
|---|---|---|
| `get` | – | state, see below |
| `on` / `off` / `toggle` | – | power of the selection (`all`: the widget's used bulbs; toggle: any on → all off, else all on). Power is saved |
| `pause` / `resume` | – | stop / restart following for the selection |
| `bulb` | `N` 0–100 | the widget's brightness slider: sets the profile's `level` while the screen is ignored (mode `independent` or not tracking), else a manual level for the selection until `resume` |
| `dim` | `N` | manual level, sent as it is (0 = off, no lowest-lit floor); replies `[{bulb, percent}]` once sent |
| `slider` | `N` 0–100 | the keyboard slider (moves Plasma's slider when the LED exists) |
| `nudge` | `±N` | the slider by N; in mode `off` the brightness of the selection. `@N nudge` (N ≠ 1) moves the same mode-bound profile value of widget N's selection by N slider steps, without the LED |
| `set` | `KEY=VALUE` | globals `mode` (`independent minimum maximum coupling off`), `lid` (`close open both never`), `flick` (`on off`), `widgets` (1–8; lowering drops the higher widgets' lists and selections); profile keys of the selection's profile: `track` (`always night day dark light never`), `off_at_zero` (`on off`), `range_lo` / `range_hi` (−100…200, never crossing), `curve` (−100…100), `level` (0–100) |
| `mode` / `track` / `off-at-zero` / `lid` / `flick` | `[VALUE]` | the same as `set`, value optional |
| `select` | `ID` \| `all` | what the slider, nudge, `set` (profile keys) and on/off act on (a used bulb of the widget); `all` uses the profile `default` |
| `widget-bulbs` | `[ID,ID…]` | the widget's bulbs (none: empty), stored in list order. Unused bulbs the widget had stay stored (they reappear with `use=on`) |
| `order` | `ID ID …` | the list order: every bulb id once. `get` lists bulbs and widget lists in it |
| `bulb-set` | `ID key=value…` | `name=`, `profile=`, `use=on\|off`, `icon_on=` / `icon_off=` (an icon name or an absolute path; empty: none). `use=off` makes the bulb idle: OnPowerUp is set to `on` (a power cut boots it lit; retried until confirmed), then nothing else is sent to it (no levels, power, flick or lid handling); it leaves every widget's list and `all` in `get` but stays stored. A backend whose bulbs are all idle is told it may drop its link |
| `profile save` | `NAME` | copy the selection's profile values into NAME (new or overwritten) |
| `profile apply` | `NAME` | one bulb selected: it uses NAME. `all`: NAME's values go into `default` and every bulb of the widget uses `default` |
| `profile rename` | `OLD NEW` | bulbs follow; `default` can't be renamed |
| `profile delete` | `NAME` | bulbs using it fall back to `default`; `default` can't be deleted |
| `preset …` | as `profile` | alias kept for the old widget |
| `discover` | `[KIND]` | replies `{"discovered": [{kind, address, name, config}]}` after ~5 s |
| `add` | `KIND key=value…` | `name=` names the bulb, the rest is checked against the backend's `fields`; a new bulb starts switched on, in use, at the end of the list and in widget 1. Replies the `get` object plus `"added": ID` |
| `remove` | `ID` | stop controlling it; it leaves every widget (a selected one: selection back to `all`) |
| `watch` | – | keeps the connection open and streams events, see below |
| `backends` | – | replies `{"backends": [...]}` (as in `get`) |
| `status` | – | replies `[{bulb, on, percent}]` as the devices report it |
| `raw` | `HEX[,HEX…]` | mesh only: send access payloads, reply `[{bulb, sent, replies}]` |
| `mock-flick` | `ID` | mock bulb: as if a wall-switch flick was seen |
| `mock-return` | `ID GAP` | mock bulb: back after an outage of GAP s (`none`: first contact) |
| `mock-offline` | `ID` | mock bulb: unreachable until `mock-return` |

## `get`

`get` and `@N get` describe widget N. The top-level fields describe its
selection: for `all`, `power` = any on,
`bulbs` = the highest output, `connected` = all connected, `online` = false
if any is offline, null if any is not known yet, else true; the profile
values are those of `default`.

```json
{
  "bulb": "sync",
  "following": true,            // no selected bulb paused
  "mode": "maximum",            // what the keyboard slider moves
  "slider": 0,                  // keyboard slider 0-100
  "track": "always", "off_at_zero": false, "range_lo": 0.0, "range_hi": 200.0,
  "curve": -26.0, "level": 50.0, // the selection's profile
  "lid": "both",
  "night": true, "day": false,  // KWin Night Light: daylight false / true
  "color_scheme": "dark",       // portal color-scheme: dark | light | default | null
  "tracking": true,             // the profile's track holds now
  "screen_on": false, "lid_closed": false,
  "power": false,
  "bulbs": 0.0,                 // output percent, null until sent
  "connected": true, "online": true,
  "onpowerup": 1,               // 0 off, 1 on, 2 restore; null if mixed or unknown
  "advert_age": 3.1,            // mesh: seconds since the proxy's last advert
  "kbd_binds": "range_hi",      // profile key the slider moves, null in mode off
  "screen": 37.0,               // screen brightness percent
  "presets": ["evening"],       // profiles other than default (the old widget's list)
  "selected": "all",            // this widget's: "all" or a bulb id
  "flick": true,
  "profile": "default",         // the profile the selection uses
  "widget": 1,                  // the widget described (@N)
  "widgets": 2,                 // how many widgets
  "in_widget": ["mesh:0002"],   // this widget's used bulbs, in list order
  "bulbs_list": [               // every bulb, used or not, in list order
    {"id": "mesh:0002", "name": "Volkslicht", "kind": "mesh", "profile": "default",
     "power": false, "output": 0.0, "connected": true, "online": true,
     "caps": ["dim", "flick", "onoff", "powerup"], "experimental": false,
     "following": true, "powerup": "on",
     "use": true, "icon_on": "", "icon_off": ""}  // icon: name or absolute path, "" none
  ],
  "profiles": ["default", "evening"],
  "backends": [
    {"kind": "mesh", "title": "Bluetooth mesh (Ledvance Volkslicht, SIG mesh lights)",
     "experimental": false, "available": true, "missing": [], "fields": []},
    {"kind": "mock", "title": "Mock bulb (testing)", "experimental": true,
     "available": true, "missing": [],
     "fields": [{"key": "min_lit", "label": "Lowest lit %", "label_de": "Niedrigste Helligkeit %",
                 "type": "choice", "required": false, "default": "1", "choices": ["0.5", "1"]},
                {"key": "address", "label": "Address", "label_de": "Adresse", "type": "text",
                 "required": false, "default": ""},
                {"key": "fail", "label": "Refuse to be added (tests the error path)",
                 "label_de": "Hinzufügen verweigern (testet den Fehlerfall)", "type": "bool",
                 "required": false, "default": false}]}
  ]
}
```

(Comments for reading only: the reply is plain JSON.)

## `watch`

`{"cmd": "watch"}` (or `@N watch`; `hauslicht-ctl watch` prints the lines)
keeps the connection open. The first line is the `get` object of widget N
plus each bulb's intended state:

```json
{"event": "snapshot", "t": 1760600000.12, …get…,
 "intended": {"mock:1": {"power": true, "percent": 50.0, "color": null}}}
```

Then one line per change, as it happens (no polling on the client side):

```json
{"event": "intended", "t": 1760600001.5, "bulb": "mock:1", "power": true, "percent": 62.0, "color": null}
{"event": "flick", "t": …, "bulb": "mock:1", "power": false}
{"event": "dropout", "t": …, "bulb": "mock:1", "power_before": true}
{"event": "returned", "t": …, "bulb": "mock:1", "gap": 40.0, "power": true}
{"event": "connected", "t": …, "bulb": "mock:1"}
{"event": "disconnected", "t": …, "bulb": "mock:1"}
```

`t` is Unix time (float). `intended` comes whenever the core's intended
state of a bulb changes, before it is sent: `percent` is the computed level
(while paused without a level: the last one sent; `null` for an idle bulb,
`use=off`), `color` is reserved (`null`). Device events come the moment the
core learns of them, before anything is sent to the bulb; for an idle bulb
`flick` and `returned` carry `"idle": true` instead of `power` (nothing is
done). Any number of watchers may connect; one that falls 1000 lines behind
is disconnected, so a stuck client never holds the daemon up.

## A test instance beside the live one

Mock bulbs only, its own state file and socket, nothing written to the
desktop (no keyboard-slider LED, no PowerDevil restart, no suspend
inhibitor) and no Bluetooth (`HAUSLICHT_BACKENDS=mock`). The mock backend is
hidden (not in `backends`, not in the Configure dialog) unless
`HAUSLICHT_BACKENDS` names `mock` or `HAUSLICHT_MOCK=1` is set, e.g.
`HAUSLICHT_BACKENDS=mock,wiz` to try a real kind beside mock bulbs:

```sh
export HAUSLICHT_STATE=/tmp/hl-test.json HAUSLICHT_SOCK=$XDG_RUNTIME_DIR/hl-test.sock
HAUSLICHT_BACKENDS=mock ~/building_stuff/Haus.Licht/hauslicht sync --no-desktop-writes &
hauslicht-ctl add mock name=Desk min_lit=0.5
hauslicht-ctl add mock name=Shelf address=AA:BB    # id mock:aabb
hauslicht-ctl select mock:1 ; hauslicht-ctl on ; hauslicht-ctl get
hauslicht-ctl mock-flick mock:aabb ; hauslicht-ctl mock-return mock:1 40
hauslicht-ctl set widgets=2 ; hauslicht-ctl @2 widget-bulbs mock:aabb ; hauslicht-ctl @2 get
hauslicht-ctl watch          # Ctrl+C to stop
kill %1
```

Keep the socket path short (Unix sockets allow ~107 bytes). With
`HAUSLICHT_SOCK` exported, `hauslicht-ctl` in that shell talks to the test
instance (the installed widget keeps talking to the live one); the state file
is created when missing. Lid events and the
screen brightness are the real ones (read only).
