# Back-to-back flicks: notes (WIP, 2026-10-16)

## What the journal shows (journalctl --user -u hauslicht --since 11:10)
- 11:11-11:36: no flick event at all. 11:36:30-11:39:04: eight reboots, each
  detected, judged "rebooted" (boot count 5 -> 1) and re-armed, states
  alternating off/on correctly. No second flick shows up as its own event.
- advert -> armed per event: 11:36:30.9 off 3.18 s; 11:36:44.9 link lost
  (off-beat check still off: within SETTLED_S 12 s of the connect), back only
  11:36:59.2 (14.2 s), on, armed 11:37:00.8; 11:37:35.9 off 8.74 s;
  11:37:57.5 on 2.03 s; 11:38:12.2 off 6.21 s; 11:38:30.6 on 5.25 s;
  11:38:48.9 off 2.06 s; 11:39:04.4 on 1.33 s.
- The slow ones lose 3.8-4.6 s between "off-beat advert" and "connecting to
  proxy" (judge_boot: lock + closing the dead link; the old link only ends at
  its supervision timeout, so the 1 s conn-update apparently did not hold, the
  bulb's own ~5 s may be back), then connect 0.6-3.2 s.
- Conclusion: a second flick that falls inside that window boots with the
  stale OnPowerUp (same state as the first flick: no visible change) and is
  swallowed: the daemon is still connecting, sees no separate reboot (boot
  count reads 1 either way; off-beat adverts are ignored until the new link
  is 12 s old). Likely victims: the 8.74 / 6.21 / 5.25 s windows.
- Side finding: bluetoothd pages an absent BR/EDR HID device
  4C:B9:9B:13:B8:D7 every 60 s ("Host is down", ~5 s page each:
  11:36:57.9, 11:37:57.97, 11:38:57.7). On this Realtek combo a page likely
  starves LE scanning/connecting: fits the 14.2 s "back" at 11:36:59 and the
  0.5 s scanner stop at 11:37:57.5. Remove/unpair that device or disable its
  reconnect to rule it out.

## Options
- A4 done (commit 4d02dc8): setup-root.sh tunes /etc/bluetooth/main.conf
  idempotently with a backup (main.conf.hauslicht-bak): ScanInterval/Window
  Connect 96/96, Min/MaxConnectionInterval 12/24, [GATT] Cache always,
  Channels 1 (default was 3 = EATT on). Needs: sudo install/setup-root.sh,
  sudo systemctl restart bluetooth. Not measured yet.
  Considered, not done: [LE] ConnectionSupervisionTimeout = 100 (1 s from the
  first packet, before the conn-update) - affects every LE device.
- A9 not started: read Generic Default Transition Time (Get 0x820D, Status
  0x8210, Set 0x820E) via the raw path; set 0 only if it fades.
- A3 not started. Concerns: kernel auto-connect (Add Device action 2) also
  reconnects while the daemon is idle (idle drops the link on purpose) and
  bypasses bluetoothd/bleak; must be removed on idle/stop.
- A2 not started. Expectation: BlueZ LEAdvertisingManager1 Type broadcast
  with Data {0x2A: network PDU} should pass (shared/ad.c accepts AD types
  0x01..0x3D; kernel only rejects types it manages); OnPowerUp Set network PDU
  is ~21 bytes, fits 31. Open: a link-drop (not a reboot) would get the wrong
  arm until the GATT verdict corrects it; SEQ via MeshNode + _reserve_seq.

# Long power-off came back dark (2026-10-16 evening, fixed 2026-10-17)

## What happened (journalctl --user -u hauslicht --since "2026-10-16 19:40")
- 19:48, 20:52, 22:19: the wall switch went off for 30 min / 4 h. Each time
  an off-beat advert first (the dying bulb adverts once: the supply keeps
  the chip alive 1-5 s), then the connect hung 20 s and found nothing, "boot
  check: no link ... counted as a flick" -> flick -> off. On the return
  "flick already handled": nothing sent, the bulb booted with OnPowerUp 0
  and stayed dark (02:24 the widget switched it on).
