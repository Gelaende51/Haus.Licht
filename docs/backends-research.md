# Backends: research for implementers

Research for the backend layer. Library figures were read from PyPI and GitHub on
2026-10-16; protocol facts come from the vendors' docs or the libraries' sources.
Re-check versions before pinning.

Constraints that shape every backend:

- **We send on every screen-brightness change.** Every backend coalesces: keep only
  the latest target per device, send at most once per backend-specific interval, and
  always send the final value. Rate limits are listed per backend below.
- **Tests run without hardware.** Every backend needs a fake: an emulator, a mock
  server, or recorded fixtures replayed against the library's own fake protocol.
- **Licence:** the project is AGPL-3.0. MIT, BSD, Apache-2.0, LGPL-3.0, GPL-3.0(+),
  EPL-2.0-or-BSD and UPL-1.0 can all be combined with it. No library below is a
  problem. (Apache-2.0 is compatible with GPLv3/AGPLv3 but not GPLv2. That does not
  matter here.)
- **Python:** every library listed as recommended supports 3.12 and 3.13. Exceptions
  are marked.
- **Power-on behaviour** means the equivalent of Bluetooth mesh Generic OnPowerUp
  (off / on / restore) that we already arm on the Ledvance bulb. It only works when
  the device can be told *locally*.

## Library snapshot

| Package | Latest | Released | Licence | Python | Runtime deps (non-extra) | Status |
|---|---|---|---|---|---|---|
| aiohttp | 3.14.5 | 2026-10-11 | Apache-2.0 AND MIT | ≥3.10 | 9 small aio-libs pkgs | base for all HTTP backends |
| hass-client | 1.4.1 | 2026-10-12 | Apache-2.0 | ≥3.11 | aiohttp | active (Music Assistant), 20★ |
| homeassistant-api | 6.1.0 | 2026-09-01 | GPL-3.0-or-later | ≥3.11 | niquests[ws], pydantic 2, simplejson | active, heavy |
| aiomqtt | 2.5.1 | 2026-03-05 | BSD-3-Clause | ≥3.8 | paho-mqtt 2.1 (EPL/BSD), typing-ext | active |
| aiohue | 4.9.0 | 2026-07-30 | Apache-2.0 | ≥3.11 | aiohttp, asyncio-throttle, awesomeversion | active (HA libs) |
| python-matter-server | 8.1.2 | 2025-12-15 | Apache-2.0 | ≥3.12 | … chip-clusters | **archived 2026-06-23, final release** |
| matter-python-client | 1.4.0 | 2026-08-07 | Apache-2.0 | ≥3.12 | aiohttp, dacite, orjson | active (client for matterjs-server) |
| pywizlight | 0.6.6 | 2026-08-05 | MIT | ≥3.11 | click | active |
| aiolifx | 1.2.2 | 2026-05-26 | MIT | ≥3.4 | async_timeout, bitstring, ifaddr, click<8.2, InquirerPy | active, CLI deps leak into runtime |
| lifx-emulator (test) | 4.7.2 | 2026-10-05 | UPL-1.0 | ≥3.10 | – | active |
| yeelight | 0.7.16 | 2025-01-28 | BSD | ≥3.4 | ifaddr, importlib_metadata, async-timeout | slow (GitLab, last activity 2026-08), has `AsyncBulb` |
| govee-local-api | 3.1.0 | 2026-09-08 | Apache-2.0 | ≥3.11 | none | active |
| python-kasa | 0.11.0.1 | 2026-10-04 | GPL-3.0-or-later | ≥3.11 | aiohttp, asyncclick, cryptography, mashumaro, tzdata | active, 1.7k★ |
| tinytuya | 1.20.0 | 2026-07-06 | MIT | (any) | requests, colorama, cryptography | active, **not asyncio** |
| aioshelly | 13.35.0 | 2026-10-15 | Apache-2.0 | ≥3.11 | aiohttp, bleak-retry-connector, habluetooth, zeroconf, orjson, … | active, heavy (BLE stack) |
| hatasmota | 0.10.2 | 2026-09-02 | MIT | **≥3.13** | voluptuous, aiohttp | active, HA discovery helper only |
| aioesphomeapi | 46.12.1 | 2026-10-15 | MIT | ≥3.11 | protobuf, zeroconf, cryptography, noiseprotocol, … (11) | active |
| bleak | 3.0.2 | 2026-05-02 | MIT | ≥3.10 | dbus-fast on Linux | active |
| HueBLE | 2.2.3 | 2026-08-21 | MIT | ≥3.11 | bleak, bleak-retry-connector | active (HA `hue_ble`) |
| led-ble | 1.1.12 | 2026-07-26 | Apache-2.0 | ≥3.10 | bleak, bleak-retry-connector, flux-led (LGPL-3.0) | active (HA) |
| amqtt (test) | 0.12.1 | 2026-09-16 | MIT | ≥3.10 | – | active, in-process MQTT broker |

