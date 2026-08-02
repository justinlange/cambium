"""Uplink demux: RADIO_RX payloads hand-packed from the framing docstring
(mac[6] + rssi:i8 + raw Nb packet), with heartbeats hand-built at BOTH wire
lengths (29 hb-short / 148 hb-full) so field decode -- signedness included --
is checked against packet.h, not against cambium's own builders."""

import struct

from cambium.uplink.parse import (
    BridgeLog,
    BridgeStatusEvent,
    PeerPacket,
    UplinkParser,
)
from cambium.wire import packets
from cambium.wire.framing import (
    FTYPE_CTRL,
    FTYPE_LOG,
    FTYPE_RADIO_RX,
    FTYPE_RADIO_TX,
    FTYPE_STATUS,
)
from cambium.wire.packets import ChoreoState, Heartbeat, NbHeader

# Full sender MAC as ESP-NOW reports it; fleet identity is the last 3 bytes.
MAC6 = bytes([0xD8, 0x85, 0xAC, 0x9E, 0x5A, 0xE8])
SRC = MAC6[3:]  # short id 9E5AE8


def _rx(raw: bytes, rssi: int = -55, mac6: bytes = MAC6) -> bytes:
    # RADIO_RX payload per wire/framing.py: mac[6] + rssi:i8 + raw Nb packet.
    return mac6 + struct.pack("<b", rssi) + raw


def _hand_header(ptype: int) -> bytes:
    # ver(1) type(1) src_id(3) seq(u32 LE) uptime_ms(u32 LE), from packet.h.
    return struct.pack("<BB3sII", 1, ptype, SRC, 42, 10_000)


def _hand_hb_short() -> bytes:
    raw = _hand_header(1)
    # base: batt_mv batt_ma soc reset ca mode dl_pdr dl_rssi
    raw += struct.pack("<hhBBBBHb", 3700, -250, 87, 3, 1, 2, 987, -71)
    # tail 1: supply_mv supply_ma supply_good
    raw += struct.pack("<hhB", 5030, -12, 1)
    return raw


def _hand_hb_full() -> bytes:
    raw = _hand_hb_short()
    # tail 2: lux_x10 light_ch0 light_ch1 ptemp_cx10 prh_pct btemp_cx10
    raw += struct.pack("<IHHhBh", 12345, 111, 222, -321, 44, -15)
    # tail 3: INA meters
    raw += struct.pack("<hhhh", -1, -2, -3, -4)
    # tail 4: cfg_cap_mah cfg_charge_ma
    raw += struct.pack("<HH", 3000, 500)
    # tail 5: drawdown
    raw += struct.pack("<HHB", 15, 100, 0)
    # tail 6: fw_rev char[24]
    raw += b"fx-1.4.2".ljust(24, b"\x00")
    # tail 7: maint_status
    raw += struct.pack("<B", 4)
    # tail 8: lifecycle summary
    raw += struct.pack("<BBHHHHHH", 5, 6, 77, 88, 99, 110, 3300, 4200)
    # tail 9: BQ25628E truth
    raw += struct.pack("<HHHBBBBBBBBB", 4600, 512, 4200, 0x16, 0x18, 1, 2, 3, 4, 5, 6, 7)
    # tail 10: energy summary
    raw += struct.pack("<HHHHHBBBBB", 10, 20, 30, 40, 50, 1, 2, 3, 4, 5)
    # tail 11: MPPT
    raw += struct.pack("<BBBBBBHHH", 1, 2, 3, 46, 48, 50, 460, 480, 500)
    # tail 12: latches
    raw += struct.pack("<BB", 1, 0)
    # tail 13: fixture-era diagnostics: profile life_state power_tier program night_min
    raw += struct.pack("<BBBBH", 1, 3, 2, 7, 245)
    return raw


# ---------------------------------------------------------------------------
# RADIO_RX -> PeerPacket
# ---------------------------------------------------------------------------

def test_heartbeat_short_exact_decode():
    raw = _hand_hb_short()
    assert len(raw) == 29  # NB_HB_SHORT_LEN
    p = UplinkParser()
    evt = p.feed(FTYPE_RADIO_RX, _rx(raw, rssi=-55))
    assert isinstance(evt, PeerPacket)
    assert evt.mac_short == "9E5AE8"  # from the last 3 bytes of mac[6]
    assert evt.rssi == -55  # signed int8
    hb = evt.packet
    assert isinstance(hb, Heartbeat)
    assert hb.batt_mv == 3700
    assert hb.batt_ma == -250  # signed: discharge current
    assert hb.soc_pct == 87
    assert hb.mode == 2
    assert hb.dl_rssi == -71  # signed int8
    assert hb.supply_mv == 5030
    assert hb.supply_ma == -12
    # everything past tail 1 is absent, not zero
    assert hb.life_state is None
    assert hb.active_program is None
    assert hb.power_tier is None
    assert p.malformed == 0 and p.dropped == 0


def test_heartbeat_full_exact_decode():
    raw = _hand_hb_full()
    assert len(raw) == 148  # sizeof(NbHeartbeat)
    evt = UplinkParser().feed(FTYPE_RADIO_RX, _rx(raw, rssi=4))
    assert isinstance(evt, PeerPacket)
    assert evt.rssi == 4  # positive rssi survives the signed decode
    hb = evt.packet
    assert isinstance(hb, Heartbeat)
    assert hb.batt_ma == -250
    assert hb.ptemp_cx10 == -321  # signed tail field
    assert hb.fw_rev == "fx-1.4.2"
    # hb-full tail 13
    assert hb.profile == 1
    assert hb.life_state == 3
    assert hb.power_tier == 2
    assert hb.active_program == 7
    assert hb.night_min == 245


