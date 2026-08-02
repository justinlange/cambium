# TESTING — what is proven where

```
.venv/bin/pytest tests/ -q          # the whole suite, ~3 s, no hardware
```

## Tiers

| Tier | Files | What it pins |
|---|---|---|
| golden parity | `test_packet_parity.py` + `tests/golden/packet_pins.json` | every struct size/offset == Ben's `test_packet_layout.cpp` (auto-extracted by `tools/extract_packet_goldens.py` with source sha; drift = CI failure saying "append a tail decoder, never reorder") |
| byte truth | `test_packets_roundtrip.py`, `test_cobs.py`, `test_framing.py`, `test_c_cobs_parity.py` | hand-packed expected bytes (never derived from the code under test); C `cobs.h` ⇄ Python `cobs.py` agree on shared vectors |
| translation | `test_normalize.py`, `test_packetize.py`, `test_scheduler.py` | clamp/white-extraction per class, stable TX partition, rate cap, latest-wins, stale→silence |
| uplink | `test_uplink_parse.py`, `test_fleetstate.py` | hb-short/full tail gating, signedness, charging edge-trigger |
| transport | `test_transport_*.py` | real COBS codec on the loopback, serial reconnect, single-writer |
| api | `test_ws_protocol.py`, `test_api_server.py`, `test_ops_cli.py` | WS vocabulary, err-not-disconnect, mapping preemption, doctor/blink/night over HTTP |
| fake fleet | `test_fakefleet_rx.py`, `test_fakefleet_hb.py` | the INDEPENDENT hand-rolled RX ladder vs the production builders — two implementations of packet.h that must agree; night gate, lease, slew, hold/stale ladder on a fake clock |
| e2e | `test_daemon_smoke.py`, `test_e2e_no_hardware.py`, `test_constellate_contract.py` | WS frame in → virtual RGBW out; exactly-one-lit sweeps; Constellate's vendored driver-contract against `/constellate/*` |
| mapping | `test_map_pipeline.py`, `test_make_sweep.py` | ground-truth recovery (5 cm at 5 mm noise), leave-one-out anchor blame, scale-gate refusals, calibration round-trip, Elliot-schema validation |

Idioms (borrowed from Constellate's suite): constructor-injected fake
clocks/transports with scripted behavior, ground-truth synthetics, full-stack
in-process e2e without ports, and CLI errors asserted to CONTAIN THEIR FIX.

Firmware: `resonance-hardware` branch `cambium-direct-frames` — `bash
firmware/fixture/tests/run_tests.sh` (native, -Werror) covers PROG_DIRECT
slew/hard-cut/ladder + the golden rows. The bridge sketch compile-gates via
`arduino-cli compile --fqbn esp32:esp32:esp32s3`.

Elliot branch `cambium-ws-bridge`: `npm run build` + `npm test` (263). The
`twin.spec` Playwright timeout pre-dates the branch (fails identically
without it).

## Cannot be tested without hardware

Each row names the runbook step that is its manual test
(QUICKSTART-BENCH10.md):

| Untestable in software | Covered by |
|---|---|
| radio PDR / range / channel congestion | doctor stage 4 census + the M6 rate rehearsal (`dl_pdr_x1000` vs tx_hz) |
| real lifecycle timing (dusk detection, night gate in the field) | step 3's night-gate box |
| power-budget brightness clamps under real batteries | step 7's "dimmer than the sim" row |
| identify visibility in daylight, camera exposure | Constellate's own `blink` + sweep |
| serial/USB quirks of the physical bridge | doctor stages 1–3 |
| PROG_DIRECT slew feel at 10 Hz (step 32 is a proposal) | M3/M4 bench session with Ben |
