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
