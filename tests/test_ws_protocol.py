"""WS JSON vocabulary: every DOWN kind dispatches validated args to the
injected services; malformed input earns an err frame (never a disconnect);
WsHub fans UP frames out to every live socket."""

import asyncio
import json

from cambium.api.services import DaemonServices
from cambium.api.ws_protocol import WsHub, handle_down
from cambium.model import Fixture, FixtureClass
from cambium.roster import Roster


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_str(self, text: str) -> None:
        if self.closed:
            raise ConnectionResetError("socket closed")
        self.sent.append(json.loads(text))


class FakeFleet:
    def __init__(self):
        self.listeners = []
        self.snap = {"fixtures": {}}

    def snapshot(self):
        return self.snap

    def add_listener(self, cb):
        self.listeners.append(cb)

    def charging_count(self):
        return 0


class FakeMapping:
    def __init__(self, active=False):
        self.active = active
        self.sim_frames_dropped = 0

    def drop_sim_frame(self):
        self.sim_frames_dropped += 1


def make_roster():
    return Roster([
        Fixture("CC0003", "B003", FixtureClass.UPLIGHT, None),
        Fixture("AA0001", "B001", FixtureClass.DOWNLIGHT, None),
        Fixture("BB0002", "B002", FixtureClass.PERIMETER, None),
    ])


def make_services():
    calls = []
    unknown = []  # mutate in a test to make submit_sim_frame report ids

    def rec(name):
        async def f(*args):
            calls.append((name, *args))
        return f

    async def submit_sim_frame(fixtures, seq):
        calls.append(("submit_sim_frame", fixtures, seq))
        return list(unknown)

    async def bridge_status():
        return {"connected": False}

    services = DaemonServices(
        roster=make_roster(),
        fleet=FakeFleet(),
        submit_sim_frame=submit_sim_frame,
        set_drive=rec("set_drive"),
        send_show=rec("send_show"),
        send_identify=rec("send_identify"),
        send_night=rec("send_night"),
        send_program=rec("send_program"),
        send_set_rate=rec("send_set_rate"),
        send_solid_debug=rec("send_solid_debug"),
        bridge_status=bridge_status,
    )
    return services, calls, unknown


async def send(ws, services, mapping=None, **msg):
    await handle_down(ws, json.dumps(msg), services, mapping)


def errs(ws):
    return [m for m in ws.sent if m["kind"] == "err"]


# ---- frame -----------------------------------------------------------------

async def test_frame_dispatches_fixtures_and_seq():
    services, calls, _ = make_services()
    ws = FakeWs()
    fixtures = [{"id": "B001", "rgb": [1.5, 0.25, 0]}, {"id": "B002", "rgb": [0, 0, 1]}]
    await send(ws, services, kind="frame", seq=7, fixtures=fixtures)
    assert calls == [("submit_sim_frame", fixtures, 7)]
    # linear floats above 1.0 are legal sim colors -- no err, no clamping here
    assert ws.sent == []


async def test_frame_unknown_ids_become_err():
    services, calls, unknown = make_services()
    unknown.append("F999")
    ws = FakeWs()
    await send(ws, services, kind="frame", seq=1,
               fixtures=[{"id": "F999", "rgb": [0, 0, 0]}])
    (e,) = errs(ws)
    assert "F999" in e["msg"] and "roster" in e["msg"]


async def test_frame_missing_seq():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="frame", fixtures=[])
    (e,) = errs(ws)
    assert "seq" in e["msg"]
    assert calls == []


async def test_frame_fixtures_not_a_list():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="frame", seq=1, fixtures={"id": "B001"})
    (e,) = errs(ws)
    assert "fixtures" in e["msg"]
    assert calls == []


async def test_frame_bad_rgb_names_index_and_fix():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="frame", seq=1,
               fixtures=[{"id": "B001", "rgb": [1, 2]}])
    (e,) = errs(ws)
    assert "fixtures[0]" in e["msg"] and "3 numbers" in e["msg"]
    assert calls == []


async def test_frame_bad_id():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="frame", seq=1,
               fixtures=[{"id": 5, "rgb": [0, 0, 0]}])
    (e,) = errs(ws)
    assert "id" in e["msg"]
    assert calls == []


# ---- mapping preemption ----------------------------------------------------

async def test_mapping_preempts_frame_silently():
    services, calls, _ = make_services()
    mapping = FakeMapping(active=True)
    ws = FakeWs()
    await send(ws, services, mapping, kind="frame", seq=1,
               fixtures=[{"id": "B001", "rgb": [1, 1, 1]}])
    assert calls == []  # dropped, never submitted
    assert mapping.sim_frames_dropped == 1  # but counted
    assert ws.sent == []  # and silent -- the sim keeps streaming harmlessly
    # non-frame kinds are NOT preempted: identify etc. still flow
    await send(ws, services, mapping, kind="drive", on=True)
    assert calls == [("set_drive", True)]


async def test_mapping_inactive_frames_flow():
    services, calls, _ = make_services()
    mapping = FakeMapping(active=False)
    ws = FakeWs()
    await send(ws, services, mapping, kind="frame", seq=2, fixtures=[])
    assert calls == [("submit_sim_frame", [], 2)]
    assert mapping.sim_frames_dropped == 0


# ---- drive / night / program / show / identify / set_rate ------------------

