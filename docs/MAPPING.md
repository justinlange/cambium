# MAPPING — spatial commissioning, frames, and artifacts

How a pile of physically identical lanterns becomes a 3D-addressable tree:
Constellate photographs them one at a time, cambium joins the triangulated
points to MACs, re-frames them into tree world, assigns authored slots, and
emits both its own canonical map and Elliot's fixtures file.

## The three coordinate frames (the one diagram this project needs)

```
CONSTELLATE CAMERA FRAME          TREE WORLD                ELLIOT'S FILE
(what points.json contains)       (what cambium stores)     (fixtures.measured.json)
+X right, +Y DOWN, +Z into        Z-up, meters,             Z-up, meters
the scene; origin = a phone's     origin = trunk base       (same frame -- no
optical center; NO gravity        (bench: line center)      further conversion)
        │                              ▲
        └──── map align ───────────────┘
              (Umeyama similarity from >= 3 operator anchors)
```

Nothing downstream of `map align` ever sees camera coordinates. The
simulator's Blender Z-up convention matches tree world, so export is a
straight copy.

## Pipeline stages and artifacts

| Stage | Command | Reads | Writes |
|---|---|---|---|
| freeze | `cambium sweep start` | roster CSV (+ live fleet if `--daemon`) | `sweeps/<s>/roster.json` (`cambium.sweep-roster/1`) — **immutable**: index = MAC-ascending order, the contract `light(n)` and ingest both resolve against |
| capture | Constellate (`constellate serve --driver http ...` — printed by sweep start) | — | a Constellate session dir |
| ingest | `cambium map ingest <session> --sweep <s>` | session + frozen roster | `map/measured-points.json` (`cambium.measured-points/1`) |
| align | `cambium map align --anchors f.json` | measured-points + anchors | `map/transform.json` (`cambium.transform/1`), append-versioned |
| assign | `cambium map assign [--authored f.json]` | world points + authored slots + overrides | `map/assignments.json` |
| export | `cambium map export [--authored f.json]` | all of the above | `out/fixtures-map.json` (`cambium.fixtures-map/1`) + `out/fixtures.measured.json` (`resonance.fixtures/0.3`) |

All artifacts live under `site/<name>/` (`--site`, default `bench`) and are
plain JSON with provenance blocks — diff them, don't trust memory.

## Doctrines

**Scale.** Constellate exports in arbitrary units unless a tape
`scale_measure` was set — the numbers look identical. `map ingest` REFUSES
unscaled sessions; `--allow-unscaled` defers to align, which then fits
scale from the anchors (and warns if tape and anchors disagree > 5%).

**Absent is not wrong.** A lantern seen by < 2 cameras is missing from the
export, status `unmapped`. Export keeps it at its authored position
(`cambium_status: "authored-fallback"`, visible in the app's DataLog);
`--strict` omits it so the hole is visible instead.

**Anchors over correspondence search.** ≥ 3 anchors (a lantern index + its
tape-measured world position, or an authored `fixture_id`) pin the frame in
five minutes at any fleet size, deterministically. Leave-one-out residuals
name a fat-fingered anchor. The 13 authored fixtures with coincident
positions are exactly why automatic global registration is not the v1 tool.

**Assignment is hypothesis until confirmed.** Mutual-NN with an ambiguity
test (2nd-nearest < 1.5× nearest, or nearest > 0.75 m → `ambiguous`,
unassigned, reported). Resolution paths: `map/assignments-overrides.json`
(always wins), or Elliot's commissioning UI — `map export --calibration
out.json` hands him hypotheses in his own calibration.ts v2 shape;
`map assign --from-calibration his-export.json` takes confirmed truth back.

## Future (noted, not built)

`fixtures-map.json` carries everything a `cambium neighbors push` would
need to compute k-nearest sets and emit `NB_NEIGHBOR_SET` per Ben's
site-data doctrine (positions live on the host, never in firmware).
