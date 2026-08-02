"""The whole stack with zero hardware: Daemon + FakeFleet on one loopback.

Unlike test_daemon_smoke (which peers with hand-rolled frames), the far end
here is the real FakeFleet: virtual fixtures consume the daemon's broadcasts
and their heartbeats flow back -- the same wiring `cambium fakefleet run`
gives a human, exercised as tests.

Timing note: ladder timing (hold/stale) is unit-tested with a fake clock in
test_fakefleet_rx; these tests only use fast paths (identify, night flip,
slew convergence within ~1 s) so the suite stays quick and race-free.
"""

import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer

from cambium.config import CambiumConfig
from cambium.daemon import Daemon
from cambium.fakefleet.runner import FakeFleet, synthetic_fixtures
from cambium.fakefleet.viewer import attach_viewer
from cambium.model import RGBW
from cambium.roster import Roster
from cambium.transport.loopback import LoopbackTransport


async def wait_until(pred, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


class Stack:
    """Daemon + fleet + HTTP client, torn down in one place."""

    def __init__(self, n: int = 3, start_night: bool = False) -> None:
        self.fixtures = synthetic_fixtures(n)
        self.transport = LoopbackTransport()
        self.fleet = FakeFleet(self.fixtures, start_night=start_night)
        self.daemon = Daemon(
            CambiumConfig(tx_hz=20.0), self.transport, Roster(list(self.fixtures))
        )
        attach_viewer(self.daemon.app, self.fleet)
        self.client: TestClient | None = None

    async def __aenter__(self) -> "Stack":
        await self.daemon.start(serve_http=False)
        self.client = TestClient(TestServer(self.daemon.app))
        await self.client.start_server()
        await self.fleet.start(self.transport.peer)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.fleet.stop()
        await self.client.close()
        await self.daemon.stop()

    def vf(self, i: int):
        return self.fleet.fixtures[self.fixtures[i].mac]


async def test_night_gate_then_render_and_ws_night():
    async with Stack() as s:
        ws = await s.client.ws_connect("/ws")
        await ws.send_json({"kind": "drive", "on": True})
        frame = {
            "kind": "frame",
            "seq": 1,
            "fixtures": [
                {"id": f.fixture_id, "rgb": [1.0, 1.0, 1.0]} for f in s.fixtures
            ],
        }
        await ws.send_json(frame)
        # Day-gated: the frame reaches the fixtures and is REFUSED, visibly.
        await wait_until(lambda: s.vf(0).gated)
        assert s.vf(0).pixels()[0] != RGBW(0, 0, 0, 255)

        # Flip the fleet to night over the wire, resend, watch it render:
        # white extraction turns sim (1,1,1) into RGBW(0,0,0,255).
        await ws.send_json({"kind": "night", "mode": 1, "mac": None})
        await wait_until(lambda: s.vf(0).night)

        async def converged() -> bool:
            await ws.send_json(frame)
            return s.vf(0).pixels()[0] == RGBW(0, 0, 0, 255)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not await converged():
            await asyncio.sleep(0.05)
        assert s.vf(0).pixels()[0] == RGBW(0, 0, 0, 255)
        assert all(s.vf(i).pixels()[0] == RGBW(0, 0, 0, 255) for i in range(3))
        await ws.close()


async def test_constellate_light_is_exclusive():
    async with Stack() as s:
        # macs FA0000/FA0001/FA0002: index 0 = FA0000 (mac-ascending).
        resp = await s.client.get("/constellate/light?led=0")
        assert resp.status == 200
        await wait_until(lambda: s.vf(0).identify is not None)
        assert s.vf(1).identify is None and s.vf(2).identify is None
        assert s.vf(0).pixels()[0] == RGBW(255, 255, 255, 255)  # works in DAY

        resp = await s.client.get("/constellate/light?led=1")
        assert resp.status == 200
        await wait_until(lambda: s.vf(1).identify is not None)
        # previous target cancelled (secs=0): never two lit at once
        await wait_until(lambda: s.vf(0).identify is None)

        resp = await s.client.get("/constellate/off")
        assert resp.status == 200
        await wait_until(
            lambda: all(s.vf(i).identify is None for i in range(3))
        )

        resp = await s.client.get("/constellate/light?led=99")
        assert resp.status == 404
        assert "3 fixtures" in await resp.text()


async def test_fleet_snapshot_charging_and_viewer():
    async with Stack() as s:
        ws = await s.client.ws_connect("/ws")
        # Heartbeats populate /fleet (boot hb-full arrives on the first tick).
        async def fleet_online() -> bool:
            resp = await s.client.get("/fleet")
            data = await resp.json()
            return len(data.get("fixtures", data)) >= 3

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not await fleet_online():
            await asyncio.sleep(0.05)
        assert await fleet_online()

        # Flip one fixture to charging; the count change must reach the WS.
        s.vf(1).batt_ma = 250
        got_charging = asyncio.Event()

        async def drain():
            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("kind") == "charging" and data.get("count") == 1:
                    got_charging.set()
                    return

        drainer = asyncio.create_task(drain())
        await asyncio.wait_for(got_charging.wait(), 5.0)
        drainer.cancel()

        # Viewer: state JSON mirrors the fixtures; page serves the canvas.
        resp = await s.client.get("/fakefleet/state")
        state = await resp.json()
        assert set(state["fixtures"]) == {f.mac for f in s.fixtures}
        one = state["fixtures"][s.fixtures[0].mac]
        assert one["life_state"] == "day" and len(one["pixels"][0]) == 4
        resp = await s.client.get("/fakefleet/")
        page = await resp.text()
        assert 'id="plan"' in page and 'id="elev"' in page
        await ws.close()
