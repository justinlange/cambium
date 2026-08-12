# Quickstart: no hardware at all (~15 minutes)

The entire tree pipeline — simulator → daemon → fleet → mapping — rehearsed
on one laptop with zero hardware. Every trap you'd hit on the bench (the
night gate, the silence fallback) is reproduced here on purpose, with the
same fix commands.

Every step shows the command and what you should see. If anything differs,
run `cambium doctor --daemon http://localhost:8600` — its errors contain
their fixes.

## 1. Install

```
cd cambium
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -q        # expect: all passing
```

(Optional: `alias cambium=$PWD/.venv/bin/cambium` for the rest of this page.)

## 2. Start the fake fleet

```
cambium fakefleet run --count 10
```

Expect:

```
fake fleet: 10 fixtures | viewer: http://localhost:8600/fakefleet/
fixtures boot DAY-gated (like real hardware): run "cambium night on" ... or pass --start-night
```

Open the viewer URL. You'll see 10 circles **breathing dim amber with gray
crosses**. That's not broken — it's the two doctrines at once:

- **amber breathing** = autonomous fallback: nothing is driving them, so
  they run their own show (real lanterns do exactly this 3 s after the
  bridge goes silent — never blank).
- **gray cross** = the **night gate**: fixtures boot in DAY lifecycle and
  IGNORE show frames. Real hardware's #1 bench trap, rehearsed here.

## 3. Doctor, then fix the night gate

In a second terminal:

```
cambium doctor --daemon http://localhost:8600 --listen 2
```

Expect stages `ok bridge / ok channel / ok fleet (10 heard)` and then the
warning: `night gate: 10/10 fixtures are in DAY ... Run 'cambium night on'`.
So:

```
cambium night on --daemon http://localhost:8600
```

The gray crosses disappear. Doctor again → `ok night gate ... READY`.

## 4. Roll-call

```
cambium blink --daemon http://localhost:8600
```

Watch the viewer: each fixture flashes white **in index order** (mac
ascending — the same order every sweep uses). On the real bench this is
where you'd call out a wrong flash and stop.

## 5. Rehearse the mapping pipeline (synthetic sweep)

No Constellate install needed — `make-sweep` emits exactly what a real
sweep produces, from known geometry:

```
cambium fakefleet make-sweep --out site/rehearsal/sweeps/t1 --count 10 --dropout 7
cambium map ingest site/rehearsal/sweeps/t1/session --sweep t1 --site rehearsal
cambium map align --anchors site/rehearsal/sweeps/t1/anchors.example.json --site rehearsal
cambium map assign --site rehearsal
cambium map export --site rehearsal
cambium map status --site rehearsal
```

Expect: `9 measured, 1 unmapped` at ingest (index 7 was dropped — *absent,
not wrong*; Constellate omits lanterns seen by fewer than two cameras),
residuals of a few cm at align, and an `out/fixtures.measured.json` in
Elliot's exact schema. On a real sweep the anchors file is 3 lanterns you
tape-measure; here it's pre-baked from ground truth.

## 6. Drive it from Elliot's simulator

The sim addresses lanterns by *its* fixture ids, so the fake fleet must be
built from the same fixtures file — a `--count` fleet (synthetic ids) will
answer every frame with `err: ... unknown fixture ids; add them to the
roster CSV`. Restart the step-2 fleet from the app's file (Ctrl-C it first):

```
cambium fakefleet run --fixtures ../resonance-lighting/app/public/fixtures.json --start-night
```

(`--start-night` is fine here — you already rehearsed the gate in step 3.)

```
cd ../resonance-lighting/app && npm install && npm run dev
```

Open `http://localhost:5173/?cambium=ws://localhost:8600/ws`
(add `&fixtures=/fixtures.measured.json` after copying
`site/.../out/fixtures.measured.json` into `app/public/` to see measured
positions instead of the authored layout).

Toggle **📡 drive real** in Controls. Put the simulator and the fake-fleet
viewer side by side: the virtual lanterns now follow the twin's patterns.
Hit **BLACKOUT** — the fleet goes dark. Close the sim tab — within ~3 s the
fleet crossfades back to amber autonomy. Both behaviors are the doctrine,
not bugs.

> Requires the `cambium-ws-bridge` branch of resonance-lighting (WsBridge +
> RealDriveDriver). `git switch cambium-ws-bridge` if the toggle does nothing.

## 7. What you just proved

Every seam of the real bench flow — WS frame vocabulary, packet building,
COBS framing, fleet RX ladder, night gate, identify sweeps, the whole map
pipeline — ran through the production code paths. The only things the bench
adds are physics: radio loss, batteries, and the actual bridge board. Those
are QUICKSTART-BENCH10.md.
