# Quickstart: the 10-lantern bench

The real thing. Do QUICKSTART-NO-HARDWARE first once — every command here
behaves identically, so any surprise on the bench is hardware, not software.

## Pre-flight

- [ ] Lanterns charged (doctor will show battery mV; LFP wants > 3200)
- [ ] One spare PowerFeather to be the bridge — **F2BED4** is earmarked
      (`ops/fleet/registry.csv` role `serial_bridge`)
- [ ] Everything is on **ESP-NOW channel 11** (the fleet's commissioned
      channel; a bridge on any other channel hears NOTHING)
- [ ] `config/roster-bench10.csv` edited to the 10 MACs actually on the bench
- [ ] For the sweep: 2–3 phones + a tape measure, on the same LAN as the laptop
- [ ] Fixture firmware: stock `fixture-2026-07-30.1` works for **identify /
      sweep / heartbeats**; the **`cambium-direct-frames` branch** (in
      resonance-hardware, awaiting Ben's review) is needed for per-fixture
      color streaming and radio night-override

## 1. Flash + connect the bridge

```
cd cambium/firmware/cambium_bridge
./build.sh --port /dev/tty.usbmodem101        # requires esp32 core >= 3.x
```

(On esp32 core 2.x the script tells you the two fixes: upgrade the core, or
`--fqbn esp32:esp32:esp32s3` for a generic-S3 build.)

## 2. Doctor

```
cambium doctor --port /dev/tty.usbmodem101
```

Stages: serial → bridge STATUS (fw/mac/**channel=11**) → 10 s heartbeat
census (every bench MAC with battery/RSSI) → night-gate status → `READY`.
**Do not proceed past a failing stage** — every failure line names its fix.

## 3. The night gate (read this box)

Daytime bench work splits in two:

- **identify / blink / Constellate sweeps work in DAY** — identify outranks
  the night gate by design (it's the commissioning primitive), and daylight
  keeps every *other* lantern dark, which is exactly what the cameras want.
- **driving patterns does NOT.** Show/direct frames are ignored in DAY
  lifecycle. Before step 7: `cambium night on --port ...` (branch firmware),
  or on stock firmware type `N1` over each lantern's own USB serial.

## 4. Roll-call

```
cambium blink --port /dev/tty.usbmodem101
```

Each lantern flashes white in printed index order. A wrong flash means the
physical labels are wrong, not the addressing — **the MAC is the identity;
labels are decoration**. Fix labels, not config, and re-run until clean.

## 5. Sweep (Constellate)

```
cambium sweep start                       # freezes the roster, prints the command
cambium serve --transport serial --port /dev/tty.usbmodem101   # leave running
# paste the printed `constellate serve --driver http ...` line (Constellate repo)
```

In Constellate: run `constellate blink` first (its own smoke test through
cambium), join the phones, **set the tape scale_measure** (led_pair,
meters — without it the export is unusable and `map ingest` will refuse),
then sweep (~10 × 3 s).

## 6. Map

```
cambium map ingest <constellate-session-dir> --sweep <name>
cambium map align --anchors anchors.json      # 3 tape-measured lanterns:
                                              # [{"index":0,"world":[x,y,z]}, ...]
cambium map assign
cambium map export
```

`map status` at any point shows where every lantern stands. Expect 10/10
`mapped` on a clean bench sweep.

## 7. Drive real

```
# night gate first (step 3)!  Then, with `cambium serve` still running:
cd ../resonance-lighting/app && npm run dev    # branch: cambium-ws-bridge
# open http://localhost:5173/?cambium=ws://localhost:8600/ws
#   (+ &fixtures=/fixtures.measured.json after copying it into app/public/)
# toggle 📡 drive real
```

Patterns on real lanterns. BLACKOUT reaches the hardware in under half a
second. Lanterns may be dimmer than the sim — that's each fixture's local
power budget clamping the request (doctrine; charge the battery).

## What-if table

| Symptom | Likely cause | Do |
|---|---|---|
| doctor: zero heartbeats | wrong channel / boards asleep | check stage-3 channel output; power-cycle a lantern next to the bridge; `--listen 30` |
| doctor hears fewer MACs than the roster | battery dead or out of range | bring the lantern near the bridge; check registry `last_verified` |
| blink lights the WRONG lantern | physical labeling wrong — MAC is truth | fix the label, not the roster |
| blink skips one lantern | that MAC asleep/missing | `cambium identify <mac> --secs 10 --port ...` to isolate |
| sweep missed index 7 | <2 cameras saw it — absent, not wrong | re-sweep with a phone moved, or accept `unmapped` (authored-fallback) |
| align residuals > 0.3 m | mistyped anchor or unscaled session | the leave-one-out warning names the suspect anchor; check `scale_measure` |
| drive real → nothing | the night gate (95% of the time) | step 3; `cambium doctor` stage 5 |
| patterns stop ~3 s after closing the sim | bridge-silence fallback — by design | restart the sim, or enjoy the autonomy |
| lanterns dimmer than the sim | power-budget clamp — by design | charge; don't fight the cap |
| `cambium doctor --port` says port busy | the daemon owns the port | use `--daemon http://localhost:8600` instead |

## Scaling to 118 (appendix)

Same commands. What changes: the sweep takes ~6 min instead of ~30 s;
`map assign` starts producing real `ambiguous` rows (resolve them in
`map/assignments-overrides.json` or confirm hypotheses in Elliot's
commissioning UI via `map export --calibration` / `map assign
--from-calibration`); and the 13 authored fixtures sharing coincident
positions are assigned arbitrarily within each group, flagged `hypothesis`.
