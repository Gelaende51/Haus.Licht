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
| `set` | `KEY=VALUE` | globals `mode` (`independent minimum maximum coupling off`), `lid` (`close open both never`; laptops), `monitor_power` (`on off`, default on; desktops only, see *Follow monitor power* below), `flick` (`off on fast exclusive`; `fast`/`exclusive` are refused while the root setup is missing or outdated: the widget runs `pkexec install/setup-root.sh` itself first, the CLI `hauslicht setup-root`), `exclusive_adapter` (the adapter flick mode `exclusive` takes from BlueZ: `hciN`, its address, or `auto`/empty: a USB adapter if there is one, else the built-in one; taken again within a second), `widgets` (1–8; lowering drops the higher widgets' lists and selections); profile keys of the selection's profile: `track` (`always night day dark light never`), `off_at_zero` (`on off`), `range_lo` / `range_hi` (−100…200, never crossing), `curve` (−100…100), `level` (0–100), colour: `hue` (0–360, default 30), `sat` (0–100, default 0 = white), `follow_ct` (`on off`: the bulbs follow Night Light's screen white), `shift` (0–200, default 100: 100 the same red shift as the screen, 0 none, 200 double); `autoconnect` (`on off`, default off, see below); `clap` (the widget's mode, `@N` or widget 1: `off clap snap both`, default off; refused while the extra is missing), `clap_source` (a source name from `clap-sources`; `default` or empty: the default source), `clap_sensitivity` (0–100, default 50), see *Clap to toggle* below |
| `mode` / `track` / `off-at-zero` / `lid` / `monitor-power` / `flick` / `autoconnect` / `clap` / `clap-source` / `clap-sensitivity` | `[VALUE]` | the same as `set`, value optional |
| `audio` | `on\|off\|toggle` (`which`) | the selection (or `which`) follows audio instead of the screen, see *Follow audio*; refused while numpy or a recorder is missing |
| `audio-source` / `audio-color` / `audio-rate` | `[VALUE]` | the globals `audio_source` (a name from `audio-sources`; `default` or empty: the default output's monitor), `audio_color` (`on off`, default on: pitch colours the bulbs; off: the profile's colour), `audio_rate` (the old name of `follow_rate`, kept as an alias); also as `set audio_source=…` etc. |
| `follow-rate` | `[N]` | the global `follow_rate`: updates per second per bulb while it follows audio or video, 1–3, default 2 (before: `audio_rate`, migrated); also `set follow_rate=N` |
| `video` | `on\|off\|toggle` (`which`) | the selection (or `which`) follows its region of the screen instead of the screen brightness, see *Follow video*; turning it on turns follow-audio off for those bulbs (and the other way round); refused while GStreamer's `gst-launch-1.0` or `pipewiresrc` is missing |
| `video-region` | `ID REGION` | the part of the screen bulb ID follows: `whole` (default), `left`, `right`, `top`, `bottom`, `top-left`, `top-right`, `bottom-left`, `bottom-right` (`up`/`down` are taken for `top`/`bottom`); kept in the bulb's entry as `video_region`, whether it follows video or not |
| `video-max` / `video-smooth` | `[VALUE]` | the globals `video_max` (the brightness the brightest screen gives, 0–100, default 100) and `video_smooth` (time averaging in seconds: `0 0.2 0.5 1 2 5`, default 0.5); also as `set video_max=…` |
| `video-forget` | – | forget the screen permission: the restore token, and its grant in the permission store; the next start of follow-video asks again |
| `video-status` | – | replies `get`'s `video_status` alone |
| `audio-sources` | – | replies `{"sources": [{name, description, monitor, default}], "selected": "…"}`: every source (`pactl`), sink monitors first; `default` marks the default output's monitor and the default microphone; `selected` is `audio_source` (`""` = the default output's monitor) |
| `audio-status` | – | replies `get`'s `audio_status` alone (for a meter: poll it) |
| `adapters` | – | replies `{"adapters": [{name, address, bus, builtin, powered, in_bluez, devices, claimed}], "selected": "…", "chosen": "hci0", "others_stop": true}`: every Bluetooth controller (sysfs, BlueZ over D-Bus); `builtin` false only for a USB adapter on a removable (or unknown) port; `devices` the BlueZ devices connected through it (our mesh bulbs left out); `claimed` held by flick mode `exclusive`; `selected` the `exclusive_adapter` setting, `chosen` the adapter it means; `others_stop` taking it leaves BlueZ without an adapter (it is built in or the only one) |
| `clap-sources` | – | replies `{"sources": [{name, description, default, muted}], "selected": "…"}`: the microphones (`pactl`, sink monitors left out), the default source first; `selected` is `clap_source` (`""` = the default) |
| `clap-hits` | – | replies `{clap, sensitivity, bars, hits}` for tuning: `bars` the family-score bars at this sensitivity (`first`, `partner`, `veto`, see *Clap to toggle*); `hits` the last 30 hits the gate passed to the model, oldest first, each `{time, age, over_db, rise_db, decay_ms, clap_like, family, kind, top, accepted, partner, why, paired}`: `time` local `HH:MM:SS.mmm`, `age` seconds; the gate's numbers (dB over the noise floor, rise over the 30 ms before, ms until it fell back); `family` the summed clap-family score, `kind` `Clap`/`Snap`/`""` (no clear kind: its partner's); `top` the 3 best classes `[[name, score], …]`; `accepted` passed the first bar, `partner` the lower one, `why` why not accepted (`"family 0.08 < 0.18"`, `"Typing 0.52 beats 0.10"`, `"speakers"`), `paired` part of a double. Memory only (numbers, never audio), gone when the detector restarts; `[]` while not listening. The same per hit at log level debug |
| `clap-meter` | – | replies `{clap, state, available, missing, install, error, muted, level_db, level, floor_db, last, double, model_runs}` for the test meter (`clap`: what the detector listens for, every widget's modes together); poll it (the widget: every 250 ms while the Sound page is shown). `level` 0–1 (−60…0 dBFS, the loudest 10 ms of the last 100 ms, DC offset removed), `floor_db` the gate's noise floor, both `null` while not listening; `last`: the last sound the classifier judged, `{label, score, accepted, mirrored, age}` (`mirrored`: the speakers played it); `double`: the last double, `{label, score, age}`; ages in seconds |
| `unignore` | `ID` \| `ADDRESS` | take a device off `autoconnect_ignore` (by its bulb id or discovered address); with autoconnect on, it is looked for at once |
| `identify` | `ID [on\|off]` | blink the bulb slowly to find it: 1 s at 100 %, 1 s off, until `off`, a flick or 120 s (every mesh message uses up a sequence number). A used, reachable bulb only (otherwise refused); while a test runs on it: refused |
| `test` | `ID [stop]` | about 25 s, then the bulb is given back: 100 % for 1.5 s; a fade to its dimmest lit level over 4 s (one message with the backend's transition time where it has one, `fade`, else 8 steps); colours 1 s each: red, green, blue, white (`hs`), warmest and coldest white (`ct` only), none otherwise; then on/off with half-periods 0.1, 0.15, 0.25, 0.4, 0.7 and 1.0 s, 3 cycles each, counting the switches the backend took (acknowledged where it waits for an answer, e.g. mesh; sent otherwise). `rate` in `testing` is the most switches per second a phase reached. Ends a running `identify` on the bulb; `stop` ends it. Flashes quickly: not for people sensitive to flashing light |
| `notify` | `[on\|off]` | the global switch of notification blinks (`settings.notify`, default off; also `set notify=on`); while on, the daemon watches desktop notifications, see *Notification blinks* |
| `notify-rules` | – | replies `{"profile": NAME, "rules": [...]}`: the rules of the selection's profile (`@N`: widget N's) |
| `notify-rules-set` | `JSON` | replaces that profile's whole rule list; validated as a whole (refused with a message on the first fault: an invalid regex names the rule and field, nothing changes) |
| `notify-test` | `RULE_NAME` | runs that rule of the selection's profile now on its bulbs (its `dark` setting applies; the switch and Do Not Disturb do not); the reply's `notify_test` lists the bulbs that blink |
| `notify-status` | – | replies `get`'s `notify_status` alone |
| `select` | `ID` \| `all` | what the slider, nudge, `set` (profile keys) and on/off act on (a used bulb of the widget); `all` uses the profile `default` |
| `widget-bulbs` | `[ID,ID…]` | the widget's bulbs (none: empty), stored in list order. Unused bulbs the widget had stay stored (they reappear with `use=on`) |
| `order` | `ID ID …` | the list order: every bulb id once. `get` lists bulbs and widget lists in it |
| `bulb-set` | `ID key=value…` | `name=`, `profile=`, `use=on\|off`, `icon_on=` / `icon_off=` / `icon_disconnected=` (an icon name or an absolute path; empty: none. Without `icon_disconnected` the widget shows `icon_on` dimmed with a `network-disconnect` badge). `use=off` makes the bulb idle: OnPowerUp is set to `on` (a power cut boots it lit; retried until confirmed), then nothing else is sent to it (no levels, power, flick or lid handling); it leaves every widget's list and `all` in `get` but stays stored. A backend whose bulbs are all idle is told it may drop its link |
| `profile save` | `NAME` | copy the selection's profile values into NAME (new or overwritten) |
| `profile apply` | `NAME` | one bulb selected: it uses NAME. `all`: NAME's values go into `default` and every bulb of the widget uses `default` |
| `profile rename` | `OLD NEW` | bulbs follow; `default` can't be renamed |
| `profile delete` | `NAME` | bulbs using it fall back to `default`; `default` can't be deleted |
| `preset …` | as `profile` | alias kept for the old widget |
| `discover` | `[KIND]` \| `bluetooth` | replies `{"discovered": [{kind, address, name, config, known}], "errors": {KIND: message}}` after ~5 s; `bluetooth`: every kind whose backend has `bluetooth`; `known`: a bulb has that address already; a failing kind is only in `errors` |
| `add` | `KIND key=value…` | `name=` names the bulb (default `<product> - <ID>`: the backend's product name, e.g. `Hauslicht - 0002` for a Ledvance mesh bulb, `Mock bulb - 1`, and the id after `kind:`), the rest is checked against the backend's `fields`; a new bulb starts switched on, in use, at the end of the list and in widget 1. Replies the `get` object plus `"added": ID` |
| `remove` | `ID` | stop controlling it; it leaves every widget (a selected one: selection back to `all`). An autoconnected bulb goes on `autoconnect_ignore` |
| `watch` | – | keeps the connection open and streams events, see below |
| `backends` | – | replies `{"backends": [...]}` (as in `get`) |
| `setup-root` | – | runs `pkexec install/setup-root.sh` in the background (a graphical polkit prompt; Bluetooth restarts when its tuning changes); replies the `get` object at once, `setup.running` tells when it is done. One run at a time. For the CLI without a terminal: the daemon is in no login session, so the polkit agent may not be found; the widget runs pkexec itself |
| `setup-status` | – | the `get` object; the setup status is read anew (after the widget's own pkexec run) |
| `status` | – | replies `[{bulb, on, percent}]` as the devices report it |
| `raw` | `HEX[,HEX…]` | mesh only: send access payloads, reply `[{bulb, sent, replies}]` |
| `mock-flick` | `ID` | mock bulb: as if a wall-switch flick was seen |
| `mock-return` | `ID GAP` | mock bulb: back after an outage of GAP s (`none`: first contact) |
| `mock-offline` | `ID` | mock bulb: unreachable until `mock-return` |

**Identify and Test.** While either runs on a bulb, nothing else is sent to
it: no screen-following level, no colour, no OnPowerUp arming (power, clap,
lid and profile changes only move its intended state meanwhile). One per bulb
at a time. A flick (power cycle), the bulb going offline, `remove`, `use=off`,
suspend and daemon stop end it; then the bulb gets its intended state again
(level, power, colour; a colour the profile leaves alone is put back as it
was) and following resumes. Suspend and stop then run as always (bulbs dark
while asleep with `lid` close/both, OnPowerUp back to `on`).

**Follow monitor power** (desktops only; a laptop, one with a lid or a
portable DMI chassis type, keeps `lid` and is never offered this). With
`monitor_power` on, the lights switch off when you switch the monitor off,
never on standby, and back on (to their intended state, as on lid open) when
it returns. The daemon reads the DRM connectors (`/sys/class/drm/card*-*`)
once a second: a monitor in use (connected, enabled, `dpms` On) that reads
disconnected twice in a row was switched off (DisplayPort drops hot-plug
detect). A connector whose `dpms` is Off (standby, blanking) never counts, nor
does a disconnect while `dpms` was already Off. With `ddcutil` installed and
i2c open to the user, VCP D6 is read at most every 10 s while the option is
on: 05 (off by the power button) counts as off, 04 (standby) is ignored, 01
is on. The lights go off only when every monitor that was on is switched
off. A resume from suspend is not a monitor switched off (the links coming
back are ignored for 15 s); suspend keeps the `lid` behaviour above. `get`'s
`monitor_power` says `available` (false on a laptop, or when no DisplayPort
monitor and no DDC/CI answer: HDMI monitors often stay connected when
switched off), `why` (a line when not), `monitors` (`connector`, `status`,
`dpms`, `watch`: `hotplug` / `ddc` / `none`, `off`) and `follow` (the setting).

**Clap to toggle.** `clap` is a widget's mode (`@N clap MODE`; the popup's
selector), `clap_source` and `clap_sensitivity` are global (Configure →
Sound). While at least one widget's mode is not `off`, the daemon listens to
the microphone (`clap_source`) through `pw-record` (else `parec`), mono 16 kHz,
through a pipe: nothing is written to disk, nothing leaves the computer, and
Plasma shows the microphone in use. The microphone's DC offset is taken off
first (some built-in microphones carry one, −17 dBFS on a laptop here). A
level gate on 10 ms blocks follows the room's noise floor (falling fast,
rising at most 5 dB/s through louder sound) and looks for a sound that rises
8–18 dB (high … low sensitivity) over it within one block, out of quiet, and
falls back within 100 ms (within 800 ms after a hit, a block that jumps 4 dB
over the last 30 ms counts too: a second clap on the first one's reverb);
only then YAMNet (TFLite, run by ai-edge-litert) classifies the 0.975 s
around it. A hit is judged by the summed score of the *clap family*:
Clapping, Hands, Applause, Slap/smack, Finger snapping (weight 1), Cap gun,
Whip, Crack, Burst/pop, Chop, Wood block (0.5), Knock, Tap (0.25). It is
*accepted* when that reaches 0.5 − 0.4 × sensitivity/100 (×0.6 for a
clap-like shape: rises 15 dB within a block and falls back within 50 ms)
and beats every Speech, Music and Typing/keyboard class. Its *kind* is
`Snap` when Finger snapping outscores the clap classes, `Clap` otherwise, and
none below 0.05 (it takes its partner's). The other hit of a double, the
*partner*, may be softer and of another kind: it needs a quarter of that bar
(nothing when clap-like) and must not be clearly something else (a Speech,
Music or Typing class at 0.15 + 0.2 × sensitivity/100 or more, and above its
family score). An accepted hit and a partner 120–800 ms apart, with no other
sharp sound in the 600 ms before or 300 ms after (a typing burst never
pairs), make a double: `Clap`, `Snap` or `Clap+Snap` (the detector listens
for what any widget wants: in `clap` alone a `Snap` double is ignored). It
toggles the selection of every widget whose mode matches (a clap+snap pair: `both` only)
like the `toggle` hotkey (any on → all off, else all on; a bulb in two such
widgets is switched by the lower-numbered one only); then 1 s cooldown.
`clap_status.muted` is the chosen source's mute switch (`pactl`, read every
3 s while listening; `null` otherwise): the widget shows a hint when it is
`true`. A hit that the default sink's monitor played within 150 ms is
dropped (a passive stream: it never wakes a suspended sink). numpy and
ai-edge-litert are an optional extra (`requirements-clap.txt`); without them
`get`'s `clap_status` says `available: false` with the `install` command.
The model (4.1 MB) is downloaded on first use to `~/.cache/hauslicht/`
(pinned URL, sha256 checked).

**Follow audio.** `audio on` switches the selection's bulbs (kept per bulb
as `audio: true` in its entry, so across restarts) from the screen to what
the computer plays. The daemon then records `audio_source` (default: the
monitor of the default output, a passive stream that never wakes a suspended
sink; any source, microphones included, may be chosen) through `pw-record`
(else `parec`), mono 16 kHz, through a pipe (nothing on disk, nothing leaves
the computer), only while some bulb follows. Per 50 ms block: the RMS in dB
through an envelope (attack 50 ms, release 400 ms), mapped between an
adaptive floor and ceiling (10th and 95th percentile of the last 10 s of
sound, at least 12 dB apart), so quiet and loud music both use the whole
range, onto the profile's `range_lo`…`range_hi` (within 0–100), never below
the lowest lit step: the mode never switches a bulb off. Silence (below the
floor, or no data, for 3 s) holds the range's low end. The spectral centroid
(50 Hz–8 kHz), smoothed over 300 ms, on a log scale 100 Hz…4 kHz gives the
colour with `audio_color` on: hue 0–300° (saturation 100) on `hs` bulbs, the
bulb's own Kelvin range on `ct` bulbs (warm low, cold high), nothing on
plain dimmers. Updates per bulb: at most `audio_rate` a second (never more
than 3, whatever the kind: fast flashing is a photosensitivity risk, and on
Bluetooth mesh every message uses one of the bulb's 24-bit sequence numbers),
and only when the brightness moved ≥ 3 points, the hue ≥ 8° or the white
≥ 150 K since the last update, or 2 s passed with any change. The level
goes out as `fade` over the update interval where the backend has a
transition time (one message), else `apply`. `audio_status.messages` counts
the messages sent this run (`mesh_messages` the mesh ones). The popup's
brightness slider, `bulb`, `dim`, `pause` / `resume` (manual / automatic) and
a nudge of the brightness end it for those bulbs, as for the screen; `use=off`
too. Power (`on`/`off`, clap, lid, flicks), Identify/Test and OnPowerUp
arming work as with the screen. A capture that fails to start or ends (no
recorder, the source gone) turns the mode off for every bulb, with the
reason in `audio_status.error`; there is no retry.

**Follow video.** `video on` switches the selection's bulbs (kept per bulb as
`video: true` in its entry, so across restarts) to what is on screen, each
bulb its own `video_region` (ambilight: one lamp follows the left half,
another the right). While some bulb follows, the daemon holds an
xdg-desktop-portal ScreenCast session (one monitor, the cursor hidden,
`persist_mode` 2) and runs `gst-launch-1.0`: `pipewiresrc` on the portal's
PipeWire fd, at most 5 frames a second, scaled to 48×27 (every pixel
counts), RGB through a pipe (nothing on disk, nothing leaves the computer).
Per frame every region's mean colour is taken in linear light (halves and
quarters; on the odd height the middle row belongs to both halves) and
smoothed exponentially over `video_smooth` seconds. Its luminance (Rec. 709)
maps onto 0…`video_max` percent, never below the lowest lit step: the mode
never switches a bulb off. `hs` bulbs also get the region's hue and
saturation (as it looks, from the sRGB encoding); white-only and plain bulbs
get brightness only. Updates go through the same cap and deadband as
follow-audio (`follow_rate`, at most 3 a second whatever the kind;
`video_status.messages` counts them, `mesh_messages` the mesh ones). The
popup's brightness slider, `bulb`, `dim`, `pause` / `resume`, a nudge of the
brightness and `use=off` end it, as for audio; power, clap, lid, flicks,
Identify/Test and OnPowerUp arming work as with the screen.

