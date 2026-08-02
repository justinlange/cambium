# cambium

*The living layer between wood and bark.*

Cambium is the trunk between Elliot's browser lighting simulator
([resonance-lighting](../resonance-lighting)) and Ben's ESP-NOW lantern fleet
([resonance-hardware](../resonance-hardware)) — a Python daemon that accepts
simulator frames as WebSocket JSON, translates them through explicit named
stages into the fleet's packed wire protocol, and speaks them to a
serial-attached bridge PowerFeather that broadcasts to ~118 fixtures
(10 currently on the bench). It also serves
[Constellate](../Constellate)'s `light(n)` sweeps over HTTP so the camera
mapper can locate the fleet in 3D. Named for the thin living layer between
wood and bark: everything that grows passes through it.

## System diagram

```
 Elliot's simulator (browser)          Constellate (camera mapper)
 resonance-lighting, twin + shows      sweep control -> index -> (x,y,z)
        |                                        |
        | WS JSON :8600                          | HTTP  (mapping sweeps)
        v                                        v
 +--------------------------------------------------------+
 |              cambium daemon  (this repo)                |
 |   SimFrame  ->  FixtureFrame  ->  wire packets          |
 +--------------------------------------------------------+
        |
        | COBS-framed packets over USB serial
        v
   bridge PowerFeather  (F2BED4, registry role: serial_bridge)
        |
        | ESP-NOW ch 11 broadcast (unencrypted, unacked)
        v
   fixture fleet  (~118 lanterns, Ben's firmware, wire protocol v1)
```

## Start here

| You have | Read | Status |
|---|---|---|
| No hardware at all | `QUICKSTART-NO-HARDWARE.md` (fake fleet + simulator) | coming (phase W3) |
| The 10-lantern bench | `QUICKSTART-BENCH10.md` (bridge + real fixtures) | coming (phase W4/W5) |
| Questions about words | [docs/GLOSSARY.md](docs/GLOSSARY.md) — the three-repo term-collision table | here now |
| Questions about shape | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — data flow, modules, constraints | here now |

## Repo layout

```
cambium/
  wire/        struct <-> bytes for protocol v1 (mirrors packet.h, golden-size tested)
  normalize/   SimFrame -> FixtureFrame (map, clamp, white extraction)
  downlink/    FixtureFrame -> batched wire packets + send scheduler
  uplink/      heartbeats/choreo state off the wire -> JSON for the browser
  transport/   COBS framing over the bridge's USB serial port
  api/         WS JSON :8600 (simulator) + HTTP (Constellate sweeps)
  fakefleet/   in-process fake fixtures so everything runs with zero hardware
docs/          GLOSSARY.md, ARCHITECTURE.md
tests/         pytest, alongside each module
```

Planned single-file modules (see ARCHITECTURE for responsibilities): `model.py`
(the named frame dataclasses), `roster.py` (fleet roster + fixtures-map.json),
`config.py` (tomllib settings), `daemon.py` (asyncio composition root).

## Status

| Module | Status |
|---|---|
| docs (README, GLOSSARY, ARCHITECTURE) | built |
| wire | built + golden-parity tested (W1) |
| model, roster, config | built + tested (W1) |
| normalize, downlink, uplink, transport, api | built + tested (W2) |
| daemon, cli (`cambium serve`) | built + loopback smoke-tested (W2) |
| fakefleet (emulator, viewer, `cambium fakefleet run`, make-sweep) | built + tested (W3) |
| cambium_bridge firmware sketch | built, compile-gated + COBS parity (W4) |
| mapping pipeline (`cambium sweep/map ...`) | built + tested (W5) |
| ops CLI (`cambium doctor/blink/night/identify`) | built + tested (W5) |

## Design principles

- **Explicit named translation stages.** Every frame passes
  `SimFrame -> FixtureFrame -> wire packets`, one module per arrow, each stage a
  plain dataclass you can print. No stage is skipped, fused, or implicit.
- **Never fight the firmware.** The fleet's doctrine is the contract, not an
  obstacle (all verified in `resonance-hardware/firmware/fixture/src`):
  - *Leases expire.* Overrides are TTL leases (`NbProgramSet.lease_s`); cambium
    renews or the fixture walks away. Never assume persistent control.
  - *Silence means autonomous fallback.* >1 s without frames: hold + fade;
    >3 s: the fixture runs its own show. Cambium keeps the stream fed or
    accepts the fallback — it never "re-syncs" by blasting.
  - *The power cap always wins.* Brightness requests are clamped by each
    fixture's local power budget (ADR 0023 ladder). Cambium requests; the
    lantern decides.
  - *The night gate is real.* Fixtures ignore show frames outside
    `LIFE_NIGHT_SHOW`. A dark bench is usually daytime, not a bug — see
    GLOSSARY "night" and `cambium night on` (W5).
