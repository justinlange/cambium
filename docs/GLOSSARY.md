# Glossary — the term-collision table

Three sibling projects grew the same words with different meanings; this table
exists so a cambium reader can translate on sight. The **cambium** column is
canonical inside this repo — when cambium code or docs say a bare term, they
mean that column.

| Term | Constellate | resonance-hardware | resonance-lighting | cambium (canonical) |
|---|---|---|---|---|
| **fixture** | An LED/node — the thing `light(n)` turns on; just an index in a sweep | A board + attached loads unit; its class (downlight/perimeter/uplight/...) is probed from attached I2C hardware at boot (ADR 0009, `class_probe`) | A `fixtures.json` slot like `F000`, with authored Blender position/aim/role — a place, not a device | A physical lantern, keyed by MAC (3-byte compact id), optionally assigned a `fixture_id` slot |
| **index** | The sweep address: `light(n)` lights index *n*; output is `index -> (x,y,z)` | (not used as an identity) | Array position in the fixtures list / twin buffers | Row *n* of a frozen sweep-roster snapshot; exists only for the duration of one sweep, then dies with it |
| **bridge** | `firmware/espnow_bridge/` — a reference sketch, marked NOT TESTED ON HARDWARE (superseded by cambium's) | The serial-attached radio board: PowerFeather `F2BED4`, registry role `serial_bridge` | `bridge.ts` — the browser-side client for JSON lines over serial | The physical board cambium's daemon owns the serial port to |
| **commissioning** | (not used) | Fleet intake: flash a board, verify OTA, record it in `ops/fleet/registry.csv` (`status=commissioned`) | `CommissioningPanel.tsx` — the UI that binds a heard MAC to a fixtures.json slot | Spatial commissioning: sweep + align + assign, producing MAC -> position -> slot |
| **identify** | `constellate blink` — flash every node in index order to smoke-test wiring | `NbIdentify` packet: locate-blink, plus color/blink tail for ordering rig rows by eye | The 🔦 button: flash a fixture so the installer can tap its slot | Same one packet (`NbIdentify`), three UIs; cambium's CLI exposes it as `cambium blink` (W5) |
| **map** | The exported `index -> MAC` file (`--index-map`) and `index -> (x,y,z)` output | Host-pushed site state, e.g. pinned CA adjacency (`NbNeighborSet`) re-pushed by the host script | `calibration.ts` — the MAC <-> `fixture_id` calibration map | `fixtures-map.json`: the join of all of the above — MAC, fixture_id, position — one file, cambium-owned |
| **night** | (not used) | A lifecycle gate: show frames only render in `LIFE_NIGHT_SHOW` (`showActive = state == LIFE_NIGHT_SHOW && tier <= 1`) — fixtures ignore shows in daytime | (simulator has no gate; the twin always renders) | The #1 first-time-user trap: bench lanterns stay dark because it is "day". Fixed by `cambium night on` (W5), which drives the firmware's force-night override |
