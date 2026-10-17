# Takeaways

Findings from getting Ledvance Volkslicht bulbs ("LEDVANCE Fil VL T-W", 2020)
onto Linux. Worth reporting upstream where marked.

## The bulbs
- Standard Bluetooth SIG Mesh, PB-GATT provisioning, no OOB. Composition:
  CID 0x052e, PID 0x0006, VID 0x3233, one element, SIG models 0x0000 0x0002
  0x0003 0x1000 0x1002 0x1004 0x1006 0x1007 0x1300 0x1301, vendor 0x052e:0x0000.
  Features relay + proxy + friend.
- They advertise the unprovisioned beacon (0x1827) until adopted, a Node
  Identity proxy advert for about a minute after provisioning, then Network ID.
- **Light Lightness is on a 0–100 scale, not 0–65535.** Values above 100 are
  ignored: a Set of 19661 ("30 %") answers with the old present value and the
  light does not change. Generic Level behaves the same. Status replies use the
  same scale (a bright bulb reads 65). Confirmed against the official Ledvance
  SMART+ app (2.1.10), which sends its 1–100 slider value unscaled in a standard
  Light Lightness Set (unacknowledged, 250 ms transition).
  → worth an issue on dasimon135/ha-bluetooth-mesh: a per-device lightness scale.
- **Below 1 %:** Light Lightness Range reads 1–100. Setting it to 1–1000 is
  accepted but only clamps; the output does not scale. A 3 s fade from 1 % to
  off looks smooth, but that is the 0.5 % step passing (see Move below). Generic Level runs at
  twice the Lightness resolution (−32768 + 2 × percent) but stops at +100
  (= 50 %), which gives one extra steady value: 0.5 % (Level −32767).
  Lightness reads that state as 0. Light Lightness Linear always reads 0 and a
  Linear Set of 128 jumped to 50 %: not usable.
  Generic Move Set is accepted (status: target 0, remaining unknown) but moves
  in whole Level steps, about one step per 20–40 s; Move with delta 0 does not
  stop it. So the firmware holds brightness as an integer Level: 0.5 % is the
  finest steady step without new firmware.
- **Wall switch:** cutting mains darkens the LED at once, but the supply
  keeps the chip alive for a variable time (about 1-5 s, shorter with the LED
  on); shorter flicks are invisible: every status keeps reporting "on". A
  reset shows as an advert off the bulb's exact 10.0 s connected rhythm (seen
  only with BlueZ's DuplicateData filter on; with it off, repeats are dropped
  and the rhythm shows 20 s gaps). The bulb asks for a ~5 s link supervision
  timeout; a central-side HCI LE Connection Update to 1 s (root) frees the
  dead link fast. Connecting to a freshly booted bulb while discovery runs
  took ~11 s; stopping discovery first 0-2 s plus 0.4-3 s connect. bleak
  re-reads the GATT table on every connect (~6 s) unless the client is
  BleakClientWithServiceCache.
- **Switching off at the wall can look like a flick:** the dying bulb
  adverts once off its 10 s connected beat (the supply keeps it alive
  1-5 s), then nothing. Only a link (boot count) or the return tells a
  flick from a power-off; an advert within ~5 s of that one is not a return.
- Right after power-up the bulb may refuse notifications: start_notify on
  the proxy's Data Out failed with GATT "Unlikely Error" (0x0e), so no mesh
  answer arrives on that link; it died 6 s later.
- The vendor model answers Telink's `VD_GROUP_G_GET` (`C1 2E 05`, company
  0x052e) with `C4 2E 05 xx`: **xx counts these Gets since boot**, not an
  on/off flag (the earlier "01 = on" was a first Get after power-up). Checked
  2026-10-16 with the light off: 01, 02, 03 on one link, 04… on the next, on
  across separate connections; Generic OnOff/Lightness Gets in between leave it
  alone; one byte, wraps ff → 00 → 01. Kept in RAM: during the wall-switch
  work it restarted at 01 after each power cycle (Licht.Haus notes). The
  official app does not use it for brightness.
- Factory reset: switch on/off five times (5 s each), then on; tunable white
  flashes cold/warm to confirm. Ledvance's pages also give longer sequences.

## Linux side
- A BlueZ configured with `ControllerMode = bredr` cannot scan Low Energy at
  all, while classic devices keep working; `dual` fixes it.
- BlueZ drops an unpaired LE device object soon after a scan stops; bleak then
  fails with "device disappeared". Scanning again right before each connect
  fixes it.
- After a timed-out connect BlueZ removes the device object at once:
  bleak-retry-connector's further attempts on the same BLEDevice only fail
  with "device not found" (0.1 s each). Retry with the device of a new
  advert. Its connect timeout is a module constant (BLEAK_TIMEOUT 20 s);
  a client subclass that caps `timeout` in `connect()` shortens it.
- A connect can hang the full timeout although the bulb adverts (02:23:41,
  20 s); the next attempt with a fresh advert took 2.6 s.
