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
- **Unanswered vendor Gets still count**: on links where start_notify
  failed ("Unlikely Error"), the Get reached the bulb and the count moved
  on (6 -> 10 over two such links, 2026-10-20). A drop/reboot verdict must
  allow for them.
- **A verdict without a read count is a guess**: on a night of weak radio
  (connects failing for minutes, notifications refused), "unknown → old
  rules" produced 30 false flicks while the count went on 5 -> 38. A bulb
  that answered before now waits for a link that reads it.
- Right after a connect the bulb adverts once (0-2 s), off the 10 s rhythm
  that follows: measuring the next advert against it looks off-beat.
- After a timed-out connect bluetoothd may log "No matching connection for
  device": the kernel link came up just as bleak cancelled it.
- Kernel log on this RTL8821CE: "unexpected event for opcode 0xfc19" on
  connects (vendor event, since 10-15, harmless as far as seen).
- **WLED and DMX libraries (2026-10-22)**:
  - python-wled 1.1.1 (MIT) not used: `update()` reads /json plus
    presets.json, /json/cfg and the effect/palette lists (heavy for an
    ESP8266 on every probe), URLs are fixed to port 80, and it pulls
    mashumaro, orjson, awesomeversion and backoff. Two endpoints in plain
    aiohttp do it. Verified from its source: `tt` is in 100 ms units, capped
    at 655; `leds.lc` bits 1 RGB, 2 white, 4 CCT; a `seg` object without an
    id applies to all selected segments.
  - sacn 1.11 (MIT): only `DataPacket(...).getBytes()` for the E1.31 wire
    format. Its sACNsender is not used: a thread per sender at 30 fps, binds
    0.0.0.0:5568, universe-discovery broadcasts by default, and
    `deactivate_output` sends stream-terminated packets (receivers then act
    on data loss at once); we need our own per-universe merge and an idle
    stop anyway.
  - stupidArtnet 1.6 (MIT) not used: one socket and one thread per universe
    at a fixed fps, no ArtPoll; ArtDmx is an 18-byte header, written by hand.
  - DMXEnttecPro 0.4 is GPL-3 and opens `serial.Serial` itself in its
    constructor (no port object can be passed in, so no fake in tests); the
    USB Pro frame is 5 + n + 1 bytes, written by hand.
  - pyftdi not used for Open DMX: it needs ftdi_sio detached and libusb
    access. pyserial's `break_condition` (break ~100 us, then MAB) works with
    the kernel driver; `flush()` (tcdrain) before the next break, else the
    break cuts the frame. `send_break()` is useless here: Linux holds it
    0.25 s or more.
  - Open DMX and USB Pro share FTDI 0403:6001; discover tells them apart only
    by the product string ("DMX USB PRO"), so the guess can be wrong.
  - The dmx id is output + universe + start address, as specified: two
    Art-Net nodes or two dongles with the same universe and address collide
    (the second add is refused as "added already").

## Colour on the network kinds (2026-10)
- python-kasa 0.11, IOT bulbs: `set_hsv` / `set_color_temp` go through
  `_set_light_state`, which adds `on_off: 1` when the caller gives none: a
  colour switches a dark bulb on. SMART (`set_device_info` without
  `device_on`) does not. We send IOT colour only while lit, with the level.
- python-kasa: `color_temp` exists as a feature only when the reported range
  is wider than one value (`_check_supported`); `valid_temperature_range` is
  deprecated for `get_feature("color_temp").minimum_value/maximum_value`.
- pywizlight 0.6.6: `get_bulbtype` raises WizLightNotKnownBulb for an RGB/TW
  module without a white range, and for an unknown module name; PilotBuilder
  `hucolor` adds a `w` key (0 at full saturation). `_set_colortemp` clamps to
  1000-10000 K only, not to the bulb's own range.
- pywizlight's fake bulb answers getUserConfig with a stray "method not
  found" first (on purpose), so a sequence of replies is normal there.
- LIFX has no capability message: the colour flag and white range live only
  in products.json; the product table in lifx.py is checked in the tests
  against lifx-emulator's generated registry. Unknown product ids count as
  colour 1500-9000 K (every product since 2020 is).
- Hue CLIP v2 has no hue/saturation, only `color.xy`: hue/sat convert via
  sRGB and Signify's wide-gamut matrix, and the bridge clips to the light's
  gamut. Mode switching is implicit (xy or mirek in the PUT).
