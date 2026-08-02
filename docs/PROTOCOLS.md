# PROTOCOLS — the three wire contracts

Single source of truth for what crosses each boundary. Byte layouts are
mirrored (and golden-tested) against Ben's `packet.h`; drift fails CI.

```
browser ── (1) WS JSON ──► cambium ── (2) COBS serial ──► bridge board ── (3) ESP-NOW ──► fleet
```

## 1. WS JSON — browser ⇄ cambium (`ws://host:8600/ws`)

One JSON object per text message. Extends the app's existing `bridge.ts`
vocabulary; unknown kinds get an `err` reply, malformed JSON never
disconnects.

**Down (browser → cambium)**

| kind | fields | becomes |
|---|---|---|
| `frame` | `seq`, `fixtures: [{id, rgb: [r,g,b] linear floats}]` | clamp → quantize → per-class white-extract → batched `NB_DIRECT_FRAME`s at ≤ tx_hz |
| `drive` | `on: bool` | arms/disarms the stream |
| `night` | `mode: 0\|1\|2`, `mac: string\|null` | `NB_FORCE_LIFECYCLE` |
| `program` | `mac?, programId, leaseS, seed?, hardCut?, params?` | `NB_PROGRAM_SET` |
| `show` | `phase, hue, flags` | `NB_SHOWFRAME` (22 B form) |
| `identify` | `mac\|null, seconds, color?, blink?` | `NB_IDENTIFY` (19 B form) |
| `set_rate` | `hbHz` | `NB_SET_RATE` |
| `ruleset` | accepted + logged only (no firmware support yet) | — |

**Up (cambium → browser):** `hb` (per heartbeat, HbFrame shape + appended
fields incl. roster `id`), `evt` (choreo-state edges), `charging`
(`{count, macs}`, on change — feeds `solarPanelsCharging`),
`bridge_status` (1 Hz), `err`.

**HTTP (same port):** `/healthz`, `/fleet`, `/bridge`,
`/constellate/light?led=N` + `/constellate/off` (mapping mode — preempts
sim frames, identify-based so it works on stock firmware in daylight;
60 s idle auto-release), `/debug/solid`, `/debug/night`, `/debug/identify`,
`/fakefleet/*` (when the fake fleet is attached).

## 2. Serial framing — cambium ⇄ cambium_bridge (USB CDC)

COBS-encoded frames, `0x00` delimited. Inside each frame:
`[ftype:u8][payload][crc16 LE]`, CRC-CCITT (poly 0x1021, init 0xFFFF) over
ftype+payload. Code: `cambium/wire/{cobs,framing}.py` ⇄
`firmware/cambium_bridge/cobs.h` (shared vectors in
`tests/golden/cobs_vectors.json`; C↔Python parity is a CI test).

| ftype | dir | payload |
|---|---|---|
| 0x01 RADIO_TX | host→bridge | complete raw Nb packet; bridge broadcasts it verbatim (≤ 250 B) |
| 0x02 RADIO_RX | bridge→host | `mac[6] + rssi:i8 +` raw received packet |
| 0x03 CTRL | host→bridge | `0x01` STATUS_REQ · `0x02` SET_CHANNEL(ch) · `0x03` REBOOT |
| 0x04 STATUS | bridge→host | `<B6sBIIIIIH16s`: proto=1, mac[6], channel, uptime_ms, tx_ok, tx_fail, rx_pkts, rx_drop, crc_err, fw[16] — at boot (HELLO) + 1 Hz |
| 0x05 LOG | bridge→host | ASCII debug (printf can never corrupt framing) |

The bridge is a **protocol-ignorant modem**: it never parses Nb packets, so
`packet.h` evolution never requires reflashing it. Cambium stamps every
`NbHeader` itself (src_id = the bridge's short id from HELLO, host-owned
monotonic seq) so fixture downlink-PDR accounting stays truthful.

## 3. ESP-NOW — protocol v1 (Ben's `packet.h`)

Broadcast FF:FF:FF:FF:FF:FF, channel 11, unencrypted, **unacked** — every
packet must be independently meaningful. Append-only evolution; golden
sizes pinned by `test_packet_layout.cpp` (his) and
`tests/golden/packet_pins.json` (ours, auto-extracted).

Existing types cambium sends: `NB_SHOWFRAME`(2), `NB_IDENTIFY`(6, 19 B
color form — `secs=0` cancels; renders in ANY lifecycle),
`NB_PROGRAM_SET`(19), `NB_SET_RATE`(5). Receives: `NB_HEARTBEAT`(1, hb-short
29 B / hb-full 148 B, tail-gated), `NB_CHOREO_STATE`(18).

**Proposed types (the `cambium-direct-frames` branch in resonance-hardware,
for Ben's review):**

```c
// 25: bridge -> all, batched per-fixture direct color
struct NbDirectEntry  { uint8_t id[3]; uint8_t r, g, b, w; };            // 7 B
struct NbDirectFrame  { NbHeader h;
                        uint8_t flags;   // bit0 = 10 s micro-lease grant
                                         // bit1 = hard-cut (skip slew)
                        uint8_t count;   // receiver: min(count, (len-15)/7)
                        NbDirectEntry entries[18]; };                     // 141 B
// wire length 15 + 7n; 118 fixtures = 7 packets/frame; 8 Hz = 56 pkt/s,
// well inside the ~250 pkt/s channel ceiling next to ~180 pkt/s of uplink.

// 26: bridge -> all/target, radio equivalent of serial 'N'
struct NbForceLifecycle { NbHeader h; uint8_t target_id[3];
                          uint8_t mode;   // 0=day 1=night 2=auto
                          uint8_t flags; };                               // 18 B
// RAM-only by design: a rebooting field unit must never stay forced.
```

Fixture-side: `PROG_DIRECT` renders entries naming its own id — micro-lease
+ hold(1 s)/stale(3 s) ladder identical to `ProgBridge`, slew 32/channel/
tick (hard-cut bypasses; step size flagged for Ben to tune), 1-px classes
set `px[0]`, the 37-px hex renders a uniform wash (v1). PowerBudget and the
night gate are untouched by construction. Old firmware ignores both types
silently (append-only doctrine).

## Constraints every layer respects

| Constraint | Value |
|---|---|
| payload ceiling | ≤ 145 B (fixture RX drops > 192; old master buffer 160) |
| fixture render | 10 Hz hard cap — cambium's tx_hz defaults to 8, never exceeds 10 |
| loss model | unacked broadcast; stale input > 2 s → cambium goes SILENT and the fixture ladder (hold → half → autonomous, never blank) takes over |
| authority | always an expiring lease; PowerBudget cap and night gate live on the fixture and always win |
| identity | last-3-MAC short id ("9E5AE8"); position is host data, never firmware |