## Home Assistant

- **Library:** plain aiohttp. REST is a single POST, and the WebSocket API is a short
  JSON protocol. `hass-client` (Apache-2.0, aiohttp only) is a fine alternative if
  it saves code. Skip `homeassistant-api`: pydantic + niquests for two calls.
- **Discovery:** mDNS `_home-assistant._tcp.local.` (zeroconf), or the user types the URL.
- **User supplies:** base URL and a long-lived access token (Profile → Security), and
  picks the entity ids (`light.*`).
- **Set:** `POST /api/services/light/turn_on` `{"entity_id": "...", "brightness_pct": 0–100, "transition": s}`;
  `light/turn_off`. Over WebSocket: `{"type": "call_service", "domain": "light", "service": "turn_on", ...}`.
  `brightness` (0–255) is also accepted. `brightness_pct: 0` turns the light off. The
  minimum lit level depends on the underlying device: 1 % maps to 3/255.
- **Latency/rate:** HA forwards each call to the integration, so the device's limit
  applies (Hue, Zigbee and Yeelight behind HA keep their limits). Use one persistent
  WebSocket, not REST per change. Coalesce to about 5–10/s per entity.
- **Push:** yes. WebSocket `subscribe_entities` (compact state diffs) or
  `subscribe_events` `state_changed`.
- **Power-on:** not generic. Some integrations expose it as a `select` entity
  (zigbee2mqtt/ZHA: `select.<light>_power_on_behavior`; Shelly, Tasmota:
  device-specific). The backend can offer it when such an entity exists.
- **Tests:** a mock aiohttp server that speaks `auth_required → auth → auth_ok →
  result/event` (about 50 lines). Optional integration job: the `ghcr.io/home-assistant/home-assistant`
  container with `demo:` in `configuration.yaml` provides demo lights (onboarding has
  to be scripted to get a token).

## MQTT (generic) + zigbee2mqtt

- **Library:** `aiomqtt` (BSD, wraps paho-mqtt 2.x).
- **Discovery:** the broker is user-supplied. z2m lists devices on retained
  `zigbee2mqtt/bridge/devices` (JSON with `exposes`), so lights are filtered by
  the `light` expose with a `brightness` feature. Generic MQTT has no discovery, unless
  the devices publish HA discovery (`homeassistant/light/.../config`), which we can read.
- **User supplies:** broker host:port, optional user/password/TLS, base topic
  (default `zigbee2mqtt`), friendly names. For generic MQTT: command/state topics and
  a payload template.
- **Set (z2m):** publish to `zigbee2mqtt/<name>/set` `{"state": "ON", "brightness": 1–254, "transition": 0.2}`.
  The state arrives on `zigbee2mqtt/<name>` (JSON). Brightness is 0–254 (Zigbee
  Level Control). 1 is the minimum lit level, but many bulbs clamp to their own
  minimum. `brightness_percent` is not a z2m set field: scale it yourself.
- **Power-on:** z2m exposes `power_on_behavior` on most Zigbee 3.0 bulbs, values
  `off | on | toggle | previous`. Set it with `{"power_on_behavior": "off"}` on
  `/set`. This writes the ZCL StartUpOnOff attribute on the bulb itself, so it is the
  exact counterpart of OnPowerUp. Check the device's `exposes` before offering it.
- **Latency/rate:** a Zigbee network can take only a few commands per second per
  device before queues build up (z2m retries, and latency grows to seconds). Coalesce
  to ≤ 4–5/s per light, prefer `transition`, and send to a z2m **group** for several
  bulbs (one multicast instead of N unicasts).
