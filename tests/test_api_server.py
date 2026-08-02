"""HTTP + WS surface end-to-end via aiohttp's in-process TestClient (no real
ports): thin handlers dispatching to a recording services fake, mapping-mode
arbitration against the sim stream, and the bridge_status ticker."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cambium.api.constellate import MappingMode
from cambium.api.server import MAPPING_KEY, build_app
from cambium.api.services import DaemonServices
from cambium.config import CambiumConfig
from cambium.model import Fixture, FixtureClass
from cambium.roster import Roster


class FakeFleet:
    def __init__(self):
        self.listeners = []
        self.snap = {"fixtures": {"AA0001": {"soc": 88}}, "charging": 2}

    def snapshot(self):
        return self.snap

    def add_listener(self, cb):
        self.listeners.append(cb)

    def charging_count(self):
        return 2


class ManualClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_roster():
    # sorted by mac: AA0001, BB0002, CC0003 -- the mapping index order
    return Roster([
        Fixture("CC0003", "B003", FixtureClass.UPLIGHT, None),
        Fixture("AA0001", "B001", FixtureClass.DOWNLIGHT, None),
        Fixture("BB0002", "B002", FixtureClass.PERIMETER, None),
    ])


def make_services():
    rec = SimpleNamespace(calls=[], unknown_ids=[],
                          bridge={"connected": False, "tx_ok": 0})
    fleet = FakeFleet()

    def record(name):
        async def f(*args):
            rec.calls.append((name, *args))
        return f

    async def submit_sim_frame(fixtures, seq):
        rec.calls.append(("submit_sim_frame", fixtures, seq))
        return list(rec.unknown_ids)

    async def send_solid_debug(id_or_mac, r, g, b, w):
        if id_or_mac == "NOPE":
            raise LookupError(
                "no fixture 'NOPE'; use a fixture id or short mac from the roster CSV"
            )
        rec.calls.append(("send_solid_debug", id_or_mac, r, g, b, w))

    async def bridge_status():
        return dict(rec.bridge)

    services = DaemonServices(
        roster=make_roster(),
        fleet=fleet,
        submit_sim_frame=submit_sim_frame,
        set_drive=record("set_drive"),
        send_show=record("send_show"),
        send_identify=record("send_identify"),
        send_night=record("send_night"),
        send_program=record("send_program"),
        send_set_rate=record("send_set_rate"),
        send_solid_debug=send_solid_debug,
        bridge_status=bridge_status,
    )
    return services, rec, fleet


async def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


async def recv_kind(ws, kind, timeout=2.0):
    """Receive frames until one of `kind` arrives (skips e.g. bridge_status)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = await ws.receive_json(timeout=timeout)
        if msg.get("kind") == kind:
            return msg
    raise AssertionError(f"no {kind!r} frame within timeout")


@pytest.fixture
async def api():
    clients = []

    async def make(*, interval=3600.0):
        services, rec, fleet = make_services()
        app = build_app(services, CambiumConfig(),
                        bridge_status_interval_s=interval)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return SimpleNamespace(client=client, rec=rec, fleet=fleet, app=app,
                               services=services, mapping=app[MAPPING_KEY])

    yield make
    for c in clients:
        await c.close()


# ---- plain HTTP ------------------------------------------------------------

async def test_healthz(api):
    ctx = await api()
    resp = await ctx.client.get("/healthz")
    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True
    assert isinstance(body["version"], str) and body["version"]
    assert body["transport_connected"] is False
    ctx.rec.bridge["connected"] = True
    body = await (await ctx.client.get("/healthz")).json()
    assert body["transport_connected"] is True


async def test_fleet_snapshot_passthrough(api):
    ctx = await api()
    resp = await ctx.client.get("/fleet")
    assert resp.status == 200
    assert await resp.json() == ctx.fleet.snap


# ---- /constellate ----------------------------------------------------------

async def test_constellate_light_happy(api):
    ctx = await api()
    resp = await ctx.client.get("/constellate/light", params={"led": "1"})
    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True and body["led"] == 1 and body["mac"] == "BB0002"
    assert ("send_identify", "BB0002", 10, 5, False) in ctx.rec.calls
    assert ctx.mapping.active


async def test_constellate_light_cancels_previous_target(api):
    ctx = await api()
    await ctx.client.get("/constellate/light", params={"led": "0"})
    await ctx.client.get("/constellate/light", params={"led": "2"})
    idents = [c for c in ctx.rec.calls if c[0] == "send_identify"]
    assert idents == [
        ("send_identify", "AA0001", 10, 5, False),
        ("send_identify", "AA0001", 0, 0, False),  # cancel before retarget
        ("send_identify", "CC0003", 10, 5, False),
    ]


async def test_constellate_light_404_names_roster_length(api):
    ctx = await api()
    resp = await ctx.client.get("/constellate/light", params={"led": "7"})
    body = await resp.json()
    assert resp.status == 404
    assert "3 fixtures" in body["error"]  # roster length is in the message
    assert "0..2" in body["error"]
    assert not [c for c in ctx.rec.calls if c[0] == "send_identify"]
    assert not ctx.mapping.active


async def test_constellate_light_bad_param(api):
    ctx = await api()
    for params in ({"led": "abc"}, {}):
        resp = await ctx.client.get("/constellate/light", params=params)
        body = await resp.json()
        assert resp.status == 400
        assert "integer" in body["error"] and "led" in body["error"]


async def test_constellate_off(api):
    ctx = await api()
    await ctx.client.get("/constellate/light", params={"led": "0"})
    resp = await ctx.client.get("/constellate/off")
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "sim_frames_dropped": 0}
    assert not ctx.mapping.active
    assert ctx.rec.calls[-1] == ("send_identify", "AA0001", 0, 0, False)