def test_choreo_state_decode():
    raw = _hand_header(18) + struct.pack("<BHBBHBB", 3, 60000, 2, 200, 1500, 1, 0)
    evt = UplinkParser().feed(FTYPE_RADIO_RX, _rx(raw))
    assert isinstance(evt, PeerPacket)
    assert evt.mac_short == "9E5AE8"
    cs = evt.packet
    assert isinstance(cs, ChoreoState)
    assert (cs.program_id, cs.generation, cs.state) == (3, 60000, 2)
    assert (cs.intensity, cs.phase_ms) == (200, 1500)


def test_mac_short_comes_from_mac6_not_header():
    # Header claims src 9E5AE8, but ESP-NOW saw a different sender MAC; the
    # radio's truth wins over the (spoofable) header field.
    other = bytes([0x24, 0x6F, 0x28, 0xF2, 0xBD, 0xB4])
    evt = UplinkParser().feed(FTYPE_RADIO_RX, _rx(_hand_hb_short(), mac6=other))
    assert isinstance(evt, PeerPacket)
    assert evt.mac_short == "F2BDB4"


def test_overheard_downlink_types_counted_and_dropped():
    # Our own broadcasts echo back off the air; builders make realistic bytes.
    p = UplinkParser()
    h = NbHeader(src_id=SRC, seq=1)
    assert p.feed(FTYPE_RADIO_RX, _rx(packets.show_frame(h, 1, 2))) is None
    assert p.feed(FTYPE_RADIO_RX, _rx(packets.identify(h, packets.BROADCAST_ID, 5))) is None
    assert p.dropped == 2
    assert p.malformed == 0


def test_garbage_inner_packet_counted_not_raised():
    p = UplinkParser()
    assert p.feed(FTYPE_RADIO_RX, _rx(b"\xff\xfe\xfd\xfc")) is None  # not even a header
    assert p.feed(FTYPE_RADIO_RX, _rx(_hand_header(99) + bytes(8))) is None  # unknown type
    assert p.feed(FTYPE_RADIO_RX, _rx(b"")) is None  # empty inner packet
    assert p.dropped == 3


def test_radio_rx_too_short_is_malformed():
    p = UplinkParser()
    for n in range(7):  # anything below mac[6]+rssi can't be demuxed at all
        assert p.feed(FTYPE_RADIO_RX, bytes(n)) is None
    assert p.malformed == 7
    assert p.dropped == 0
    # parser still works afterwards
    assert isinstance(p.feed(FTYPE_RADIO_RX, _rx(_hand_hb_short())), PeerPacket)


# ---------------------------------------------------------------------------
# STATUS / LOG demux
# ---------------------------------------------------------------------------

def _hand_status() -> bytes:
    # proto:u8 mac[6] channel:u8 uptime:u32 tx_ok:u32 tx_fail:u32 rx_pkts:u32
    # rx_drop:u32 crc_err:u16 fw:char[16] zero-padded (framing docstring)
    return (
        bytes([1])
        + bytes([0x24, 0x6F, 0x28, 0xF2, 0xBE, 0xD4])
        + bytes([11])
        + (123456).to_bytes(4, "little")
        + (1000).to_bytes(4, "little")
        + (7).to_bytes(4, "little")
        + (2500).to_bytes(4, "little")
        + (3).to_bytes(4, "little")
        + (9).to_bytes(2, "little")
        + b"bridge-0.1.0".ljust(16, b"\x00")
    )


def test_status_demux_with_injected_clock():
    p = UplinkParser(clock=lambda: 123.5)
    evt = p.feed(FTYPE_STATUS, _hand_status())
    assert isinstance(evt, BridgeStatusEvent)
    assert evt.received_at == 123.5
    assert evt.status.channel == 11
    assert evt.status.tx_ok == 1000
    assert evt.status.fw == "bridge-0.1.0"
    # append-only doctrine: unknown STATUS tail is fine
    assert isinstance(p.feed(FTYPE_STATUS, _hand_status() + b"\xde\xad"), BridgeStatusEvent)
    assert p.malformed == 0


def test_status_too_short_counted_not_raised():
    p = UplinkParser()
    assert p.feed(FTYPE_STATUS, bytes(45)) is None
    assert p.malformed == 1


def test_log_demux():
    evt = UplinkParser().feed(FTYPE_LOG, b"boot ok ch=11\r\n")
    assert isinstance(evt, BridgeLog)
    assert evt.text == "boot ok ch=11"


def test_log_with_garbage_bytes_never_raises():
    evt = UplinkParser().feed(FTYPE_LOG, b"\xfftemp\x80")
    assert isinstance(evt, BridgeLog)
    assert "temp" in evt.text


def test_unknown_and_downlink_ftypes_counted():
    p = UplinkParser()
    assert p.feed(FTYPE_RADIO_TX, _hand_hb_short()) is None  # downlink echo
    assert p.feed(FTYPE_CTRL, b"\x01") is None
    assert p.feed(0x7F, b"whatever") is None
    assert p.unknown_ftypes == 3
    assert p.malformed == 0 and p.dropped == 0