- Tasmota does not report its CT range in Status 11 (CTRange exists only as
  a command); and with SetOption20 0 any colour command switches it on.
- Shelly Gen1 colour bulbs take the level as `gain` in colour mode and as
  `brightness` in white mode: apply must know the mode. Gen2+ colour lives
  in separate RGB / RGBW / CCT components, which the backend does not drive.
- Govee's LAN API has no capability query; devStatus showing `color` /
  `colorTemInKelvin` is the only hint.

## Clap detection (haus/clap.py)

- `pw-record … -` writes a Sun AU header (24 bytes, ".snd" little-endian)
  before the samples unless `--raw` is given; `parec` writes raw by default.
- Recording the sink monitor with `parec -d @DEFAULT_MONITOR@` wakes a
  suspended sink (state RUNNING: the amplifier stays on). `pw-record -P '{
  stream.capture.sink = true node.passive = true }'` leaves it SUSPENDED and
  gets data only while something plays: the right stream for "did the
  speakers make that sound".
- MediaPipe's `yamnet.tflite` (float32/1, 4.1 MB, pinned sha256) is a zip
  too: `yamnet_label_list.txt` is inside, so the class names need no second
  download. Input 15600 float samples, output [1, 521]; ~5 ms per window on
  one thread with ai-edge-litert 2.3 (XNNPACK), cp314 wheel works.
- A synthetic decaying noise burst is "Cap gun" to YAMNet, not Clapping: the
  tests mock the classifier; thresholds need real claps in the real room.
- A noise floor that falls fast and rises slowly sits below the mean room
  noise: "quiet before" and "decayed" must be judged against the floor too
  (within 6 dB), not only against the threshold, or soft snaps never pass.
- The default microphone here is muted at the source (pactl get-source-mute):
  the recorder then delivers exact zeros (level −120 dB), no error.

## HCI user channel (flick mode exclusive)
- Binding HCI_CHANNEL_USER needs CAP_NET_ADMIN and a device that is down
  (EBUSY otherwise: power it off through mgmt first); after the bind the
  kernel checks nothing on reads and writes ("enforced when binding"), so a
  root helper can pass the socket to an unprivileged daemon (SCM_RIGHTS).
- Closing the last copy of the channel closes the device and re-adds the
  mgmt index: bluetoothd sees a new adapter. Whether it powers it on depends
  on AutoEnable, hence the helper's own Set Powered afterwards.
- The kernel's init is skipped for a user channel: the HCI Reset, the event
  mask (LE Meta, bit 61, is not in the default mask) and the scan setup are
  the daemon's job.
- Core 4.x controllers refuse non-connectable adverts below 100 ms (0x12,
  invalid parameters); 5.0 allows 20 ms. This RTL8821CE reports HCI 8 (4.2).
- The mgmt control channel answers Read Info without root (address, powered),
  and HCIGETDEVINFO gives address, bus and the up flag without root too.
- Python's socket module binds BTPROTO_HCI with a (device, channel) tuple
  (3.14), so no ctypes is needed for the user and control channels.
- sudo keeps stdin as it is when there is no terminal: a socketpair end as
  stdin reaches the helper and carries the fd (would break with sudo I/O
  logging, which pipes stdio).
- The internal combo card's Bluetooth is a USB device (hci0 on usb1/1-4):
  "USB" alone does not mean a dongle; the port's `removable` attribute
  ("fixed") tells.
- Advert gaps on the advertising bearer (hci0 = RTL8821CE, Wi-Fi on the same
  antenna; bulb unconnected, learnt rhythm 0.06-0.21 s), 2026-10-20
  12:00-16:05: 1786 silences of 0.6 s and more, 1719 of them the same boot
  (p50 1.05 s, p99 1.98 s, longest 2.68 s); real reboots were silent 1.01,
  1.50 and 1.79 s. The supply keeps the chip alive through most of a short
  cut, so a reboot's silence can be little more than the boot itself: no
  gap threshold separates lost adverts from reboots on this radio, only the
  boot count does. Checks cluster: 36 % began within 2 s of the previous
  one. Threshold now 0.9 s (a third fewer checks), each check one Get.