- After a long absence the device object (and with it BlueZ's GATT cache)
  is gone: "No services cache available", a full discovery on connect.
- "Service Discovery has not been performed yet" from btmesh's TX pump is a
  write to a link that already died, not a cause.
- btmesh's `provision_and_toggle.py` only accepts a Network ID proxy advert, so
  configuring right after provisioning fails for about a minute (Node
  Identity); any already-provisioned bulb can serve as proxy instead.
- Plasma's brightness applet has no API for third-party devices. A `uleds` LED
  named `*::kbd_backlight` is picked up by UPower (1.91) on hotplug and offered
  as the keyboard backlight, but PowerDevil only looks at startup: it needs a
  restart once the LED exists.
- A new uleds LED reads 0 until set; ignore that first value or the saved
  slider position is lost.

## Generic OnPowerUp (power-up behaviour)
- The Volkslicht has Generic Power OnOff Server/Setup Server (0x1006/0x1007);
  opcodes as in the Mesh Model spec and the official app's Telink constants
  (G_ON_POWER_UP_GET/STATUS/SET/SET_NOACK = 0x8211/0x8212/0x8213/0x8214).
- **The AppKey was not bound to 0x1006/0x1007** on a bulb set up before:
  OnPowerUp Get went unanswered (OnOff Get on the same link answered). After a
  Config Model App Bind of both models, Get answered at once. `setup`/`adopt`
  now bind them; the daemon binds them itself once when a Get goes unanswered.
- Factory value read: 1 (default: on at Light Lightness Last). Set 0 and 1 are
  acknowledged with a Status echoing the value within ~60-110 ms and read back
  unchanged after a daemon restart (new link), so the value is kept in the
  bulb. Each Set is presumably a flash write: the daemon sends one only when
  the expected-after-flick value changes (power toggle, lid, settings), never
  per brightness change; a daemon restart costs two (1 at stop, re-arm at start).
- Not yet observed on a real flick: whether a reset with OnPowerUp 0 boots
  without any visible flash, and how long the bulb takes to come up dark.
- The daemon's own disconnect at shutdown makes the bulb advertise off its
  10 s grid, which the flick detector took for a flick: the scanner callback
  is now muted once shutdown starts.
- **A Bluetooth link drop looked like a flick**: link gone and bulb back within
  15 s (or an off-beat advert, which a bulb that dropped the link also sends)
  toggled the light, or switched it on with flick mode off, without any power
  cut. Now the mesh backend reads the vendor boot count on every new link
  (~0.1 s, one Get): went on by 1–4 → same boot, the link only dropped: no
  flick, no power return; restarted at 1–4 → rebooted: the old rules; no
  answer or anything else → the old rules, logged ("boot count unknown"). The
  stored reference is moved out of 1–4 and the wrap with a few extra Gets, so
  "continued" and "fresh" can never both fit. A bulb that never answers is
  asked twice per daemon run, then not again (non-Telink mesh lights).
  Alternatives not needed: the bulb's own mesh SEQ jump after a reboot, the
  advert phase. Still to see on a real wall-switch flick: that a power cycle
  of ~1 s restarts the count (a supply that keeps the chip alive is no reboot
  and now rightly no flick), and the delay the extra Get adds before the toggle.

## Several bulbs (the haus/ package)
- KWin Night Light's `running` is true day and night whenever Night Light is
  on; only `daylight` tells the hour (false = night, true = day or Night
  Light inactive; Constant mode: always false). `track night|day` read it.
- Power is now kept per bulb in hauslicht.json. A bulb not reached since the
  daemon started is a *first contact*, not an outage: it keeps its saved
  power (before, the "long outage → on" rule lit it after every restart).
  The migration from the single-bulb file switches every bulb off.
- Wall-switch detection stays per proxy link: the 10 s advert grid only holds
  for the bulb we are connected to, so with several mesh bulbs only the
  proxy's flicks and outages are seen.
- bleak warns "Using default MTU value" on every GATT write here (noise).
- Unix socket paths are limited to ~107 bytes: a socket in a deep temp
  directory fails with "AF_UNIX path too long".

## Backend layer (contract gaps the backend work turned up)
- `discover(timeout)` gets no config: server-based kinds (Home Assistant,
  MQTT, Matter, Hue Bridge) can only list lights of servers already
  configured, and copy the credentials from another bulb of the same server.
- `set_powerup` can only refuse a whole call (False): Kasa cannot do "off",
  and there is no way to say "this value never, the others yes" up front.
- Backends cannot coordinate Bluetooth scanning with the mesh scanner; the
  Bluetooth kinds (huebt, ledble) scan on their own beside it.
- Each module carries its own small German string table, because
  `common.py` is shared and would turn into a merge hotspot.
- Only mesh, LIFX, Shelly, Tasmota and HA/MQTT/Matter availability can
  report a power return (uptime counters or availability topics);
  WiZ, Govee, Yeelight, ESPHome and Kasa cannot, so no flick/outage
  detection there.
- BLE failures raise ConnectionError, so the core's retry back-off (up to
  300 s) slows recovery after a bulb comes back; an Offline-style "wait for
  the advert" path would be faster.
- Test fakes: amqtt pins websockets==15, lifx-emulator needs a newer one;
  pip refuses both in one resolve. The suite runs with websockets 17 and
  amqtt installed with `--no-deps` (requirements-dev.txt).
- The widget's executable data engine does not pass HAUSLICHT_SOCK from a
  `plasmawindowed` started with it set: a windowed widget still talks to the
  live socket, so it cannot be pointed at a test instance that way.
- pkexec from a systemd user service: the process is in no logind session, so polkit must fall back to the user's display session to find the agent; unverified here (2026-10-17). Exit 126 = dismissed, 127 = no agent / not authorised; the daemon reverts the flick mode on either.
