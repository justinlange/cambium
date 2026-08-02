"""FleetState behavior under a fake clock: online/offline transitions,
charging edge-trigger + 1/s rate limit, choreo edge events, listener
exception isolation, and snapshot shape with and without a roster."""

from cambium.model import Fixture, FixtureClass
from cambium.roster import Roster
from cambium.uplink.fleetstate import FleetState
from cambium.uplink.parse import BridgeLog, BridgeStatusEvent, PeerPacket
from cambium.wire.framing import BridgeStatus
from cambium.wire.packets import ChoreoState, Heartbeat, NbHeader, short_id_from_str

MAC_A = "9E5AE8"
MAC_B = "F2BDB4"


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _hb(
    mac: str = MAC_A,
    batt_ma: int = -100,
    *,
    life_state: int | None = None,
    program: int | None = None,
    power_tier: int | None = None,
    batt_mv: int = 3700,
    soc: int = 80,
    rssi: int = -55,
) -> PeerPacket:
    hb = Heartbeat(
        h=NbHeader(src_id=short_id_from_str(mac)),
        batt_mv=batt_mv,
        batt_ma=batt_ma,
        soc_pct=soc,
        dl_rssi=-70,
        mode=2,
        life_state=life_state,
        active_program=program,
        power_tier=power_tier,
    )
    return PeerPacket(mac_short=mac, rssi=rssi, packet=hb)


def _choreo(
    mac: str = MAC_A,
    state: int = 1,
    program_id: int = 3,
    generation: int = 7,
    intensity: int = 200,
) -> PeerPacket:
    cs = ChoreoState(
        h=NbHeader(src_id=short_id_from_str(mac)),
        program_id=program_id,
        generation=generation,
        state=state,
        intensity=intensity,
        phase_ms=0,
        flags=0,
        reserved=0,
    )
    return PeerPacket(mac_short=mac, rssi=-60, packet=cs)


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))

    def of(self, kind: str) -> list[dict]:
        return [p for k, p in self.events if k == kind]


# ---------------------------------------------------------------------------
# Telemetry upsert + online/offline
# ---------------------------------------------------------------------------

async def test_heartbeat_upsert_keeps_full_tail_across_short_beats():
    clock = FakeClock()
    fleet = FleetState(clock=clock)
    await fleet.update(_hb(batt_ma=-250, life_state=3, program=7, power_tier=1))
    t = fleet.telemetry[MAC_A]
    assert (t.batt_ma, t.life_state, t.program, t.power_tier) == (-250, 3, 7, 1)

    # hb-short: tail-13 fields absent -> previous full-heartbeat truth kept,
    # base fields updated.
    clock.t = 5.0
    await fleet.update(_hb(batt_ma=120, batt_mv=3900))
    t = fleet.telemetry[MAC_A]
    assert (t.batt_mv, t.batt_ma) == (3900, 120)
    assert (t.life_state, t.program, t.power_tier) == (3, 7, 1)
    assert t.last_seen == 5.0


async def test_online_offline_transitions():
    clock = FakeClock()
    fleet = FleetState(clock=clock)
    await fleet.update(_hb())
    assert fleet.snapshot()[MAC_A]["online"] is True

    clock.t = 29.0
    assert fleet.snapshot()[MAC_A]["online"] is True  # within 30 s window
    clock.t = 31.0
    assert fleet.snapshot()[MAC_A]["online"] is False  # silent too long

    await fleet.update(_hb())  # node comes back
    assert fleet.snapshot()[MAC_A]["online"] is True


async def test_hb_listener_called_per_heartbeat():
    rec = Recorder()
    fleet = FleetState(clock=FakeClock())
    fleet.add_listener(rec)
    await fleet.update(_hb(batt_ma=-250))
    await fleet.update(_hb(batt_ma=-240))
    hbs = rec.of("hb")
    assert len(hbs) == 2
    assert hbs[0]["mac"] == MAC_A
    assert hbs[0]["batt_ma"] == -250
    assert hbs[0]["rssi"] == -55
    assert hbs[1]["batt_ma"] == -240


# ---------------------------------------------------------------------------
# Charging: edge-triggered + rate-limited
# ---------------------------------------------------------------------------