- 02:23 return: notifications never came up (start_notify "Unlikely
  Error"), boot count unanswered, link dead 8 s later; the OnPowerUp Set
  waited 4 s on the dead link and its retry 60 s; the reconnect's first
  attempt hung 20 s although the bulb adverted (24.4 s advert -> connected).

## Fix
- Window: off up to 10 s is a flick; measured drop -> back that is up to
  ~18 s (derivation at QUICK_GAP_S, haus/backends/__init__.py): QUICK_GAP_S
  20 s (was 15). Longer: a power-off, back lit whatever it was before.
- An off-beat advert with no link is no longer decided: the bulb is marked
  lost with the advert as the drop, and the return decides (gap <= 20 s a
  flick, else on). Adverts within HOLDUP_S (5 s) of the off-beat one do not
  count as a return. With a link, the gap advert -> link (or the first advert
  5 s later) decides the same way. "Already handled" only when that advert
  was judged on a link.
- A reboot counted without reading the boot count (unanswered) leaves the
  reference stale: the next fresh count is taken as the same boot, so a link
  drop right after a return no longer toggles the light off.
- Widget off then a long cut: OnPowerUp stays armed "on", the return sets
  power on (test_widget_off_then_long_cut_comes_back_lit).
- Arm retry: a failed OnPowerUp Set is retried on the next link at once
  (was 60 s); a lost link cancels its requests in flight (no lock held 4 s).
- Reconnect: connect timeout 10 s (successes took 0.3-7.3 s), one attempt
  per BlueZ device object, then wait for an advert newer than the failure.

## Still open (root, later)
- A4 is committed but not applied here: /etc/bluetooth/main.conf is
  untouched (sudo install/setup-root.sh; restart bluetooth).
- A3 (kernel auto-connect) and A2 (advertising-bearer arm) not started:
  notes above. The 4C:B9:9B:13:B8:D7 page every 60 s is still a suspect for
  the hung connects.

# Flick modes and the versioned root setup (2026-10-17)
- settings.flick is a mode since schema 4: off | on | fast | exclusive
  (schema 3's true -> on, false -> off). on, fast and exclusive all toggle;
  exclusive is stored and shown only (acts as fast, logged) until its own task.
- Root setup is versioned: install/setup-version (now 3) is the one place;
  setup-root.sh writes it to /usr/local/lib/hauslicht/setup-version at the
  end. Unstamped but conn-update or the uleds conf present: "outdated".
  Choosing fast/exclusive with the setup missing/outdated: the widget runs
  `pkexec install/setup-root.sh` itself (inside the login session, where the
  polkit agent is), then sets the mode; exit 126 (dismissed), 127 (no agent /
  not allowed) or any failure keeps the mode and shows a note. The daemon
  refuses fast/exclusive without a current setup and never runs pkexec for
  the widget; its `setup-root` command is for the CLI (sudo on a terminal).
- setup-root.sh now restarts bluetooth itself when the A4 tuning changed
  (the widget says so before; the mesh link drops and reconnects, boot count
  unchanged: no flick).
- pkexec from the systemd user service (ctl setup-root without a terminal)
  needs polkit to find the user's graphical session (the service is in
  none); that is why the widget runs it. Exit 127: use a terminal.
- The popup's Flick switch: off <-> settings.flick_last (the last mode but
  off, default on), so fast/exclusive survive toggling.

## What fast does now
- A4: the BlueZ tuning via the setup (global for bluetoothd; switching the
  mode back does not undo it; the backup is main.conf.hauslicht-bak).
- A9 (daemon side, fast only, unproven on the Volkslicht): after the first arm
  on each new link the proxy bulb's Generic Default Transition Time is read
  (0x820D -> 0x8210); not 0 -> set to 0 (0x820E, a flash write), once per bulb
  per run. No answer is logged ("A9 not available": DTT server absent or not
  bound to the AppKey). Our level sets carry their own 200 ms transition, so
  DTT only shapes the power-up / OnOff path.
- A3 (kernel auto-connect) and A2 (advertising-bearer arm): not implemented.
  Both need hardware runs first (A3: does bleak take over a kernel-made link,
  and the add/remove around idle/stop; A2: does BlueZ broadcast AD type 0x2A,
  and the SEQ reservation). They would go behind fast; nothing in the setup
  for them yet.

# Link instability, night of 2026-10-20 (fixed in user space the same day)

## What the journal shows (02:31-06:44, lid closed 01:41-06:44)
- 30 "flick ->" toggles, 76 link drops ("bulb unreachable" or off-beat
  advert, same boot), ~260 failed connects (10 s timeouts). The bulb never
  lost power: adverts every 2-20 s throughout, and the boot count went on
  5 -> 38 across every link of the night. Every toggle was false.
- Each false toggle was decided without a count: the return's connects all
  failed ("boot check: no link") or the new link refused notifications
  (start_notify "Unlikely Error": "boot count unanswered"), and "unknown"
  fell back to "counted as power back" / "counted as a reboot".
- Connects that worked took 3-8 s (normally 0.3-3 s); several failed ones
  had the kernel link come up just as bleak gave up at 10 s (bluetoothd "No
  matching connection for device" at the cancel). RF was marginal, cause
  unknown: the previous nights with the lid closed had no failure at all,
  the 4C:B9:9B:13:B8:D7 page (every 60 s, all week) does not line up with
  the drops, and nothing else in the system log moved at 02:31. Controller:
  RTL8821CE (Wi-Fi and Bluetooth share the antenna). RSSI is now in the log.
- 12 of 76 drops came within 3 s of the 1 s supervision-timeout update:
  under weak radio the short timeout may itself drop links. Left as is (the
  flick path needs it); first candidate if drops stay frequent.
- Off-beat adverts 06:08:00/20/41: gaps 12.2-13.8 s from an advert 0-2 s
  after the connect. The bulb adverts once right after a connect, off the
  rhythm that follows; 5 of 42 off-beat adverts since 10-15 were this, each
  costing a reconnect (not our own disconnect: the check never judged an
  advert within SETTLED_S of the connect, only against one from then).

## Fixes
- A bulb whose counter answered before is decided on a read count only: no
  link or an unanswered Get keeps the return (and an off-beat suspect)
  waiting; each new advert tries one more link (a fresh one). Bulbs without
  a counter keep the old rules.
- Unanswered Gets still count on the bulb (03:10-03:11: two such links, then
  6 -> 10): each widens the next "same boot" / "fresh" window by 2.
- The grid reference must be an advert >= 3 s after the connect
  (CONNECT_ADVERT_S).
- Warnings (and the "connect failed" / "boot check: no link" info lines)
  repeat once per 10 min, then a "repeated N more times" line
  (haus/logrepeat.py).

## A3 / A2: not done
- A3 (kernel auto-connect) uses the same LE create-connection path that timed
  out tonight, so it would not have connected sooner, and it reconnects while
  idle. A2 (advertising-bearer arm) shortens the re-arm after a real flick;
  tonight had none. Neither is clearly beneficial: setup-version stays 3.
- A4 (live since 06:52): first connects 1.3 s; too few to compare yet.