async def test_mapping_preempts_sim_frames_end_to_end(api):
    ctx = await api()
    await ctx.client.get("/constellate/light", params={"led": "0"})
    ws = await ctx.client.ws_connect("/ws")
    frame = {"kind": "frame", "seq": 1,
             "fixtures": [{"id": "B001", "rgb": [1, 0, 0]}]}
    await ws.send_str(json.dumps(frame))
    # a second, non-frame message proves the first was processed (same-socket
    # dispatch is sequential), so the assert below is not a race
    await ws.send_str(json.dumps({"kind": "drive", "on": True}))
    await wait_until(lambda: ("set_drive", True) in ctx.rec.calls)
    assert not [c for c in ctx.rec.calls if c[0] == "submit_sim_frame"]
    # the drop is counted and visible on /constellate/off
    resp = await ctx.client.get("/constellate/off")
    assert (await resp.json())["sim_frames_dropped"] == 1
    # after release, sim frames flow again
    await ws.send_str(json.dumps(frame))
    await wait_until(
        lambda: any(c[0] == "submit_sim_frame" for c in ctx.rec.calls))
    await ws.close()


# ---- /debug/solid ----------------------------------------------------------

async def test_debug_solid_happy(api):
    ctx = await api()
    resp = await ctx.client.get(
        "/debug/solid",
        params={"id": "B003", "r": "255", "g": "64", "b": "1", "w": "12"})
    body = await resp.json()
    assert resp.status == 200 and body["ok"] is True
    assert ("send_solid_debug", "B003", 255, 64, 1, 12) in ctx.rec.calls


async def test_debug_solid_channels_default_to_zero(api):
    ctx = await api()
    resp = await ctx.client.get("/debug/solid",
                                params={"id": "AA0001", "r": "128"})
    assert resp.status == 200
    assert ("send_solid_debug", "AA0001", 128, 0, 0, 0) in ctx.rec.calls


async def test_debug_solid_missing_id(api):
    ctx = await api()
    resp = await ctx.client.get("/debug/solid", params={"r": "10"})
    body = await resp.json()
    assert resp.status == 400
    assert "id" in body["error"] and "B003" in body["error"]  # names the fix


async def test_debug_solid_bad_channel_values(api):
    ctx = await api()
    resp = await ctx.client.get("/debug/solid",
                                params={"id": "B003", "r": "300"})
    body = await resp.json()
    assert resp.status == 400 and "0..255" in body["error"]
    resp = await ctx.client.get("/debug/solid",
                                params={"id": "B003", "g": "loud"})
    body = await resp.json()
    assert resp.status == 400 and "integer" in body["error"]
    assert not [c for c in ctx.rec.calls if c[0] == "send_solid_debug"]


async def test_debug_solid_unknown_fixture_is_404(api):
    ctx = await api()
    resp = await ctx.client.get("/debug/solid",
                                params={"id": "NOPE", "r": "1"})
    body = await resp.json()
    assert resp.status == 404 and "roster" in body["error"]


# ---- WS surface ------------------------------------------------------------

async def test_ws_malformed_json_keeps_socket_alive(api):
    ctx = await api()
    ws = await ctx.client.ws_connect("/ws")
    await ws.send_str("{definitely not json")
    err = await recv_kind(ws, "err")
    assert "JSON" in err["msg"]
    await ws.send_str(json.dumps({"kind": "drive", "on": True}))
    await wait_until(lambda: ("set_drive", True) in ctx.rec.calls)
    await ws.close()


async def test_fleet_listener_broadcasts_to_all_sockets(api):
    ctx = await api()
    ws1 = await ctx.client.ws_connect("/ws")
    ws2 = await ctx.client.ws_connect("/ws")
    # build_app wired the hub in as a fleet listener
    (listener,) = ctx.fleet.listeners
    listener("charging", {"count": 4})
    for ws in (ws1, ws2):
        msg = await recv_kind(ws, "charging")
        assert msg == {"kind": "charging", "count": 4}
    await ws1.close()
    await ws2.close()


async def test_bridge_status_ticker_broadcasts(api):
    ctx = await api(interval=0.05)
    ws = await ctx.client.ws_connect("/ws")
    msg = await recv_kind(ws, "bridge_status")
    assert msg["connected"] is False and "tx_ok" in msg
    await ws.close()


# ---- MappingMode internals (clock/sleep injected) --------------------------

async def test_mapping_refreshes_while_held():
    services, rec, _ = make_services()
    m = MappingMode(services, sleep=lambda s: asyncio.sleep(0))
    await m.light(0)
    await wait_until(
        lambda: rec.calls.count(("send_identify", "AA0001", 10, 5, False)) >= 2)
    await m.off()
    assert not m.active
    assert rec.calls[-1] == ("send_identify", "AA0001", 0, 0, False)


async def test_mapping_idle_auto_release():
    services, rec, _ = make_services()
    clock = ManualClock()
    m = MappingMode(services, clock=clock, sleep=lambda s: asyncio.sleep(0))
    await m.light(1)
    clock.t = 61.0  # a minute of sweep-driver silence
    await wait_until(lambda: not m.active)
    assert ("send_identify", "BB0002", 0, 0, False) in rec.calls


async def test_mapping_out_of_range_is_lookup_error():
    services, rec, _ = make_services()
    m = MappingMode(services)
    with pytest.raises(LookupError) as e:
        await m.light(3)
    assert "3 fixtures" in str(e.value)
    with pytest.raises(LookupError):
        await m.light(-1)
    assert not m.active and rec.calls == []
