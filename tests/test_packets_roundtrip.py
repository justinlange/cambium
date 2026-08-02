"""Build -> parse roundtrips, plus decode of HAND-PACKED wire bytes.

The heartbeat/choreo/legacy-form bytes below are written by hand from the
struct layouts in packet.h (field-by-field struct.pack calls), NOT derived
from cambium.wire.packets -- so a layout bug in the module cannot hide by
agreeing with itself.
"""

import struct

import pytest

from cambium.wire import packets
from cambium.wire.packets import (
    BROADCAST_ID,
    ChoreoState,
    DirectFrame,
    ForceLifecycle,
    Heartbeat,
    Identify,
    NbHeader,
    NbType,
    ProgramSet,
    ShowFrame,
    Unknown,
    parse_packet,
    short_id_from_str,
    short_id_to_str,
)

H = NbHeader(src_id=b"\xaa\xbb\xcc", seq=42, uptime_ms=10_000)


def _hand_header(ptype: int) -> bytes:
    # ver(1) type(1) src_id(3) seq(u32 LE) uptime_ms(u32 LE) -- written from
    # the packet.h declaration, independent of NbHeader.pack().
    return struct.pack("<BB3sII", 1, ptype, b"\xaa\xbb\xcc", 42, 10_000)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def test_header_pack_matches_hand_layout():
    h = NbHeader(ver=1, type=6, src_id=b"\xaa\xbb\xcc", seq=42, uptime_ms=10_000)
    assert h.pack() == _hand_header(6)
    assert NbHeader.unpack(h.pack()) == h


def test_builder_stamps_type_over_stale_header():
    stale = NbHeader(type=99, src_id=b"\xaa\xbb\xcc", seq=42, uptime_ms=10_000)
    assert packets.identify(stale, BROADCAST_ID, 5)[1] == NbType.IDENTIFY


# ---------------------------------------------------------------------------
# Heartbeat: hand-packed, both wire lengths
# ---------------------------------------------------------------------------

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
    raw += struct.pack("<IHHhBh", 0xFFFFFFFF, 111, 222, -321, 255, -32768)
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
    # tail 13: fixture-era diagnostics
    raw += struct.pack("<BBBBH", 1, 3, 2, 7, 245)
    return raw


def _check_base_and_tail1(hb: Heartbeat) -> None:
    assert hb.h.src_id == b"\xaa\xbb\xcc"
    assert hb.h.seq == 42
    assert hb.h.uptime_ms == 10_000
    assert hb.batt_mv == 3700
    assert hb.batt_ma == -250  # signed: discharge current
    assert hb.soc_pct == 87
    assert hb.reset_reason == 3
    assert hb.ca_state == 1
    assert hb.mode == 2
    assert hb.dl_pdr_x1000 == 987
    assert hb.dl_rssi == -71  # signed int8
    assert hb.supply_mv == 5030
    assert hb.supply_ma == -12
    assert hb.supply_good == 1


def test_heartbeat_short():
    raw = _hand_hb_short()
    assert len(raw) == 29  # NB_HB_SHORT_LEN
    hb = parse_packet(raw)
    assert isinstance(hb, Heartbeat)
    _check_base_and_tail1(hb)
    # everything past tail 1 is absent, not zero
    assert hb.lux_x10 is None
    assert hb.fw_rev is None
    assert hb.field_phase is None
    assert hb.profile is None
    assert hb.night_min is None


