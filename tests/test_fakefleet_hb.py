"""VirtualFixture heartbeats: exact truncation lengths, production-parser
round-trip, cadence, and edge-triggered full heartbeats."""

import struct

from cambium.fakefleet.fixture_sim import VirtualFixture
from cambium.model import FixtureClass
from cambium.uplink.parse import PeerPacket, UplinkParser
from cambium.wire.framing import FTYPE_RADIO_RX
from cambium.wire.packets import (
    NbHeader,
    force_lifecycle,
    short_id_from_str,
)


class Clock:
    def __init__(self, t: float = 50.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(clock) -> VirtualFixture:
    return VirtualFixture("0A0001", FixtureClass.DOWNLIGHT, clock=clock)


def rx_payload(vf: VirtualFixture, hb: bytes) -> bytes:
    mac6 = b"\x68\xee\x8f" + bytes.fromhex(vf.mac)
    return mac6 + struct.pack("<b", vf.dl_rssi) + hb


def test_boot_heartbeat_is_full_then_short_cadence():
    clock = Clock()
    f = make(clock)
    first = f.next_uplink()
    assert len(first) == 1 and len(first[0]) == 148  # boot -> hb-full
    assert f.next_uplink() == []  # nothing due yet
    clock.t += 1.0
    (short,) = f.next_uplink()
    assert len(short) == 29  # NB_HB_SHORT_LEN exactly
    clock.t += 60.0
    (full,) = f.next_uplink()
    assert len(full) == 148  # 60 s -> hb-full again


def test_life_state_edge_forces_full_heartbeat():
    clock = Clock()
    f = make(clock)
    f.next_uplink()  # drain boot full
    clock.t += 0.2  # nothing due on cadence
    f.consume(
        force_lifecycle(
            NbHeader(src_id=short_id_from_str("FAB000")),
            short_id_from_str("0A0001"),
            1,
        )
    )
    (edge,) = f.next_uplink()
    assert len(edge) == 148  # state change -> full, off-cadence


def test_production_parser_roundtrip_both_lengths():
    clock = Clock()
    f = make(clock)
    f.batt_ma = -120
    parser = UplinkParser()
    (full,) = f.next_uplink()
    clock.t += 1.0
    (short,) = f.next_uplink()

    ev_full = parser.feed(FTYPE_RADIO_RX, rx_payload(f, full))
    assert isinstance(ev_full, PeerPacket) and ev_full.mac_short == "0A0001"
    hb = ev_full.packet
    assert hb.batt_ma == -120  # signedness survives end to end
    assert hb.life_state == 1  # LIFE_DAY_CHARGE; tail 13 present in hb-full
    assert hb.fw_rev == "fake-fleet-0.1"

    ev_short = parser.feed(FTYPE_RADIO_RX, rx_payload(f, short))
    hb_s = ev_short.packet
    assert hb_s.batt_ma == -120
    assert hb_s.life_state is None  # truncated before tail 13
    assert hb_s.supply_good is not None  # hb-short still carries tail 1


def test_seq_monotonic_across_heartbeats():
    clock = Clock()
    f = make(clock)
    seqs = []
    for _ in range(4):
        for hb in f.next_uplink():
            (seq,) = struct.unpack_from("<I", hb, 5)
            seqs.append(seq)
        clock.t += 1.0
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_charging_flag_reaches_parser():
    clock = Clock()
    f = make(clock)
    f.batt_ma = 250  # solar charging
    parser = UplinkParser()
    (full,) = f.next_uplink()
    ev = parser.feed(FTYPE_RADIO_RX, rx_payload(f, full))
    assert ev.packet.batt_ma == 250  # FleetState counts batt_ma > 0 as charging