- btmesh's MeshNode has no replay protection and matches a reply by source
  and opcode only: with a fresh node per session (and an empty network
  message cache) a late copy of the previous Status answered the next Get
  ("5 -> 5", a false flick). The bulb repeats each PDU (network transmit,
  relays) for 0.1 s and more. Upstream: an RPL per source would drop it.
- A fresh MeshNode per silence check reserved 64 SEQ and rewrote the state
  file each time: ~0.7 M of the 24-bit SEQ space a day, gone in under a
  month (then only an IV Update helps). One node per claim now.
- Clap detection, 2026-10-21: YAMNet calls a soft or untypical clap in a
  mostly quiet 0.975 s window "Silence" or "Inside, small room"; the clap
  evidence is spread over Clapping, Hands, Slap and impulsive classes (Cap
  gun on synthetic bursts). Judge by a weighted family sum, not the top-1
  class. A room's reverb after the first clap held the level gate "busy",
  hiding a softer second clap; a jump over the tail now counts.
- Grepping the journal for `clap` hides the `power …: (double snap …)`
  lines: "double snaps did nothing" was a grep artifact. The real lag was
  the mesh: `screen … -> …` (logged after the backend sent it) came 1.5–3 s
  late after advertising-bearer silences, once letting an on and the next
  off land together.
- btmesh's MeshNode.request waits `timeout` per attempt before it sends
  again: right for a GATT link (nothing gets lost), too slow for the
  advertising bearer, where a lost advert held a Set 3 s. AdvLink asks again
  after 0.5 s (3 s in all). Same TID on each retry: one transaction.
- A silence check (off-beat advert) cancelled the request in flight "on the
  old link", a GATT notion: on the advertising bearer nothing died and the
  bulb was just heard. The user's level Sets are kept now; the check's own
  boot-count reads are still cancelled (the verdict path reads anew).
- btmesh's `MODEL_LIGHT_CTL_TEMPERATURE_SERVER` is 0x1304, the Mesh Model
  spec's Light CTL Setup Server; the Temperature Server is 0x1306 (CTL
  Server 0x1303, HSL 0x1307, Hue 0x130A, Saturation 0x130B). mesh.py keeps
  its own ids. Upstream: a one-line fix. Its opcodes (0x825E.., 0x8264..)
  are right. The colour models sit on secondary elements: the bind names
  the element address but goes to the node's primary one (device key).
- busylight-core 2.5 (2026-10-22): the PyPI hidapi wheel it pulls in makes
  `import hid` the libusb backend on Linux (`hidraw` is a separate module):
  paths are "bus:device:interface", opening needs /dev/bus/usb/BBB/DDD and
  detaches usbhid, so a hidraw-only udev rule is not enough (its own
  `Light.udev_rules()` writes both a usb and a hidraw line, MODE 0666).
  Upstream idea: let the caller pick the hidraw backend.
- busylight-core claims serial ports by VID:PID alone: MuteSync on any CP210x
  (10c4:ea60, also countless Arduino clones and ESP boards), BusyTag on
  Espressif 303a:81df, fit-statUSB on 2047:03df. `Light.all_lights()` would
  open them, toggling DTR (an Arduino or a 3D printer resets). Haus.Licht
  enumerates `ConnectionType.HID` only and leaves serial ports to the
  `serial` kind. Upstream: a product-string check for MuteSync.
- busylight-core's Kuando keepalive is an asyncio task when `on()` is called
  inside a running loop (a threading.Timer otherwise); its periodic runner
  logs and silently ends at the first failed write, so an unplug shows only
  at the next `on()`. Released without `off()`, a Kuando goes dark ~15 s
  later (no keepalive): "what it shows stays" cannot hold for it.
- busylight-core disables its loguru logger on import (`logger.disable`), so
  its "Failed to acquire" reasons stay invisible unless re-enabled.
- Several busylight-core classes share a VID:PID (BlinkStick variants
  20a0:41e5, Luxafor 04d8:f372) and tell themselves apart in `claims()` by
  serial-number version and release number: take the first claiming class.
- Adalight's header carries the LED count minus 1 (hi, lo, hi^lo^0x55);
  WLED's realtime serial input falls back to WLED's own state ~2.5 s after the
  last frame, which may be lit: the serial kind repeats dark frames too while
  a bulb is driven, and stops only when idle.
- logind's Session.SetBrightness needs the caller's own session with a seat;
  the daemon is a user service outside any session, so "session/auto" does
  not resolve for it: user/self's Display property names the session.