The first start shows the portal's permission dialog; its restore token is
kept in the settings (`video_token`, never in `get`; `video_status.permission`
says whether there is one), so later starts and restarts need no dialog when
"allow restoring" was ticked. The portal keys the grant by the caller's app
id: the daemon registers as `io.github.gelaende51.HausLicht`
(`org.freedesktop.host.portal.Registry`), which needs that `.desktop` file
(`install/install.sh` installs it, hidden); without it the daemon is an app
without an id, which persists all the same. At start, bulbs that followed
video resume only when the permission store still holds the token's grant;
otherwise they stay off and `video_status.hint` says "Follow video needs
permission again" (no dialog pops up unasked). A refused dialog, a session
the compositor or the user ends, or a capture that exits turns the mode off
for every bulb, with the reason in `video_status.error`; there is no retry.

**Notification blinks.** With `notify` on, the daemon opens a second
session-bus connection and makes it a monitor
(`org.freedesktop.DBus.Monitoring.BecomeMonitor`, match
`type='method_call',interface='org.freedesktop.Notifications',member='Notify'`);
where the bus refuses that, it tries an `eavesdrop='true'` match, else
`notify_status.state` is `unavailable` with the reason in `error`. Do Not
Disturb (`Inhibited` of `org.freedesktop.Notifications`, set by Plasma) is
read when a notification arrives: while true nothing blinks. Each used bulb
takes the first rule of *its own profile's* `notify_rules` whose regexes
all match (searched, case-insensitive; empty matches anything) the app name,
title (summary) and body; the rule's `bulbs` then say whether the bulb takes
part: `all` (every bulb in use) or `selection` (widget 1's selection). A
rule:

```json
{"name": "Mail", "app": "thunderbird", "title": "", "body": "", "pattern": "blink",
 "count": 2, "intensity": 100, "color": "#3080ff", "bulbs": "all", "dark": false}
```

`name` required and unique; `app`, `title`, `body` regexes (at most 200
characters); `pattern` `blink` (on/off), `pulse` (down in two steps and up
again) or `double` (two quick blinks, then a 1 s pause); `count` 1–10;
`intensity` 1–100, the flash's level; `color` `#rrggbb` or empty, sent only
to colour (`hs`) bulbs and put back afterwards (a colour that cannot be read
back is not changed); `dark` false (default): a bulb that is off is left
alone, so a notification at night never lights a dark room; a lit bulb
dims to its lowest lit level between flashes instead of switching off. The
blink is an action like `identify` (kind `notify`): following pauses, the
bulb gets its intended state back after it. Light changes are at least
0.34 s apart (never more than 3 per second: photosensitivity, and every
mesh message uses up a sequence number). A notification for a bulb that
is blinking already, or that runs `identify` or `test`, is dropped, never
queued; `identify` and `test` take a bulb from a running blink. Titles and
bodies are never logged or stored: the log and `notify_status.last` carry
the rule's name and the app name only.

**Autoconnect.** With `autoconnect` on, the daemon runs `discover` over
every available kind that allows it, at once when switched on (or at start)
and then every 5 minutes, and adds each device it finds that no bulb has yet
(matched by the discovered address against the bulbs' config) and that is not
on `autoconnect_ignore`, as `add` would: in use, switched on, in widget 1,
with the default name. Bluetooth mesh bulbs are never added this way
(`autoconnect = False` in the backend: adopting provisions the bulb into this
mesh); adopt them by hand. A device whose `add` fails (it needs a key, a
pairing button) is tried once per daemon run. It may take over lights that
belong to someone else nearby (neighbours' bulbs, shared spaces). Removing an
autoconnected bulb puts `{id, address, name}` on `autoconnect_ignore`;
`unignore` takes it off.

Mock bulbs take `color=none|ct|hs|both` on `add` (their colour caps) and
`transition=on` (their `fade` takes a transition time); `status` replies
their last colour as `color`.

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
  "hue": 30.0, "sat": 0.0, "follow_ct": false, "shift": 100.0, // its colour
  "lid": "both",
  "laptop": true,               // a lid or a portable chassis: lid applies, monitor_power does not
  "monitor_power": {"available": false, "why": "a laptop: …", "monitors": [], "follow": true},
  "night": true, "day": false,  // KWin Night Light: daylight false / true
  "color_scheme": "dark",       // portal color-scheme: dark | light | default | null
  "screen_kelvin": 4500,        // KWin Night Light currentTemperature (6500 = no shift), null if unreadable
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
  "flick": "on",                // off | on | fast | exclusive
  "exclusive_adapter": "",      // hciN or a MAC; "" automatic (USB if any, else built-in)
  "exclusive": {"active": false, "adapter": null, "note": ""},
                                // exclusive runs on adapter (hciN); note: why it works like fast
  "flick_last": "fast",         // the last mode but off: the popup switch turns flick back on to it
  "autoconnect": false,         // add newly discovered devices by itself (never mesh)
  "autoconnect_ignore": [{"id": "wiz:a8bb50aabbcc", "address": "192.168.1.40", "name": "WiZ - a8bb50aabbcc"}],
                                // removed autoconnected devices, not added again
  "clap": "off",                // this widget's mode, off | clap | snap | both: a double clap/snap
                                // toggles its selection
  "clap_source": "",            // microphone (a source name), "" the default source (global)
  "clap_sensitivity": 50,       // 0 (low) - 100 (high) (global)
  "clap_status": {"state": "listening", "available": true, "missing": [], "install": "", "error": "",
                  "muted": false},
                                // state: off (no widget has a mode) | unavailable | downloading | starting
                                // | listening | error; muted: the source's mute switch, null when unknown
                                // or not listening;
                                // install: the pip command while numpy / ai-edge-litert are missing
  "audio": false,               // the selection follows audio (any of its bulbs)
  "audio_level": null,          // the level it is driven to, percent (null: not following audio)
  "audio_source": "",           // what follow-audio records, "" the default output's monitor (global)
  "audio_color": true,          // pitch colours the bulbs (global)
  "audio_rate": 2,              // updates per second per bulb, 1-3 (global)
  "audio_status": {"state": "listening", "error": "", "available": true, "missing": [], "install": "",
                   "silent": false, "level": 0.62, "tone": 0.41, "centroid": 470, "level_db": -23.5,
                   "floor_db": -31.0, "ceil_db": -14.2, "messages": 118, "mesh_messages": 118,
                   "by_kind": {"mesh": 118}, "bulbs": ["mesh:0002"]},
                                // state: off | starting | listening | error (error stays until the
                                // next start); level 0-1 and tone 0-1 (null in silence / before any
                                // sound); messages: what follow-audio sent this run
  "follow_rate": 2,             // updates per second per bulb following audio or video, 1-3 (global);
                                // "audio_rate" repeats it (its old name)
  "video": false,               // the selection follows video (any of its bulbs)
  "video_level": null,          // the level it is driven to, percent (null: not following video)
  "video_region": "whole",      // the selected bulb's region (null when several are selected)
  "video_max": 100,             // the brightness of the brightest screen (global)
  "video_smooth": 0.5,          // time averaging in seconds (global)
  "video_status": {"state": "capturing", "error": "", "available": true, "missing": [], "install": "",
                   "frames": 812, "fps": 5.0, "luminance": {"whole": 0.21, "left": 0.18, …},
                   "messages": 240, "mesh_messages": 240, "by_kind": {"mesh": 240},
                   "bulbs": ["mesh:0002"], "permission": true, "hint": ""},
                                // state: off | starting | asking (the dialog may be up) | capturing |
                                // error (stays until the next start); luminance per region, 0-1;
                                // permission: a restore token is kept; hint: why it is off after a restart
  "setup": {"status": "current", "installed": 3, "required": 3, "running": false, "note": "",
            "script": "/…/install/setup-root.sh"},
                                // root setup: missing | outdated | current (stamp
                                // /usr/local/lib/hauslicht/setup-version vs install/setup-version);
                                // note: why the last run failed (shown by the widget)
  "profile": "default",         // the profile the selection uses
  "widget": 1,                  // the widget described (@N)
  "widgets": 2,                 // how many widgets
  "in_widget": ["mesh:0002"],   // this widget's used bulbs, in list order
  "bulbs_list": [               // every bulb, used or not, in list order
    {"id": "mesh:0002", "name": "Volkslicht", "kind": "mesh", "profile": "default",
     "power": false, "output": 0.0, "connected": true, "online": true,
     "caps": ["dim", "flick", "onoff", "powerup"], // + "ct" (white temperature), "hs" (colour)
     "experimental": false,
     "following": true, "powerup": "on",
     "use": true, "icon_on": "", "icon_off": "", "icon_disconnected": "",  // icon: name or absolute path, "" none
     "audio": false, "video": false, "video_region": "whole",  // follows audio / video, its screen region
     "details": {"model": "Hauslicht", "address": "0x0002", "mac": "A4:C1:38:00:00:00",
                 "cid": "0x052e", "pid": "0x0001", "firmware": "0x0102", "rssi": -61},
                                  // what the backend knows without asking the device, only known keys:
                                  // model (product name), address (mesh unicast, BLE address or host/IP),
                                  // mac, firmware, rssi (dBm, last advert); mesh cid/pid/firmware (VID)
                                  // are stored at adoption (`hauslicht adopt`)
     "step": 1.0,                 // native brightness step in percent (backend `step`, docs/backend-api.md):
                                  // 1 Ledvance mesh, 100/255 8-bit, 100/65535 LIFX; a slider moves in it
     "autoconnected": false}      // added by autoconnect (removing it puts it on autoconnect_ignore)
  ],
  "identifying": ["mesh:0002"],  // bulbs blinking for `identify`
  "testing": {"mock:1": {"step": "strobe", "rate": 6.8, "running": true}},
                                // per bulb its running or last `test` (kept until the next one or a
                                // restart): step bright | fade | colour | strobe while running, done |
                                // stopped after; rate: the most on/off switches per second reached (null
                                // before the strobe); after it ends also why (stop, flick, offline,
                                // suspend, daemon stop, …; "" when done) and fade (transition | steps)
  "notify": false,              // notification blinks on (global)
  "notify_rules": [],           // the rules of the selection's profile (see notify-rules-set)
  "notify_status": {"state": "monitor", "available": true, "error": "", "inhibited": false,
                    "last": {"rule": "Mail", "app": "Thunderbird", "time": 1760000000.0},
                    "running": []},
                                // state off | starting | monitor | eavesdrop | unavailable (error: why);
                                // inhibited: Do Not Disturb at the last reading (null: unreadable);
                                // last: the last match, never the text; running: bulbs blinking now
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
{"event": "snapshot", "t": 1760600000.12, …get… (bulbs_list with each bulb's `step`),
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
{"event": "clap", "t": …, "label": "Clap", "widget": 1, "score": 0.82, "power": true}
{"event": "audio", "t": …, "on": false, "error": "recorder ended (exit 1): …"}
{"event": "video", "t": …, "on": false, "error": "screen sharing was refused"}
{"event": "identify", "t": …, "bulb": "mock:1", "running": false, "why": "limit"}
{"event": "test", "t": …, "bulb": "mock:1", "running": false, "why": "", "rate": 7.1}
```

`t` is Unix time (float). `intended` comes whenever the core's intended
state of a bulb changes, before it is sent: `percent` is the computed level
(while paused without a level: the last one sent; `null` for an idle bulb,
`use=off`), `color` is the colour the core wants it to show:
`{"mode": "hs", "hue": 30.0, "sat": 80.0, "rgb": "#ff9933"}` or
`{"mode": "ct", "kelvin": 3200, "rgb": "#ffb977"}` (`rgb` for display: hs at
full value, ct its blackbody colour), `null` when the colour is left alone
(no `ct`/`hs` cap, a white-only bulb not following Night Light, or idle). It
is sent only while the bulb is lit. Device events come the moment the
core learns of them, before anything is sent to the bulb; for an idle bulb
`flick` and `returned` carry `"idle": true` instead of `power` (nothing is
done). Any number of watchers may connect; one that falls 1000 lines behind
is disconnected, so a stuck client never holds the daemon up.

## Hotkeys and panel widgets (KDE Plasma)

`haus/kde.py`, started with the daemon; outside Plasma, or when a call
fails, it logs and does nothing.

**Global shortcuts.** kglobalaccel component `hauslicht` ("Haus.Licht"),
all actions without default keys (assign them in System Settings →
Shortcuts; keys set there survive restarts: the actions are registered with
`IsDefault` empty and then `SetPresent` with autoloading). They act on
widget 1: `select-all`, `select-1` … `select-9` (the k-th bulb of widget
1's `in_widget`; ignored when there is none), `toggle` (as `toggle`),
`remote-up` / `remote-down` (as the wheel: `nudge ±5`), `direct-up` /
`direct-down` (the selection's brightness ±5: the level while it rules,
else a manual level, as `nudge` in mode `off`). Try one with
`busctl --user call org.kde.kglobalaccel /component/hauslicht
org.kde.kglobalaccel.Component invokeShortcut s toggle` (every running
daemon with this code reacts, test instances included).

**OSD.** Each hotkey shows Plasma's OSD (`org.kde.osdService`): `showText`
with the selection ("All bulbs", a bulb name, "… · on/off"); levels as a bar
through `mediaPlayerVolumeChanged` (0 % as text). Silent on failure.

**Panel widgets.** At start and whenever `widgets` changes, every panel
holding a system tray gets `haus.licht.tray` applets with `widgetIndex`
(General) 2…N exactly once, inserted just left of the tray, and loses those
above N (and duplicates), through `org.kde.PlasmaShell.evaluateScript`.
Widget 1 is the tray's own instance and is never touched; applets with
`widgetIndex` 1 neither. A panel whose screen is not connected is only
tidied. `HAUSLICHT_NO_PANEL=1` switches this off, `=0` on; unset, it is on
for the live daemon and off for a test instance (`--no-desktop-writes`).

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
