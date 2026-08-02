"""The fake-fleet viewer: see the virtual tree respond.

Two routes mounted onto the daemon's aiohttp app when a FakeFleet is
attached: /fakefleet/state (JSON, polled) and /fakefleet/ (one static,
dependency-free canvas page). This is a bench instrument, not a product --
ugly-simple on purpose.
"""

from __future__ import annotations

from aiohttp import web

from .runner import FakeFleet


def attach_viewer(app: web.Application, fleet: FakeFleet) -> None:
    async def state(_request: web.Request) -> web.Response:
        fixtures = {}
        for mac, vf in fleet.fixtures.items():
            fixtures[mac] = {
                "fixture_id": fleet.fixture_id(mac),
                "class": vf.cls.name,
                "xyz": list(vf.xyz) if vf.xyz else None,
                "pixels": [[p.r, p.g, p.b, p.w] for p in vf.pixels()],
                "life_state": vf.life_state,
                "gated": vf.gated,
                "lease": vf.lease,
                "identify": vf.identify,
                "last_rx_age_s": (
                    round(vf.last_rx_age, 2) if vf.last_rx_age is not None else None
                ),
            }
        return web.json_response({"fixtures": fixtures})

    async def page(_request: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html")

    app.router.add_get("/fakefleet/state", state)
    app.router.add_get("/fakefleet/", page)


_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>cambium fake fleet</title>
<style>
  body { background:#111; color:#ccc; font:13px monospace; margin:16px; }
  canvas { background:#181818; border:1px solid #333; margin-right:12px; }
  #status { margin:8px 0; }
</style>
<div id="status">connecting…</div>
<canvas id="plan" width="420" height="420"></canvas>
<canvas id="elev" width="420" height="420"></canvas>
<script>
// Plan view = (x,y); elevation = (x,z). Circle color = averaged pixels.
// Gray cross = day-gated (fixtures ignore show frames -- the night gate is
// real). White ring = identify hold. Poll 10 Hz.
const plan = document.getElementById('plan').getContext('2d');
const elev = document.getElementById('elev').getContext('2d');

function bounds(fs, ax, ay) {
  let lo = [Infinity, Infinity], hi = [-Infinity, -Infinity];
  for (const f of fs) {
    if (!f.xyz) continue;
    lo[0] = Math.min(lo[0], f.xyz[ax]); hi[0] = Math.max(hi[0], f.xyz[ax]);
    lo[1] = Math.min(lo[1], f.xyz[ay]); hi[1] = Math.max(hi[1], f.xyz[ay]);
  }
  const pad = 1.0;
  return [lo[0] - pad, lo[1] - pad, hi[0] + pad, hi[1] + pad];
}

function draw(ctx, fs, ax, ay, label) {
  const [x0, y0, x1, y1] = bounds(fs, ax, ay);
  const W = ctx.canvas.width, H = ctx.canvas.height;
  const s = Math.min(W / (x1 - x0), H / (y1 - y0));
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#666';
  ctx.fillText(label, 8, 14);
  for (const f of fs) {
    if (!f.xyz) continue;
    const px = (f.xyz[ax] - x0) * s;
    const py = H - (f.xyz[ay] - y0) * s;
    let r = 0, g = 0, b = 0;
    for (const p of f.pixels) { r += p[0] + p[3]; g += p[1] + p[3]; b += p[2] + p[3]; }
    const n = f.pixels.length;
    r = Math.min(255, r / n); g = Math.min(255, g / n); b = Math.min(255, b / n);
    ctx.beginPath();
    ctx.arc(px, py, 9, 0, 7);
    ctx.fillStyle = `rgb(${r|0},${g|0},${b|0})`;
    ctx.fill();
    ctx.strokeStyle = '#444';
    ctx.stroke();
    if (f.identify) {
      ctx.beginPath(); ctx.arc(px, py, 13, 0, 7);
      ctx.strokeStyle = '#fff'; ctx.stroke();
    }
    if (f.gated || f.life_state === 'day') {
      ctx.strokeStyle = '#888';
      ctx.beginPath();
      ctx.moveTo(px - 6, py - 6); ctx.lineTo(px + 6, py + 6);
      ctx.moveTo(px - 6, py + 6); ctx.lineTo(px + 6, py - 6);
      ctx.stroke();
    }
    ctx.fillStyle = '#777';
    ctx.fillText(f.fixture_id || '', px + 12, py + 4);
  }
}

async function poll() {
  try {
    const res = await fetch('/fakefleet/state');
    const data = await res.json();
    const fs = Object.values(data.fixtures);
    const day = fs.filter(f => f.life_state === 'day').length;
    document.getElementById('status').textContent =
      `${fs.length} fixtures | ${day} day-gated (they IGNORE show frames -- ` +
      `run "cambium night on") | ${fs.length - day} night`;
    draw(plan, fs, 0, 1, 'plan (x,y)');
    draw(elev, fs, 0, 2, 'elevation (x,z)');
  } catch (e) {
    document.getElementById('status').textContent = 'daemon unreachable: ' + e;
  }
  setTimeout(poll, 100);
}
poll();
</script>
"""