- **Push:** yes (state topic, `/availability` if z2m availability is enabled).
- **Tests:** `amqtt` broker in-process (MIT, pure Python) or a `mosquitto` service
  container. A small fake-z2m task subscribes to `+/set` and echoes state. No hardware.

## Philips Hue Bridge (API v2 / CLIP v2)

- **Library:** `aiohue` (Apache-2.0, `HueBridgeV2`, the library HA's Hue integration
  uses). Plain aiohttp is also feasible, since CLIP v2 is plain REST + SSE.
- **Discovery:** mDNS `_hue._tcp.local.` (local). The fallback `discovery.meethue.com`
  is a cloud lookup and should not be the default.
- **User supplies:** a bridge IP, then a press of the bridge **link button**, then
  `POST /api` `{"devicetype": "hauslicht#host", "generatekey": true}` returns the app
  key (`hue-application-key` header). HTTPS with a self-signed bridge certificate.
- **Set:** `PUT /clip/v2/resource/light/<id>` `{"on": {"on": true}, "dimming": {"brightness": 0.0–100.0}, "dynamics": {"duration": ms}}`.
  The light reports `dimming.min_dim_level` (lowest lit %). Grouped lights:
  `grouped_light/<id>`.
- **Latency/rate:** Signify's guidance is about 10 light commands/s per bridge total
  and about 1/s for grouped lights. Above that the bridge answers 429 or drops
  commands. aiohue throttles internally (asyncio-throttle). Coalesce to ≤ 5/s overall.
- **Push:** yes, SSE `GET /eventstream/clip/v2` (aiohue subscribes).
- **Power-on:** yes, locally. `light.powerup` has `preset`: `safety` (full
  bright/2700 K), `powerfail` (restore, but off stays off), `last_on_state`, `custom`
  (with `on.mode`: `on | toggle | previous` and `dimming`). Writable with the same PUT.
  Only on newer firmware/models, and `powerup` is absent when unsupported. The field
  names come from the openhue OpenAPI spec: verify on a real bridge before shipping.
- **Tests:** `aiohue`'s test fixtures (JSON resource dumps) behind a mock aiohttp
  server. Optional emulator: **diyHue** (Docker `diyhue/core`; has `v2restapi.py` and
  an event streamer). Its v2 coverage is partial, so use it for smoke tests only.

## Matter

- **Library:** `python-matter-server` is **archived (2026-06-23, 8.1.2 final)**. Its
  successor is **matterjs-server** (Open Home Foundation, Node ≥ 22, `npx matter-server`
  or Docker `ghcr.io/matter-js/matterjs-server:stable`, beta, not yet CSA-recertified).
  Its WebSocket API (`ws://host:5580/ws`) is meant to stay compatible. Python client:
  `matter-python-client` 1.4.0 (Apache-2.0, aiohttp/dacite/orjson, py ≥ 3.12).
- **What the user must run:** a Matter controller. Either the matterjs-server (or the
  HA Matter add-on, which is the same server: then point us at its WS port) or Home
  Assistant itself (then use the HA backend instead). Wi-Fi Matter bulbs need BLE
  commissioning once. Thread bulbs need a Thread border router. The setup code
  (QR/11-digit) commissions the bulb into the controller's fabric. A bulb can be in
  several fabrics (multi-admin), so it can stay in Google/Apple/Hue as well.
- **Discovery:** handled by the controller (DNS-SD `_matter._tcp`). We list nodes over WS.
- **Set:** OnOff cluster (0x0006) On/Off. Level Control (0x0008) `MoveToLevelWithOnOff`,
  level 1–254 (`MinLevel` attribute, usually 1), transition in tenths of a second.
- **Latency/rate:** subscriptions, no polling. Thread bulbs are slower (tens to
  hundreds of ms). Coalesce to about 5/s.
- **Push:** yes (attribute subscriptions forwarded as WS events).
- **Power-on:** yes, standard and local. OnOff `StartUpOnOff` (0 off, 1 on, 2 toggle,
  null = previous) and Level Control `StartUpCurrentLevel`, written via the
  controller's write-attribute command. This is a direct OnPowerUp equivalent.