def test_heartbeat_full():
    raw = _hand_hb_full()
    assert len(raw) == 148  # sizeof(NbHeartbeat)
    hb = parse_packet(raw)
    assert isinstance(hb, Heartbeat)
    _check_base_and_tail1(hb)
    # tail 2
    assert hb.lux_x10 == 0xFFFFFFFF  # absent-sensor sentinel survives as u32
    assert hb.light_ch0 == 111
    assert hb.light_ch1 == 222
    assert hb.ptemp_cx10 == -321
    assert hb.prh_pct == 255
    assert hb.btemp_cx10 == -32768  # INT16_MIN absent sentinel, signed decode
    # tail 3
    assert hb.ina_pv_mv == -1
    assert hb.ina_pa_ma == -2
    assert hb.ina_bv_mv == -3
    assert hb.ina_ba_ma == -4
    # tail 4
    assert hb.cfg_cap_mah == 3000
    assert hb.cfg_charge_ma == 500
    # tail 5
    assert hb.drawdown_mah_x10 == 15
    assert hb.drawdown_budget_mah == 100
    assert hb.drawdown_active == 0
    # tail 6
    assert hb.fw_rev == "fx-1.4.2"
    # tail 7
    assert hb.maint_status == 4
    # tail 8
    assert hb.field_phase == 5
    assert hb.field_reason == 6
    assert hb.field_cycle == 77
    assert hb.field_elapsed_s == 88
    assert hb.field_charge_mah == 99
    assert hb.field_discharge_mah == 110
    assert hb.field_min_mv == 3300
    assert hb.field_max_mv == 4200
    # tail 9
    assert hb.bq_vindpm_mv == 4600
    assert hb.bq_ichg_ma == 512
    assert hb.bq_vreg_mv == 4200
    assert hb.bq_reg16 == 0x16
    assert hb.bq_reg18 == 0x18
    assert hb.bq_stat0 == 1
    assert hb.bq_stat1 == 2
    assert hb.bq_fault0 == 3
    assert hb.bq_flag0 == 4
    assert hb.bq_flag1 == 5
    assert hb.bq_fault_flag0 == 6
    assert hb.bq_part == 7
    # tail 10
    assert hb.field_charge_wh_x10 == 10
    assert hb.field_discharge_wh_x10 == 20
    assert hb.field_peak_panel_w_x100 == 30
    assert hb.field_peak_charge_w_x100 == 40
    assert hb.field_peak_draw_w_x100 == 50
    assert hb.field_low_s == 1
    assert hb.field_charge_min == 2
    assert hb.field_wait_min == 3
    assert hb.field_draw_min == 4
    assert hb.field_protect_min == 5
    # tail 11
    assert hb.mppt_status == 1
    assert hb.mppt_reason == 2
    assert hb.mppt_runs == 3
    assert hb.mppt_active_v10 == 46
    assert hb.mppt_best_v10 == 48
    assert hb.mppt_last_v10 == 50
    assert hb.mppt_p46_w_x100 == 460
    assert hb.mppt_p48_w_x100 == 480
    assert hb.mppt_p50_w_x100 == 500
    # tail 12
    assert hb.field_load_dimmed == 1
    assert hb.field_protect_latched == 0
    # tail 13
    assert hb.profile == 1
    assert hb.life_state == 3
    assert hb.power_tier == 2
    assert hb.active_program == 7
    assert hb.night_min == 245


def test_heartbeat_bench_era_142():
    # A maxed-out bench heartbeat is byte-identical to hb-full truncated at
    # `profile`: tail 13 must gate to None, tail 12 must still decode.
    hb = parse_packet(_hand_hb_full()[:142])
    assert isinstance(hb, Heartbeat)
    assert hb.field_protect_latched == 0
    assert hb.profile is None
    assert hb.night_min is None


# ---------------------------------------------------------------------------
# ShowFrame
# ---------------------------------------------------------------------------

def test_show_frame_full_roundtrip():
    raw = packets.show_frame(
        H, 1234, 96, 1, val=200, bright=180, effect=2, beat_phase=64, energy=99
    )
    assert len(raw) == 22
    sf = parse_packet(raw)
    assert isinstance(sf, ShowFrame)
    assert (sf.phase, sf.hue, sf.flags) == (1234, 96, 1)
    assert (sf.val, sf.bright, sf.effect, sf.beat_phase, sf.energy) == (200, 180, 2, 64, 99)


def test_show_frame_legacy_17():
    raw = packets.show_frame(H, 7, 8, legacy=True)
    assert len(raw) == 17
    # hand-check the body layout: phase(u16 LE) hue flags
    assert raw[13:] == struct.pack("<HBB", 7, 8, 0)
    sf = parse_packet(raw)
    assert isinstance(sf, ShowFrame)
    assert (sf.phase, sf.hue, sf.flags) == (7, 8, 0)
    # absent tail -> legacy defaults (val 255 = full, receiver parity)
    assert (sf.val, sf.bright, sf.effect, sf.beat_phase, sf.energy) == (255, 255, 0, 0, 0)


