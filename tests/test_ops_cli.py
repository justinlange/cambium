"""Ops commands against a live Daemon+FakeFleet through the HTTP link --
the same path `cambium doctor --daemon http://localhost:8600` takes."""

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from cambium.config import CambiumConfig
from cambium.daemon import Daemon
from cambium.fakefleet.runner import FakeFleet, synthetic_fixtures
from cambium.ops import commands
from cambium.ops.oplink import HttpOpsLink
from cambium.roster import Roster
from cambium.transport.loopback import LoopbackTransport


class Stack:
    def __init__(self, n: int = 3, start_night: bool = False) -> None:
        self.fixtures = synthetic_fixtures(n)
        self.roster = Roster(list(self.fixtures))
        self.transport = LoopbackTransport()
        self.fleet = FakeFleet(self.fixtures, start_night=start_night)
        self.daemon = Daemon(CambiumConfig(), self.transport, self.roster)

    async def __aenter__(self):
        await self.daemon.start(serve_http=False)
        self.client = TestClient(TestServer(self.daemon.app))
        await self.client.start_server()
        await self.fleet.start(self.transport.peer)
        # An HttpOpsLink that reuses the in-process test client.
        self.link = HttpOpsLink("http://in-process")
        self.link._session = None  # replaced below

        class _Sess:
            def __init__(self, client):
                self._client = client

            def get(self, url, **kw):
                path = url.split("in-process", 1)[-1]
                return self._client.get(path)

            async def close(self):
                pass

        self.link._session = _Sess(self.client)
        return self

    async def __aexit__(self, *exc):
        await self.fleet.stop()
        await self.client.close()
        await self.daemon.stop()

    async def wait_heartbeats(self, n: int, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.daemon.fleet.snapshot()) >= n:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("heartbeats did not arrive")


async def test_doctor_stages_and_night_warning():
    async with Stack() as s:
        await s.wait_heartbeats(3)
        code, lines = await commands.doctor(
            s.link, s.roster, expect_channel=11, listen_s=0.2
        )
        text = "\n".join(lines)
        assert code == 0 and "READY" in text
        assert "ok   bridge:" in text and "channel=11" in str(text)
        assert "3 heard, 0 missing" in text
        # Day-gated fleet -> the night warning names the fix
        assert "cambium night on" in text


async def test_doctor_channel_mismatch_fails():
    async with Stack() as s:
        await s.wait_heartbeats(1)
        code, lines = await commands.doctor(
            s.link, s.roster, expect_channel=6, listen_s=0.1
        )
        assert code == 1
        assert any("bridge is on 11" in ln and "--channel 6" in ln for ln in lines)


async def test_night_on_flips_fleet_and_doctor_goes_green():
    async with Stack() as s:
        await s.wait_heartbeats(3)
        code, lines = await commands.night(s.link, "on")
        assert code == 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not all(
            vf.night for vf in s.fleet.fixtures.values()
        ):
            await asyncio.sleep(0.05)
        assert all(vf.night for vf in s.fleet.fixtures.values())
        # stock-firmware fallback is always mentioned
        assert any("N1" in ln for ln in lines)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not all(
            (entry.get("telemetry") or {}).get("life_state") == 3
            for entry in s.daemon.fleet.snapshot().values()
        ):
            await asyncio.sleep(0.05)
        code, lines = await commands.doctor(
            s.link, s.roster, expect_channel=11, listen_s=0.1
        )
        assert code == 0
        assert "ok   night gate: fleet is in NIGHT" in "\n".join(lines)


async def test_blink_roll_call_in_sweep_order():
    async with Stack() as s:
        seen: list[str] = []

        async def spy_sleep(dt: float):
            lit = [m for m, vf in s.fleet.fixtures.items() if vf.identify]
            seen.extend(lit)
            await asyncio.sleep(0.05)  # let cancel/identify frames land

        code, lines = await commands.blink(
            s.link, s.roster, secs=1, delay_s=0.0, sleep=spy_sleep
        )
        assert code == 0
        # sweep order = mac ascending = FA0000, FA0001, FA0002
        assert seen == [f.mac for f in sorted(s.fixtures, key=lambda f: f.mac)]
        assert "call out any mismatch" in lines[0]


async def test_blink_unknown_fixture_names_the_fix():
    async with Stack() as s:
        code, lines = await commands.blink(s.link, s.roster, mac="NOPE")
        assert code == 1 and "roster" in lines[0]


async def test_debug_endpoints_validate():
    async with Stack() as s:
        resp = await s.client.get("/debug/night?mode=9")
        assert resp.status == 400
        assert "0 (force day)" in (await resp.json())["error"]
        resp = await s.client.get("/debug/identify?mac=FA0001&secs=5&color=2")
        assert resp.status == 200
        deadline = time.monotonic() + 2.0
        vf = s.fleet.fixtures["FA0001"]
        while time.monotonic() < deadline and vf.identify is None:
            await asyncio.sleep(0.05)
        assert vf.identify is not None and vf.identify["color"] == 2
        resp = await s.client.get("/bridge")
        data = await resp.json()
        assert data.get("connected") is True
