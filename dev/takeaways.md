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
  BleakClientWithServiceCache. The vendor status (`C4 2E 05 xx`) counts
  queries since boot.
- The vendor model answers Telink's `VD_GROUP_G_GET` (`C1 2E 05` →
  `C4 2E 05 01` = on); the official app does not use it for brightness.
- Factory reset: switch on/off five times (5 s each), then on; tunable white
  flashes cold/warm to confirm. Ledvance's pages also give longer sequences.

## Linux side
- A BlueZ configured with `ControllerMode = bredr` cannot scan Low Energy at
  all, while classic devices keep working; `dual` fixes it.
- BlueZ drops an unpaired LE device object soon after a scan stops; bleak then
  fails with "device disappeared". Scanning again right before each connect
  fixes it.
- btmesh's `provision_and_toggle.py` only accepts a Network ID proxy advert, so
  configuring right after provisioning fails for about a minute (Node
  Identity); any already-provisioned bulb can serve as proxy instead.
- Plasma's brightness applet has no API for third-party devices. A `uleds` LED
  named `*::kbd_backlight` is picked up by UPower (1.91) on hotplug and offered
  as the keyboard backlight, but PowerDevil only looks at startup: it needs a
  restart once the LED exists.
- A new uleds LED reads 0 until set; ignore that first value or the saved
  slider position is lost.