def test_show_frame_partial_tail_gated_per_field():
    # A frame truncated mid-tail is valid wire under the append-only doctrine.
    raw = _hand_header(2) + struct.pack("<HBB", 5, 6, 0) + bytes([10, 20, 30])  # 20 B
    sf = parse_packet(raw)
    assert isinstance(sf, ShowFrame)
    assert (sf.val, sf.bright, sf.effect) == (10, 20, 30)
    assert (sf.beat_phase, sf.energy) == (0, 0)  # absent -> defaults


# ---------------------------------------------------------------------------
# Identify
# ---------------------------------------------------------------------------

def test_identify_roundtrip_19():
    target = short_id_from_str("F2BDB4")
    raw = packets.identify(H, target, 30, color=2, blink=1)
    assert len(raw) == 19
    ident = parse_packet(raw)
    assert isinstance(ident, Identify)
    assert ident.target_id == target
    assert (ident.secs, ident.color, ident.blink) == (30, 2, 1)


def test_identify_secs_zero_is_cancel():
    ident = parse_packet(packets.identify(H, BROADCAST_ID, 0))
    assert isinstance(ident, Identify)
    assert ident.secs == 0


def test_identify_legacy_17_hand_packed():
    raw = _hand_header(6) + struct.pack("<3sB", b"\xf2\xbd\xb4", 10)
    assert len(raw) == 17
    ident = parse_packet(raw)
    assert isinstance(ident, Identify)
    assert (ident.secs, ident.color, ident.blink) == (10, 0, 0)  # legacy blink pattern


def test_identify_rejects_bad_color():
    with pytest.raises(ValueError):
        packets.identify(H, BROADCAST_ID, 5, color=6)


# ---------------------------------------------------------------------------
# ChoreoState / ProgramSet
# ---------------------------------------------------------------------------

def test_choreo_state_hand_packed():
    raw = _hand_header(18) + struct.pack("<BHBBHBB", 3, 60000, 0x12, 200, 1500, 0b101, 0)
    assert len(raw) == 22
    cs = parse_packet(raw)
    assert isinstance(cs, ChoreoState)
    assert (cs.program_id, cs.generation, cs.state) == (3, 60000, 0x12)
    assert (cs.intensity, cs.phase_ms, cs.flags, cs.reserved) == (200, 1500, 0b101, 0)


def test_program_set_roundtrip():
    raw = packets.program_set(
        H, b"\xf2\xbd\xb4", 4, 300, seed=0xDEADBEEF, flags=1, params=b"\x01\x02"
    )
    assert len(raw) == 32
    ps = parse_packet(raw)
    assert isinstance(ps, ProgramSet)
    assert ps.target_id == b"\xf2\xbd\xb4"
    assert (ps.program_id, ps.lease_s, ps.seed, ps.flags) == (4, 300, 0xDEADBEEF, 1)
    assert ps.params == b"\x01\x02" + bytes(6)  # zero-padded to 8


def test_program_set_rejects_oversize_params():
    with pytest.raises(ValueError):
        packets.program_set(H, BROADCAST_ID, 1, 60, params=bytes(9))


# ---------------------------------------------------------------------------
# NbCmd shells
# ---------------------------------------------------------------------------

def test_cmd_shells_exact_bytes():
    assert packets.set_rate(H, 5) == _hand_header(5) + b"\x05"
    assert packets.enter_maint(H) == _hand_header(3) + b"\x00"
    assert packets.resume(H) == _hand_header(4) + b"\x00"


# ---------------------------------------------------------------------------
# Proposed type 25: DirectFrame
# ---------------------------------------------------------------------------

def test_direct_frame_roundtrip():
    entries = [
        (short_id_from_str("F2BDB4"), 255, 0, 0, 0),
        (short_id_from_str("F4031C"), 0, 128, 64, 32),
    ]
    raw = packets.direct_frame(H, entries, flags=0b11)
    assert len(raw) == 15 + 7 * 2
    # hand-check the layout: flags@13 count@14 entries@15
    assert raw[13] == 0b11
    assert raw[14] == 2
    assert raw[15:22] == b"\xf2\xbd\xb4" + bytes([255, 0, 0, 0])
    assert raw[22:29] == b"\xf4\x03\x1c" + bytes([0, 128, 64, 32])
    df = parse_packet(raw)
    assert isinstance(df, DirectFrame)
    assert df.flags == 0b11
    assert df.count == 2
    assert [(e.id, e.r, e.g, e.b, e.w) for e in df.entries] == [
        (b"\xf2\xbd\xb4", 255, 0, 0, 0),
        (b"\xf4\x03\x1c", 0, 128, 64, 32),
    ]