async def test_charging_edge_trigger_and_rate_limit():
    clock = FakeClock()
    rec = Recorder()
    fleet = FleetState(clock=clock)
    fleet.add_listener(rec)

    # first charger -> immediate edge
    await fleet.update(_hb(MAC_A, batt_ma=500))
    assert rec.of("charging") == [{"count": 1, "macs": [MAC_A]}]

    # still charging, count unchanged -> no event
    await fleet.update(_hb(MAC_A, batt_ma=600))
    assert len(rec.of("charging")) == 1

    # count changes 1 -> 2 inside the 1 s window: suppressed by rate limit
    clock.t = 0.5
    await fleet.update(_hb(MAC_B, batt_ma=200))
    assert len(rec.of("charging")) == 1
    assert fleet.charging_count() == 2  # state is current even when unemitted

    # next heartbeat after the window flushes the pending change
    clock.t = 1.6
    await fleet.update(_hb(MAC_A, batt_ma=600))
    assert rec.of("charging")[-1] == {"count": 2, "macs": [MAC_A, MAC_B]}

    # A stops charging (discharge is negative) -> edge back down
    clock.t = 3.0
    await fleet.update(_hb(MAC_A, batt_ma=-50))
    assert rec.of("charging")[-1] == {"count": 1, "macs": [MAC_B]}
    assert len(rec.of("charging")) == 3


async def test_charging_is_strictly_positive_batt_ma():
    fleet = FleetState(clock=FakeClock())
    await fleet.update(_hb(MAC_A, batt_ma=0))  # idle, not charging
    await fleet.update(_hb(MAC_B, batt_ma=-10))
    assert fleet.charging_macs() == []
    assert fleet.charging_count() == 0


# ---------------------------------------------------------------------------
# Choreo edge events
# ---------------------------------------------------------------------------

async def test_choreo_edge_events():
    rec = Recorder()
    fleet = FleetState(clock=FakeClock())
    fleet.add_listener(rec)

    # first sighting counts as an edge (unknown -> state)
    await fleet.update(_choreo(state=1, program_id=3, generation=7, intensity=200))
    assert rec.of("evt") == [
        {
            "mac": MAC_A,
            "state": 1,
            "prev_state": None,
            "program": 3,
            "generation": 7,
            "intensity": 200,
        }
    ]

    # same state again -> no edge, but recorded values refresh
    await fleet.update(_choreo(state=1, intensity=210))
    assert len(rec.of("evt")) == 1
    assert fleet.snapshot()[MAC_A]["choreo"]["intensity"] == 210

    # state change -> edge with prev_state
    await fleet.update(_choreo(state=2, generation=8))
    evt = rec.of("evt")[-1]
    assert (evt["state"], evt["prev_state"], evt["generation"]) == (2, 1, 8)


# ---------------------------------------------------------------------------
# Listener isolation
# ---------------------------------------------------------------------------

async def test_listener_exception_isolated_and_counted():
    fleet = FleetState(clock=FakeClock())
    rec = Recorder()

    async def broken(kind: str, payload: dict) -> None:
        raise RuntimeError("half-closed websocket")

    fleet.add_listener(broken)  # added FIRST so it fails before rec runs
    fleet.add_listener(rec)
    await fleet.update(_hb(batt_ma=500))  # hb + charging: broken fails twice
    assert len(rec.of("hb")) == 1  # later listener still served
    assert len(rec.of("charging")) == 1
    assert fleet.listener_errors == 2


# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------

async def test_snapshot_without_roster():
    fleet = FleetState(clock=FakeClock())
    await fleet.update(_hb(MAC_A, batt_ma=-250))
    await fleet.update(_choreo(MAC_B))  # choreo-only node: seen, no telemetry
    snap = fleet.snapshot()
    assert set(snap) == {MAC_A, MAC_B}
    a = snap[MAC_A]
    assert a["online"] is True
    assert a["telemetry"]["batt_ma"] == -250
    assert a["choreo"] is None
    assert "fixture_id" not in a  # no roster attached -> no fixture_id key
    b = snap[MAC_B]
    assert b["online"] is True
    assert b["telemetry"] is None
    assert b["choreo"]["state"] == 1


async def test_snapshot_with_roster():
    roster = Roster([Fixture(MAC_A, "B003", FixtureClass.DOWNLIGHT, None)])
    fleet = FleetState(clock=FakeClock(), roster=roster)
    await fleet.update(_hb(MAC_A))
    await fleet.update(_hb(MAC_B))  # heard on air but not in the roster
    snap = fleet.snapshot()
    assert snap[MAC_A]["fixture_id"] == "B003"
    assert snap[MAC_B]["fixture_id"] is None


async def test_bridge_events_are_ignored():
    rec = Recorder()
    fleet = FleetState(clock=FakeClock())
    fleet.add_listener(rec)
    status = BridgeStatus(
        proto=1, mac=bytes(6), channel=11, uptime_ms=1, tx_ok=0, tx_fail=0,
        rx_pkts=0, rx_drop=0, crc_err=0, fw="bridge-0.1.0",
    )
    await fleet.update(BridgeStatusEvent(status=status, received_at=0.0))
    await fleet.update(BridgeLog(text="boot ok"))
    assert fleet.snapshot() == {}
    assert rec.events == []
