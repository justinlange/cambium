# Architecture

## System picture

```
 resonance-lighting (browser twin)         Constellate (camera mapper)
        |         ^                                |
        | WS JSON | WS JSON (uplink:               | HTTP light(n)/all_off
        | frames  |  heartbeats, fleet state)      | during mapping sweeps
        v         |                                v
 +------------------------------------------------------------------+
 |                        cambium daemon                             |
 |                                                                  |
 |  api ---> normalize ---> downlink ---> transport ---> serial     |
 |  (WS/HTTP) (SimFrame ->  (FixtureFrame  (COBS framing)  port     |
 |             FixtureFrame) -> packets,                            |
 |             uses roster    scheduler)                            |
 |                                                                  |
 |  api <--- uplink <------------------- transport <--- serial     |
 |           (bytes -> JSON)                                        |
 +------------------------------------------------------------------+
        |
        v
   bridge PowerFeather (F2BED4) --ESP-NOW ch 11 broadcast--> ~118 fixtures
                                                             (10 on bench)
```

The wire contract is Ben's `resonance-hardware/firmware/fixture/src/core/packet.h`
(protocol v1, packed little-endian, append-only, types 1–24 assigned, 25+ free),
with golden sizes pinned in `firmware/fixture/tests/test_packet_layout.cpp`.
Cambium's `wire/` mirrors those structs byte-for-byte and re-pins the same
golden sizes in pytest.

## The three data representations

Every frame passes through three named forms. Each is a plain dataclass (or
bytes); each arrow is owned by exactly one module. No fused shortcuts.

| Form | What it is | Keyed by | Produced by |
|---|---|---|---|
| **SimFrame** | What the browser sends: float colors per fixture slot, untrusted, unclamped | `fixture_id` (F000...) | `api` (deserialized off the WS) |
| **FixtureFrame** | Canonical per-lantern color: RGBW, 8-bit, post-clamp, post-white-extraction | MAC (3-byte compact id) | `normalize` (owns SimFrame -> FixtureFrame, joins via `roster`) |
| **wire packets** | Packed protocol-v1 bytes — batched direct-frame packets (proposed `NbDirectFrame`, type from the free 25+ range, lands W4 with Ben's sign-off) plus existing types (`NbShowFrame`, `NbIdentify`, `NbProgramSet`, ...) | broadcast (+ target ids inside) | `downlink` (owns FixtureFrame -> packets, using `wire` codecs) |

`uplink` owns the reverse direction: wire bytes (`NbHeartbeat` short/full,
`NbChoreoState`) -> JSON for the browser and roster updates.

## Module responsibilities

| Module | Owns | Phase |
|---|---|---|
| `wire/` | struct <-> bytes for every protocol-v1 packet; golden-size tests against `test_packet_layout.cpp` | W1 (in progress) |
| `model.py` | SimFrame / FixtureFrame dataclasses and nothing else | W1 |
| `roster.py` | Who exists: fleet roster from heartbeats + `registry.csv`, `fixtures-map.json` (MAC <-> fixture_id <-> position), frozen sweep-roster snapshots | W1 |
| `normalize/` | SimFrame -> FixtureFrame: fixture_id -> MAC join, clamp to 8-bit, RGB -> RGBW white extraction | W2 |
| `downlink/` | FixtureFrame -> batched packets; the send scheduler that respects the rate constraints below; lease renewal | W2 |
| `uplink/` | Parsing inbound packets into fleet state + JSON events for the browser | W2 |
| `transport/` | COBS framing over the bridge's USB serial port (pyserial-asyncio); reconnect | W2 |
| `api/` | WS JSON server on :8600 for the simulator; HTTP endpoints for Constellate's `light(n)` sweep driver | W2 |
| `config.py` | Settings via tomllib (port, serial device, paths) | W2 |
| `daemon.py` | asyncio composition root wiring the above; the `cambium` entry point | W2 |
| `fakefleet/` | In-process fake fixtures implementing the firmware doctrine (night gate, ladder, leases) so the whole path runs with zero hardware | W3 |

Not in this repo: the `cambium_bridge` firmware sketch (serial <-> ESP-NOW,
replaces Constellate's untested reference sketch) is phase W4; the mapping
pipeline + CLI (`cambium sweep/ingest/align/assign/export/doctor/blink/night`)
is phase W5.

## Load-bearing constraints

These are facts about the fleet, verified in the hardware repo. Cambium designs
around them; it never argues with them.

| Constraint | Value | Why / source |
|---|---|---|
| Max packet size | **<= 145 B** on the wire | Fixture RX buffer is 192 B (`espnow_link.h RxItem.data[192]`) — longer packets are dropped; old net_bench masters still on the mesh buffer only 160 B (`net_bench.ino data[160]`). 145 B clears the smallest buffer with margin. |
| Fixture render rate | **10 Hz cap** | `fixture.ino renderTick()` renders at most every 100 ms. Sending faster than 10 Hz per fixture buys nothing. |
| Channel throughput | **~250 pkt/s practical ceiling** | 5-node feasibility test (`docs/tests/NETWORKING_FEASIBILITY_5NODE_2026-06-07.md`): loss grows ~linearly with aggregate rate (~97.8% PDR at 200 pkt/s, ~94.7% at 500). Batched whole-fleet frames at 5–10 Hz fit; per-fixture unicast streaming (118 x 10 Hz = 1180 pkt/s) does NOT. |
| Delivery model | **Broadcast, unacked** | Single unencrypted broadcast peer (encrypted peers cap at ~17). Any packet can be lost, so every packet must be independently meaningful — full state, never deltas that assume the last one arrived. |
| Silence ladder | **1 s hold -> 3 s autonomous fallback, 2 s crossfade, never blank** | `choreo/program.h`: `RES_SHOWFRAME_HOLD_MS 1000`, `RES_SHOWFRAME_STALE_MS 3000`, `RES_CHOREO_FADE_MS 2000`; fallback program is a class-tinted breathe, "never blank". Stop sending and the tree keeps living. |
| Identity | **Last 3 bytes of MAC** | `NbHeader.src_id[3]`; same ids key `registry.csv` (e.g. `9E5AE8` from `D8:85:AC:9E:5A:E8`). `00:00:00` target = all. |
| Night gate | **Show frames render only in `LIFE_NIGHT_SHOW`** | `lifecycle.cpp`: `showActive = (state == LIFE_NIGHT_SHOW) && tier <= 1`. Daytime fixtures ignore shows entirely; force-night override exists (serial `N1`, lifecycle `forceNight`). |
| Power cap | **Local power budget always wins** | ADR 0023 ladder (FULL -> DIM -> OFF -> PROTECT); `NbShowFrame.bright` is "clamped by local PowerBudget" (`packet.h`). Cambium's brightness is a request, not a command. |
| Channel | **ch 11, must match maintenance AP** | Fleet commissioned on channel 11; channel comes from NVS and "must equal the maintenance AP channel or ESP-NOW silently dies" (`espnow_link.h`). |
| Leases | **All overrides expire** | `NbProgramSet.lease_s` is a TTL from receipt; 0 releases. `NbShowFrame` flag bit0 is a 10 s micro-lease. Renew or relinquish — never assume persistent control. |