def test_direct_frame_empty_and_max():
    assert len(packets.direct_frame(H, [])) == 15
    full = [(b"\x00\x00\x01", 1, 2, 3, 4)] * 18
    assert len(packets.direct_frame(H, full)) == 141


def test_direct_frame_rejects_too_many():
    with pytest.raises(ValueError):
        packets.direct_frame(H, [(b"\x00\x00\x01", 0, 0, 0, 0)] * 19)


def test_direct_frame_parse_is_liberal_on_short_body():
    # count byte claims 3 entries but only 1 fits the length: decode what's
    # there (firmware-style length gating), never raise.
    raw = _hand_header(25) + bytes([0, 3]) + b"\x01\x02\x03" + bytes([9, 8, 7, 6])
    df = parse_packet(raw)
    assert isinstance(df, DirectFrame)
    assert df.count == 3
    assert len(df.entries) == 1
    assert df.entries[0].id == b"\x01\x02\x03"


# ---------------------------------------------------------------------------
# Proposed type 26: ForceLifecycle
# ---------------------------------------------------------------------------

def test_force_lifecycle_roundtrip():
    raw = packets.force_lifecycle(H, b"\xf2\xbd\xb4", 1)
    assert len(raw) == 18
    assert raw[13:16] == b"\xf2\xbd\xb4"
    assert raw[16] == 1
    assert raw[17] == 0
    fl = parse_packet(raw)
    assert isinstance(fl, ForceLifecycle)
    assert (fl.target_id, fl.mode, fl.flags) == (b"\xf2\xbd\xb4", 1, 0)


def test_force_lifecycle_rejects_bad_mode():
    with pytest.raises(ValueError):
        packets.force_lifecycle(H, BROADCAST_ID, 3)


# ---------------------------------------------------------------------------
# Unknown / hostile input never raises
# ---------------------------------------------------------------------------

def test_unknown_type_and_reserved_types():
    for ptype in (7, 20, 22, 23, 99, 255):
        raw = _hand_header(ptype) + bytes(8)
        result = parse_packet(raw)
        assert isinstance(result, Unknown)
        assert result.type == ptype
        assert result.raw == raw


def test_short_and_garbage_input():
    assert isinstance(parse_packet(b""), Unknown)
    assert isinstance(parse_packet(b"\x01"), Unknown)
    assert isinstance(parse_packet(bytes(12)), Unknown)
    # known type but body below its legacy minimum -> Unknown, not a crash
    assert isinstance(parse_packet(_hand_header(2) + b"\x00"), Unknown)
    assert isinstance(parse_packet(_hand_header(1) + bytes(5)), Unknown)


def test_foreign_proto_version_is_unknown():
    raw = bytes([2]) + _hand_header(2)[1:]  # ver=2: flag day, offsets untrusted
    assert isinstance(parse_packet(raw), Unknown)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_short_id_helpers():
    assert short_id_to_str(b"\xf2\xbd\xb4") == "F2BDB4"
    assert short_id_from_str("F2BDB4") == b"\xf2\xbd\xb4"
    assert short_id_from_str("f2:bd:b4") == b"\xf2\xbd\xb4"
    assert short_id_from_str("F2-BD-B4") == b"\xf2\xbd\xb4"
    with pytest.raises(ValueError):
        short_id_to_str(b"\xf2\xbd")
    with pytest.raises(ValueError):
        short_id_from_str("F2BD")
    with pytest.raises(ValueError):
        short_id_from_str("zzzzzz")


def test_target_matches():
    me = b"\xf2\xbf\xa0"
    assert packets.target_matches(BROADCAST_ID, me)
    assert packets.target_matches(me, me)
    assert not packets.target_matches(b"\xf4\x03\x1c", me)