async def test_drive():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="drive", on=True)
    await send(ws, services, kind="drive", on=False)
    assert calls == [("set_drive", True), ("set_drive", False)]


async def test_drive_needs_boolean_on():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="drive", on=1)
    await send(ws, services, kind="drive")
    assert len(errs(ws)) == 2 and all("on" in e["msg"] for e in errs(ws))
    assert calls == []


async def test_night():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="night", mode=1, mac="F2BDB4")
    await send(ws, services, kind="night", mode=2)
    assert calls == [("send_night", 1, "F2BDB4"), ("send_night", 2, None)]


async def test_night_bad_mode():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="night", mode=3)
    await send(ws, services, kind="night")
    assert len(errs(ws)) == 2
    assert "0=force day" in errs(ws)[0]["msg"]
    assert calls == []


async def test_program_full():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="program", mac="AA0001", programId=3,
               leaseS=120, seed=9, hardCut=True, params=[1, 2])
    assert calls == [("send_program", "AA0001", 3, 120, 9, True, [1, 2])]


async def test_program_defaults():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="program", programId=0, leaseS=0)
    assert calls == [("send_program", None, 0, 0, 0, False, None)]


async def test_program_missing_program_id():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="program", leaseS=60)
    (e,) = errs(ws)
    assert "programId" in e["msg"]
    assert calls == []


async def test_show():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="show", phase=512, hue=200)
    await send(ws, services, kind="show", phase=0, hue=0, flags=2)
    assert calls == [("send_show", 512, 200, 0), ("send_show", 0, 0, 2)]


async def test_identify():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="identify", seconds=10)
    await send(ws, services, kind="identify", mac="BB0002", seconds=5,
               color=5, blink=True)
    assert calls == [
        ("send_identify", None, 10, 0, False),
        ("send_identify", "BB0002", 5, 5, True),
    ]


async def test_identify_missing_seconds():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="identify", mac="BB0002")
    (e,) = errs(ws)
    assert "seconds" in e["msg"]
    assert calls == []


async def test_set_rate_frame_hz_and_hb_hz():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="set_rate", frameHz=8)
    await send(ws, services, kind="set_rate", hbHz=2)
    # one firmware knob: frameHz wins when both are given
    await send(ws, services, kind="set_rate", frameHz=5, hbHz=1)
    assert calls == [("send_set_rate", 8), ("send_set_rate", 2), ("send_set_rate", 5)]


async def test_set_rate_needs_a_rate():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="set_rate")
    (e,) = errs(ws)
    assert "frameHz" in e["msg"] and "hbHz" in e["msg"]
    assert calls == []


# ---- ruleset / unknown / malformed -----------------------------------------

async def test_ruleset_is_silently_accepted():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="ruleset", rules=[{"when": "dusk"}])
    assert calls == []  # nothing on the wire (no firmware support)
    assert ws.sent == []  # and no err: a healthy connection must look healthy


async def test_unknown_kind_lists_valid_kinds_and_continues():
    services, calls, _ = make_services()
    ws = FakeWs()
    await send(ws, services, kind="warp", factor=9)
    (e,) = errs(ws)
    assert "warp" in e["msg"] and "frame" in e["msg"] and "ruleset" in e["msg"]
    # the session continues: the next valid message dispatches normally
    await send(ws, services, kind="drive", on=True)
    assert calls == [("set_drive", True)]


async def test_malformed_json_is_err_not_disconnect():
    services, calls, _ = make_services()
    ws = FakeWs()
    await handle_down(ws, "{definitely not json", services)
    (e,) = errs(ws)
    assert "JSON" in e["msg"]
    await send(ws, services, kind="drive", on=False)
    assert calls == [("set_drive", False)]


async def test_non_object_json_is_err():
    services, calls, _ = make_services()
    ws = FakeWs()
    await handle_down(ws, "[1, 2, 3]", services)
    (e,) = errs(ws)
    assert "object" in e["msg"]
    assert calls == []


async def test_service_exception_becomes_err_frame():
    services, calls, _ = make_services()

    async def boom(on):
        raise RuntimeError("transport fell over")

    services.set_drive = boom
    ws = FakeWs()
    await send(ws, services, kind="drive", on=True)
    (e,) = errs(ws)
    assert "internal error" in e["msg"] and "drive" in e["msg"]


# ---- WsHub -----------------------------------------------------------------

async def test_hub_fans_out_to_two_sockets():
    hub = WsHub()
    a, b = FakeWs(), FakeWs()
    hub.add(a)
    hub.add(b)
    await hub.broadcast("hb", {"mac": "AA0001", "soc": 91})
    assert a.sent == [{"kind": "hb", "mac": "AA0001", "soc": 91}]
    assert b.sent == a.sent


async def test_hub_drops_dead_socket_and_keeps_going():
    hub = WsHub()
    a, b = FakeWs(), FakeWs()
    hub.add(a)
    hub.add(b)
    a.closed = True
    await hub.broadcast("evt", {"n": 1})
    await hub.broadcast("evt", {"n": 2})
    assert a.sent == []
    assert [m["n"] for m in b.sent] == [1, 2]


async def test_hub_broadcast_soon_from_sync_context():
    hub = WsHub()
    a = FakeWs()
    hub.add(a)
    hub.broadcast_soon("charging", {"count": 3})
    for _ in range(3):
        await asyncio.sleep(0)
    assert a.sent == [{"kind": "charging", "count": 3}]
