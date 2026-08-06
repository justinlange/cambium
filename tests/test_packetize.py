from pathlib import Path

import pytest

from cambium.downlink.packetize import FLAG_MICRO_LEASE, HeaderStamper, frame_to_packets
from cambium.model import Fixture, FixtureClass, FixtureFrame, RGBW
from cambium.roster import Roster
from cambium.wire.packets import DirectFrame, parse_packet, short_id_to_str

BENCH10 = Path(__file__).parent.parent / "config" / "roster-bench10.csv"


def make_roster(n):
    # Synthesized fleet-scale roster; macs sort in construction order.
    return Roster(
        [Fixture(f"{0xB00000 + i:06X}", f"P{i:03d}", FixtureClass.DOWNLIGHT, None)
         for i in range(n)]
    )


def full_frame(roster, seq=1):
    return FixtureFrame(
        seq=seq,
        colors={f.mac: RGBW(i % 256, 0, 0, 0) for i, f in enumerate(roster.fixtures)},
    )


def parsed(raws):
    out = [parse_packet(r) for r in raws]
    assert all(isinstance(p, DirectFrame) for p in out)
    return out


# ---- HeaderStamper ---------------------------------------------------------

def test_stamper_seq_starts_at_1_and_uptime_from_clock():
    t = [100.0]
    stamper = HeaderStamper(clock=lambda: t[0])
    t[0] = 100.5
    h1 = stamper.stamp()
    t[0] = 101.25
    h2 = stamper.stamp()
    assert (h1.seq, h2.seq) == (1, 2)
    assert (h1.uptime_ms, h2.uptime_ms) == (500, 1250)
    assert h1.src_id == b"\x00\x00\x00"  # until the bridge STATUS HELLO


def test_stamper_set_src_id():
    stamper = HeaderStamper(clock=lambda: 0.0)
    stamper.set_src_id(b"\xf2\xbe\xd4")
    assert stamper.stamp().src_id == b"\xf2\xbe\xd4"
    with pytest.raises(ValueError) as e:
        stamper.set_src_id(b"\x12")
    assert "3 bytes" in str(e.value) and "bridge MAC" in str(e.value)


# ---- frame_to_packets ------------------------------------------------------

def test_bench10_is_one_packet():
    roster = Roster.load(BENCH10)
    raws = frame_to_packets(full_frame(roster), roster, HeaderStamper(lambda: 0.0))
    assert len(raws) == 1
    (df,) = parsed(raws)
    assert [short_id_to_str(e.id) for e in df.entries] == sorted(roster.by_mac)
    assert df.count == 10


def test_130_fixtures_is_eight_packets_under_wire_cap():
    roster = make_roster(130)
    raws = frame_to_packets(full_frame(roster), roster, HeaderStamper(lambda: 0.0))
    assert len(raws) == 8  # ceil(130 / 18)
    dfs = parsed(raws)
    assert [df.count for df in dfs] == [18] * 7 + [4]
    # Load-bearing: fixture RX buffer margin caps packets at 145 B on the wire.
    assert all(len(r) <= 145 for r in raws)


def test_chunk_membership_matches_tx_partition_and_is_stable():
    roster = make_roster(130)
    expected = [[f.mac for f in chunk] for chunk in roster.tx_partition()]

    def memberships():
        raws = frame_to_packets(
            full_frame(roster), roster, HeaderStamper(lambda: 0.0)
        )
        return [[short_id_to_str(e.id) for e in df.entries] for df in parsed(raws)]

    assert memberships() == expected
    assert memberships() == expected  # stable across rebuilds


def test_entries_only_for_macs_present_in_frame():
    roster = make_roster(130)
    part = roster.tx_partition()
    # Address 3 fixtures in chunk 0 and 2 in chunk 5; every other chunk is
    # empty and must produce NO packet at all.
    macs = [f.mac for f in part[0][:3]] + [f.mac for f in part[5][8:10]]
    frame = FixtureFrame(seq=1, colors={m: RGBW(1, 2, 3, 4) for m in macs})
    raws = frame_to_packets(frame, roster, HeaderStamper(lambda: 0.0))
    dfs = parsed(raws)
    assert [df.count for df in dfs] == [3, 2]
    assert [short_id_to_str(e.id) for df in dfs for e in df.entries] == macs
    e = dfs[0].entries[0]
    assert (e.r, e.g, e.b, e.w) == (1, 2, 3, 4)


def test_empty_frame_yields_no_packets():
    roster = make_roster(130)
    assert frame_to_packets(
        FixtureFrame(seq=1), roster, HeaderStamper(lambda: 0.0)
    ) == []


def test_micro_lease_bit_set_by_default_and_overridable():
    roster = Roster.load(BENCH10)
    frame = full_frame(roster)
    (df,) = parsed(frame_to_packets(frame, roster, HeaderStamper(lambda: 0.0)))
    assert df.flags & FLAG_MICRO_LEASE
    (df0,) = parsed(
        frame_to_packets(frame, roster, HeaderStamper(lambda: 0.0), flags=0)
    )
    assert df0.flags == 0


def test_header_seq_monotonic_across_frames():
    roster = make_roster(130)
    stamper = HeaderStamper(clock=lambda: 0.0)
    first = parsed(frame_to_packets(full_frame(roster, seq=1), roster, stamper))
    second = parsed(frame_to_packets(full_frame(roster, seq=2), roster, stamper))
    assert [df.h.seq for df in first] == list(range(1, 9))
    assert [df.h.seq for df in second] == list(range(9, 17))