- **Tests:** a mock WS server replaying matter-server messages for unit tests.
  Integration: a **virtual Matter light** commissioned into a real matterjs-server in
  CI: matter.js's example light device (`@matter/examples`, Node), or
  connectedhomeip's `chip-lighting-app` on Linux. Both run on-network without hardware.
- **Verdict:** experimental. It works, but it depends on a beta Node server we do not
  control. Through HA it is solid.

## WiZ

- **Library:** `pywizlight` (MIT, asyncio, only dep `click`).
- **Discovery:** UDP broadcast `getPilot`/registration to port 38899 on the LAN
  broadcast address. Cloud-free.
- **User supplies:** nothing (optionally the IP). Local control works out of the box
  (the app's "Allow local communication" must not be disabled).
- **Set:** UDP JSON `{"method": "setPilot", "params": {"state": true, "dimming": 10–100}}`.
  **Firmware rejects or clamps dimming < 10**, so 10 % is the minimum lit level. Map
  our 1–100 onto 10–100 or clamp, and document the jump.
- **Latency/rate:** UDP, about 10–50 ms. No documented limit. Bulbs drop packets when
  flooded, so coalesce to about 5–10/s and re-send the final value once.
- **Push:** yes. After `registration` the bulb sends `syncPilot` heartbeats/changes
  to our port 38900.
- **Power-on:** no documented local API (it is set in the app/cloud). Not offered.
- **Tests:** `pywizlight/tests/fake_bulb.py` is a fake UDP bulb server used by
  pywizlight's own tests, with several firmware profiles. Reuse it.

## LIFX (LAN)

- **Library:** `aiolifx` (MIT, asyncio). It pulls `click` and `InquirerPy` at runtime.
  Plain UDP is also small: the header is 36 bytes, plus SetLightPower (117), SetColor
  (102) and GetColor/State (101/107). Choose plain UDP if the deps bother us.
- **Discovery:** UDP broadcast GetService (2) to port 56700. Cloud-free, no setup.
- **User supplies:** nothing.
- **Set:** `SetLightPower` level 0/65535 + duration ms. `SetColor` HSBK, brightness
  **0–65535** (16-bit). Keep hue/saturation/kelvin from the last `State`. Any
  brightness > 0 is lit. The lowest visible level is device-dependent (about 1 %).
- **Latency/rate:** LIFX recommends **≤ 20 messages/s per device**, and excess
  causes erratic behaviour. Coalesce to about 10/s. Use `ack_required` only for the
  final value.
- **Push:** no (poll `GetColor`). Bulbs do not report changes made by others.
- **Power-on:** no LAN message. A bulb always comes up on, in its last colour.
- **Tests:** **`lifx-emulator`** (UPL-1.0, 4.7.2, active), a standalone emulator with
  an HTTP management API. Also a unit-level fake UDP server.

## Yeelight (LAN)

- **Library:** `yeelight` (BSD, has `yeelight.aio.AsyncBulb`, used by HA). The last
  PyPI release is from 2025-01, but the repo is alive. Plain asyncio TCP is also small
  (newline-delimited JSON). Skip `aioyeelight`: dead since 2020.
- **Discovery:** SSDP-like multicast `M-SEARCH` to 239.255.255.250:**1982**
  (`ST: wifi_bulb`). Cloud-free.
- **User supplies:** **"LAN Control" must be enabled per bulb in the Yeelight app.**
  Newer models (2022+) have lost or limited it on some firmware: check per model.
- **Set:** TCP 55443 JSON-RPC `set_power ["on","smooth",200]`,
  `set_bright [1–100,"smooth",200]`. 1 is the minimum lit level.
- **Latency/rate:** **quota of 60 commands/min per bulb**, after which it answers
  "client quota exceeded" (even for `get_prop`). That is fatal for brightness
  following. **Music mode** (`set_music [1, host, port]`) makes the bulb connect back to
  our TCP server with no quota, but it also sends no property notifications. Use music
  mode while following, or coalesce to ≤ 1/s.
- **Push:** yes, `props` notifications on the control connection (not in music mode).
- **Power-on:** partial. `set_default` saves the *current* state as the power-on
  state. There is no off/restore choice over LAN.
- **Tests:** a mock asyncio TCP server (JSON lines), about 60 lines. No maintained
  emulator found.

## Govee (LAN API)

- **Library:** `govee-local-api` (Apache-2.0, zero deps, asyncio, HA's `govee_light_local`).
- **Discovery:** UDP multicast `scan` to 239.255.255.250:**4001**. Devices reply to our
  port **4002**, and commands go to the device's port **4003**. Cloud-free.
- **User supplies:** enable **"LAN Control"** per device in the Govee Home app (only
  supported models, listed in Govee's LAN API docs).
- **Set:** `{"msg": {"cmd": "turn", "data": {"value": 0|1}}}`,
  `{"cmd": "brightness", "data": {"value": 1–100}}`.
- **Latency/rate:** UDP, fire-and-forget, no ack. No documented limit, but devices
  drop bursts. Coalesce to about 5/s and confirm with `devStatus`.
- **Push:** no (poll `devStatus`).
- **Power-on:** not via LAN.
- **Tests:** a mock UDP device (the library's `tests/` show the message formats).

## TP-Link Kasa + Tapo

- **Library:** `python-kasa` (GPL-3.0-or-later, so fine with AGPL; asyncio; HA's
  `tplink`). One library covers both the legacy Kasa (IOT) and the new SMART/Tapo
  protocol.
- **Discovery:** UDP broadcast on **9999** (legacy XOR protocol) and **20002** (new
  protocol). Local.
- **User supplies:** older Kasa: nothing. **Newer Kasa firmware and all Tapo: the
  TP-Link cloud username + password.** They are used locally to derive the KLAP/AES
  handshake key, and the device must have been set up through the app with that
  account. Store them like a token.
- **Set:** `light.set_brightness(1–100)`, `turn_on/turn_off`, with optional
  `transition` (ms) on Kasa bulbs. 1 is the minimum lit level.
- **Latency/rate:** TCP/HTTP request–response, about 50–200 ms each. Tapo's handshake
  is costly, so reuse the session. Coalesce to ≤ 3–4/s.
- **Push:** no (poll `update()`).
- **Power-on:** Kasa IOT bulbs: `default_behavior` (hard_on/soft_on with `last_status`
  or a preset), in the library's `iotbulb.py`. Tapo bulbs: `default_states`
  (`last_states` / custom) appears in fixtures, but there is no typed API. Treat it as
  experimental.
- **Tests:** strong. `tests/fakeprotocol_iot.py`, `fakeprotocol_smart.py` plus
  hundreds of real device dumps in `tests/fixtures/` (e.g. KL110, L530). Inject the fake
  protocol, no hardware.

## Tuya (local)

- **Library:** `tinytuya` (MIT, active, but **synchronous sockets, no asyncio**). Run
  it in a dedicated thread (`asyncio.to_thread` / one worker per device with a
  persistent socket).
- **Discovery:** UDP broadcast listening on 6666/6667/7000 (devices announce
  themselves). Control is TCP 6668.
- **User supplies:** **device id + local key + protocol version (3.1–3.5) per
  device.** The local key needs a Tuya IoT developer account: `python -m tinytuya wizard`
  with API id/secret from iot.tuya.com, linked to the Smart Life app. The "IoT Core"
  trial expires monthly and must be renewed to fetch keys for new devices. The key
  changes when the device is re-paired. This is the biggest setup burden of all backends.
- **Set:** data points. On v2 bulbs: DP 20 switch, DP 21 mode (`white`), DP 22
  brightness **10–1000**. Older "type A" bulbs use DP 1/2/3 with 25–255. Detect per
  device from the DP map.
- **Latency/rate:** **one TCP connection at a time per device** (the Smart Life app
  locks us out while open). tinytuya advises against polling faster than 1/s.
  Coalesce to about 2–3/s on a persistent socket.
- **Push:** partial. With a persistent socket the device sends status updates (DPS) on
  change.
- **Power-on:** some bulbs have a "relay_status"/power-on DP. It varies per product and
  is not standard.
- **Tests:** unit only (mock socket with recorded encrypted frames).
- **Verdict:** experimental: key provisioning, protocol versions and DP maps vary per
  product.

## Shelly (Gen1 + Gen2/3/4)

- **Library:** **plain aiohttp.** `aioshelly` (Apache-2.0) pulls the HA Bluetooth stack
  (habluetooth, bleak-retry-connector, zeroconf, orjson), which is too heavy for two
  HTTP calls. Use its `fixtures/` as test data.
- **Discovery:** mDNS `_shelly._tcp.local.` (Gen2+) / `_http._tcp` with a `shelly*`
  hostname (Gen1). Gen1 also announces over CoIoT (CoAP multicast 224.0.1.187:5683).
- **User supplies:** IP/host. A password if device auth is enabled (Gen1 basic auth,
  Gen2+ digest auth).
- **Set:**
  - Gen1 (Shelly **Duo**, **Vintage**, Dimmer 1/2, Bulb):
    `GET /light/0?turn=on&brightness=1–100&transition=ms`.
  - Gen2+ (Plus Wall Dimmer, Dimmer Gen3/Gen4, Pro Dimmer):
    RPC `Light.Set {"id": 0, "on": true, "brightness": 0–100, "transition_duration": s}`
    over HTTP `/rpc` or a WebSocket.
  - The minimum lit level is device-calibrated. Gen2 `min_brightness_on_toggle` and the
    dimmer's own calibration apply.
- **Latency/rate:** local HTTP about 20–50 ms. Gen2 RPC tolerates several calls/s.
  Gen1 (ESP8266) is weaker: keep ≤ 3–4/s and don't open parallel connections.
- **Push:** Gen2+: WebSocket `/rpc` with `NotifyStatus` frames for `light:0`. Gen1:
  CoIoT multicast status.
- **Power-on:** yes, locally. Gen2+ `Light.SetConfig {"config": {"initial_state": "off|on|restore_last|toggle"}}`.
  Gen1 `GET /settings/light/0?default_state=off|on|last|switch`.
- **Tests:** a mock aiohttp server driven by `aioshelly`'s `fixtures/` (real device
  dumps). No official emulator.

## Tasmota

- **Library:** plain aiohttp (HTTP) or aiomqtt (MQTT, shared with the MQTT backend).
  `hatasmota` is a discovery helper for HA and requires **Python ≥ 3.13**: not needed.
- **Discovery:** none built in (mDNS is off by default in many builds). Over MQTT,
  devices publish `tasmota/discovery/<mac>/config` (retained) when `SetOption19 0`,
  which we can read.
- **User supplies:** host (+ web user/password if set) or MQTT topic.
- **Set:** `GET /cm?user=..&password=..&cmnd=Dimmer%2050`, `cmnd=Power%20ON|OFF`.
  `Dimmer` 0–100. `DimmerRange` maps to the hardware. `Fade 1` + `Speed n` for
  transitions. MQTT: `cmnd/<topic>/Dimmer 50`.
- **Latency/rate:** fast (ESP, local). Over HTTP keep ≤ 5/s with one connection. MQTT
  handles bursts better.
- **Push:** MQTT `stat/<topic>/RESULT` and `tele/<topic>/STATE`. HTTP has none (poll
  `Status 11`).
- **Power-on:** yes, locally: `PowerOnState 0` off, `1` on, `2` toggle, `3` last saved
  (default), `4` on and lock, `5` PulseTime-inverted.
- **Tests:** a mock HTTP server for `/cm`, and the amqtt broker for MQTT. No emulator needed.

## ESPHome

- **Library:** `aioesphomeapi` (MIT, asyncio, HA's ESPHome integration). Deps are
  moderate (protobuf, zeroconf, cryptography, noiseprotocol).
- **Discovery:** mDNS `_esphomelib._tcp.local.`.
- **User supplies:** host and the **API encryption key** (`api: encryption: key:`,
  base64 32 bytes, Noise protocol). Legacy plaintext-password devices are deprecated.
- **Set:** `client.light_command(key, state=True, brightness=0.0–1.0, transition_length=s)`.
  The minimum lit level is device-config (`gamma_correct`, `min_power` on the output).
- **Latency/rate:** persistent TCP (6053) with push. Handles about 10/s fine.
- **Push:** yes (`subscribe_states`).
- **Power-on:** **compile-time only** (`restore_mode: ALWAYS_OFF | RESTORE_DEFAULT_OFF | …`
  in YAML), not settable over the API. Not offered.
- **Tests:** **ESPHome `host` platform**. `esphome run light.yaml` compiles a native
  Linux binary serving the real native API on port 6053 (no Wi-Fi, no mDNS; add by IP).
  Whether light entities are fully supported on `host` must be tried. A template
  light/`output: template` should work. Unit tests: aioesphomeapi's own mocks.

## Other Bluetooth SIG-mesh bulbs

- **Library:** our own `btmesh` code, as for Ledvance. Generic SIG-mesh bulbs use the
  same Generic OnOff (0x1000), Light Lightness (0x1300) and Generic OnPowerUp
  (0x1006/0x1007) models.
- **Standard scale vs Ledvance quirk:** the spec defines Light Lightness as
  **0–65535** (actual, perceptually linear). Ledvance treats it as **0–100** (see
  `dev/takeaways.md`). **Detection per device, at provisioning/setup:**
  1. **Light Lightness Range Get (0x8257 → Status 0x8258)**. Ledvance reports
     `range_min 1, range_max 100`. A standard bulb reports a max near 65535 (often
     `1–65535`). This is the primary signal and is read-only.
  2. Composition Data **CID**. Ledvance is `0x052e` (PID `0x0006` seen here). Keep a
     quirk table keyed by CID/PID as an override.
  3. As a fallback, store `lightness_scale` per device in config (`100 | 65535`) so a
     user can correct it.
- **Discovery:** unprovisioned beacons / proxy advertisements (`0x1827`/`0x1828`), as today.
- **Power-on:** standard Generic OnPowerUp (already implemented) and Light Lightness
  Default (0x8259 set) for the level.
- **Rate:** the mesh proxy link is the limit, as with Ledvance.
- **Tests:** unit-level. Feed recorded access-layer PDUs to the scale detector. No
  emulator. (Zephyr's `bluetooth/mesh` light-server samples on `native_sim` could
  serve as one, but that is heavy.)
- **Caution:** many cheap "BLE mesh" bulbs are **Tuya BLE mesh or Telink proprietary
  mesh, not SIG mesh**, and will not provision with our stack.

## Other BLE bulbs

| Family | Library | Usable? | Notes |
|---|---|---|---|
| Philips Hue Bluetooth (GATT) | `HueBLE` 2.2.3 (MIT, bleak) | yes | Used by HA `hue_ble`. The bulb must be made discoverable/paired (Hue app "voice assistant" pairing or a factory reset). Brightness 1–254. One BLE connection per bulb: conflicts with our mesh proxy connection only if on the same adapter at the same time. |
| Magic Home / Triones / LEDBlue / QHM / "Dream~" | `led-ble` 1.1.12 (Apache, pulls flux-led LGPL-3.0) | yes | Advert name prefixes `Triones:`, `LEDBLE-`, `LEDBlue-`, `QHM-`, `Dream~`. "LEDnetWF" Magic Home Wi-Fi/BLE devices are **not** listed and are covered by flux-led over Wi-Fi instead. |
| Govee BLE lights | none maintained | no | `govee-ble` is sensors only. Lights only via the Govee LAN API above. |
| Yeelight BT / ALLOYSEED | – | no | Explicitly excluded by led-ble, no maintained lib. |

BLE tests: unit only (bleak mocked). No BLE emulator is practical in CI.

## Desktop signals

Verified on this machine (KDE Plasma / KWin 6.7.5), read-only.

### KWin Night Light: `org.kde.KWin.NightLight` at `/org/kde/KWin/NightLight` (session bus)

Properties (all `emits-change`, i.e. `org.freedesktop.DBus.Properties.PropertiesChanged`):

| Property | Type | Value read now (≈03:00) | Meaning (from `nightlightmanager.cpp`) |
|---|---|---|---|
| `available` | b | true | Night Light supported on this output setup |
| `enabled` | b | true | User setting "Night Light on" |
| `inhibited` | b | false | Temporarily blocked (inhibit() reference count > 0, e.g. the tray toggle) |
| `running` | b | true | `(enabled \|\| temporarily activated) && !inhibited`, i.e. the feature is active, **day or night** |
| `daylight` | b | false | "Night Light is currently on day time". In DarkLight mode: past the morning transition and not yet past the evening one (true already during the morning ramp). Constant mode: always false. Not active: true |
| `mode` | u | 1 | enum `Constant = 0`, `DarkLight = 1` (time-of-day schedule) in Plasma 6.7. Older Plasma had Automatic/Location/Timings/Constant: read it, don't hard-code |
| `currentTemperature` / `targetTemperature` | u | 2300 / 2300 | Kelvin now / where the current transition ends |
| `previousTransitionDateTime` / `scheduledTransitionDateTime` | t | 1792090800 / 1792130400 | Unix seconds: 2026-10-15 21:00 and 2026-10-16 08:00 local |
| `previousTransitionDuration` / `scheduledTransitionDuration` | u | 1800000 | ms (30 min ramps) |

Methods: `inhibit() → u cookie`, `uninhibit(u)`, `preview(u kelvin)`, `stopPreview()`.

So **`running` does not mean "it is night"**. Use `daylight == false` (or
`currentTemperature < 6500`) for "evening/night", and the two transition timestamps
for scheduling a bulb colour/brightness ramp. Subscribe to `PropertiesChanged` on that path.

### Dark / light colour scheme: XDG portal Settings

```
busctl --user call org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop \
  org.freedesktop.portal.Settings ReadOne ss org.freedesktop.appearance color-scheme
→ v u 1        # 0 = no preference, 1 = prefer dark, 2 = prefer light
```

Read now: **1 (dark)**. The interface (`version` 2) has `ReadOne`, `ReadAll`,
deprecated `Read`, and the signal **`SettingChanged(s namespace, s key, v value)`**.
Per the portal spec and xdg-desktop-portal-kde, it fires with
`("org.freedesktop.appearance", "color-scheme", <u>)` when the global theme or colour
scheme switches between dark and light. **Not exercised here (we did not switch the
scheme)**, so confirm once with `busctl --user monitor org.freedesktop.portal.Desktop`
while switching. Use `ReadOne` (not `Read`, which double-wraps the variant on old portals).

## Recommendation

| Backend | Library | Tier | Test strategy |
|---|---|---|---|
| Home Assistant | plain aiohttp (REST + WS) | solid | mock WS server; optional HA container with `demo` lights |
| MQTT / zigbee2mqtt | aiomqtt | solid | amqtt in-process broker + fake z2m echo |
| Philips Hue Bridge v2 | aiohue | solid | mock server with aiohue fixtures; diyHue smoke test |
| Shelly Gen1/Gen2+ | plain aiohttp | solid | mock server with aioshelly fixtures |
| Tasmota | plain aiohttp (+ aiomqtt) | solid | mock `/cm` server; amqtt |
| ESPHome | aioesphomeapi | solid | emulator: ESPHome `host` platform binary |
| TP-Link Kasa/Tapo | python-kasa | solid (Kasa) / experimental (Tapo power-on) | library's fake protocols + device fixtures |
| WiZ | pywizlight | solid | pywizlight `fake_bulb.py` |
| LIFX LAN | aiolifx or plain UDP | solid | emulator: lifx-emulator |
| Govee LAN | govee-local-api | experimental | mock UDP device |
| Yeelight LAN | yeelight (AsyncBulb), music mode | experimental | mock TCP server |
| Matter | matter-python-client + matterjs-server | experimental | mock WS; emulator: matter.js example light / chip-lighting-app |
| Tuya local | tinytuya (in a thread) | experimental | unit only (mock socket) |
| Other SIG-mesh bulbs | own btmesh | experimental | unit only (recorded PDUs) |
| Hue Bluetooth | HueBLE | experimental | unit only (bleak mocked) |
| Triones/LEDBlue BLE | led-ble | experimental | unit only (bleak mocked) |
| Govee BLE lights | – | not worth doing | – |

Not worth doing on their own: Govee BLE lights (no library), and Matter *without* a
controller the user already runs (HA covers it better). Yeelight is only worth it with
music mode, because the 60/min quota rules out normal brightness following.
