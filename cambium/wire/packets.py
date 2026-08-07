"""Byte-truth mirror of the fleet ESP-NOW wire protocol (protocol v1).

This module mirrors, field-by-field, the packed little-endian structs in
beneckart/resonance-lighting/firmware/fixture/src/core/packet.h. That header is the
single allocation authority; golden sizeof/offsetof pins live in
firmware/fixture/tests/test_packet_layout.cpp and are re-checked here via
tools/extract_packet_goldens.py -> tests/golden/packet_pins.json.

Append-only doctrine (same as the firmware):
  - NEVER reuse or renumber a type. NB_PROTO_VER stays 1.
  - Struct evolution is APPEND-ONLY: new fields go at the END. Receivers
    length-gate every tail field (NB_HAS_HB_FIELD in C, per-field offset
    checks here) and ignore unknown tails, which also makes send-side
    truncation at any tail boundary valid (hb-short).
  - Parsing never raises on unknown types or short tails: unknown types come
    back as Unknown(type, raw); absent tail fields come back as None (or the
    documented legacy default, e.g. ShowFrame.val -> 255).

Types 25 (NB_DIRECT_FRAME) and 26 (NB_FORCE_LIFECYCLE) are allocated in the
firmware header. Their layouts are extracted from the firmware's native
layout test and pinned here with every other protocol-v1 struct.

No magic bytes and no CRC at this layer -- ESP-NOW frames carry these
structs bare. Framing/CRC for the serial bridge lives in cambium.wire.framing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

NB_PROTO_VER = 1


class NbType(IntEnum):
    # 1..17: net_bench era (bench masters still send/understand these).
    HEARTBEAT = 1
    SHOWFRAME = 2
    ENTER_MAINT = 3
    RESUME = 4
    SET_RATE = 5
    IDENTIFY = 6
    SCANAP = 7
    SET_MAINTAIN = 8
    SET_CAPACITY = 9
    SET_CHARGE_MA = 10
    SLEEP_FOR = 11
    DRAWDOWN = 12
    TARGET_SLEEP_FOR = 13
    TARGET_CAPACITY = 14
    TARGET_CHARGE_MA = 15
    TARGET_ENTER_MAINT = 16
    TARGET_SOLENOID = 17
    # 18..24: fixture era.
    CHOREO_STATE = 18
    PROGRAM_SET = 19
    TIME_QUALITY = 20  # RESERVED in firmware: defined, not sent
    PROFILE = 21
    NEIGHBOR_REPORT = 22  # RESERVED
    EVENT = 23  # RESERVED
    NEIGHBOR_SET = 24
    # 25..26: Cambium direct streaming and lifecycle override.
    DIRECT_FRAME = 25
    FORCE_LIFECYCLE = 26


# ---------------------------------------------------------------------------
# Layout tables: one entry per C struct, one row per field, in declaration
# order. This is the single source of truth for offsets on the Python side;
# tests/test_packet_parity.py checks it against the firmware's golden pins.
# Fixed-size arrays are 'Ns' blobs; nested struct arrays are flattened to a
# single blob because only their sizeof/offset matter for parity.
# ---------------------------------------------------------------------------

STRUCT_FIELDS: dict[str, list[tuple[str, str]]] = {
    "NbHeader": [
        ("ver", "B"),
        ("type", "B"),
        ("src_id", "3s"),
        ("seq", "I"),
        ("uptime_ms", "I"),
    ],
    "NbCmd": [("h", "13s"), ("arg", "B")],
    "NbSetU16": [("h", "13s"), ("value", "H")],
    "NbTargetU16": [("h", "13s"), ("target_id", "3s"), ("value", "H")],
    "NbTargetCmd": [("h", "13s"), ("target_id", "3s"), ("arg", "B")],
    "NbShowFrame": [
        ("h", "13s"),
        ("phase", "H"),
        ("hue", "B"),
        ("flags", "B"),
        # append-only tail 1 (fixture era); old net_bench masters send 17 B
        ("val", "B"),
        ("bright", "B"),
        ("effect", "B"),
        ("beat_phase", "B"),
        ("energy", "B"),
    ],
    "NbIdentify": [
        ("h", "13s"),
        ("target_id", "3s"),
        ("secs", "B"),
        # append-only tail 1 (fixture era); legacy peers send/parse 17 B
        ("color", "B"),
        ("blink", "B"),
    ],
    "NbScanAp": [
        ("h", "13s"),
        ("scan_id", "B"),
        ("idx", "B"),
        ("count", "B"),
        ("bssid", "6s"),
        ("ap_rssi", "b"),
        ("channel", "B"),
        ("enc", "B"),
        ("ssid", "20s"),
    ],
    "NbHeartbeat": [
        ("h", "13s"),
        # base block -- present in every valid heartbeat
        ("batt_mv", "h"),
        ("batt_ma", "h"),
        ("soc_pct", "B"),
        ("reset_reason", "B"),
        ("ca_state", "B"),
        ("mode", "B"),
        ("dl_pdr_x1000", "H"),
        ("dl_rssi", "b"),
        # tail 1 -- hb-short (0.2 Hz cadence) truncates after supply_good
        ("supply_mv", "h"),
        ("supply_ma", "h"),
        ("supply_good", "B"),
        # tail 2: env sensors (bench-only; fixture sends absent sentinels)
        ("lux_x10", "I"),
        ("light_ch0", "H"),
        ("light_ch1", "H"),
        ("ptemp_cx10", "h"),
        ("prh_pct", "B"),
        ("btemp_cx10", "h"),
        # tail 3: bench INA219 meters (instrument retired)
        ("ina_pv_mv", "h"),
        ("ina_pa_ma", "h"),
        ("ina_bv_mv", "h"),
        ("ina_ba_ma", "h"),
        # tail 4: runtime config
        ("cfg_cap_mah", "H"),
        ("cfg_charge_ma", "H"),
        # tail 5: bench drawdown
        ("drawdown_mah_x10", "H"),
        ("drawdown_budget_mah", "H"),
        ("drawdown_active", "B"),
        # tail 6: identity
        ("fw_rev", "24s"),
        # tail 7: maintenance health
        ("maint_status", "B"),
        # tail 8: lifecycle summary
        ("field_phase", "B"),
        ("field_reason", "B"),
        ("field_cycle", "H"),
        ("field_elapsed_s", "H"),
        ("field_charge_mah", "H"),
        ("field_discharge_mah", "H"),
        ("field_min_mv", "H"),
        ("field_max_mv", "H"),
        # tail 9: BQ25628E charger truth
        ("bq_vindpm_mv", "H"),
        ("bq_ichg_ma", "H"),
        ("bq_vreg_mv", "H"),
        ("bq_reg16", "B"),
        ("bq_reg18", "B"),
        ("bq_stat0", "B"),
        ("bq_stat1", "B"),
        ("bq_fault0", "B"),
        ("bq_flag0", "B"),
        ("bq_flag1", "B"),
        ("bq_fault_flag0", "B"),
        ("bq_part", "B"),
        # tail 10: energy summary
        ("field_charge_wh_x10", "H"),
        ("field_discharge_wh_x10", "H"),
        ("field_peak_panel_w_x100", "H"),
        ("field_peak_charge_w_x100", "H"),
        ("field_peak_draw_w_x100", "H"),
        ("field_low_s", "B"),
        ("field_charge_min", "B"),
        ("field_wait_min", "B"),
        ("field_draw_min", "B"),
        ("field_protect_min", "B"),
        # tail 11: bench MPPT perturb
        ("mppt_status", "B"),
        ("mppt_reason", "B"),
        ("mppt_runs", "B"),
        ("mppt_active_v10", "B"),
        ("mppt_best_v10", "B"),
        ("mppt_last_v10", "B"),
        ("mppt_p46_w_x100", "H"),
        ("mppt_p48_w_x100", "H"),
        ("mppt_p50_w_x100", "H"),
        # tail 12: low-voltage latches
        ("field_load_dimmed", "B"),
        ("field_protect_latched", "B"),
        # tail 13: fixture-era slow diagnostics (hb-full only)
        ("profile", "B"),
        ("life_state", "B"),
        ("power_tier", "B"),
        ("active_program", "B"),
        ("night_min", "H"),
    ],
    "NbChoreoState": [
        ("h", "13s"),
        ("program_id", "B"),
        ("generation", "H"),
        ("state", "B"),
        ("intensity", "B"),
        ("phase_ms", "H"),
        ("flags", "B"),
        ("reserved", "B"),
    ],
    "NbProgramSet": [
        ("h", "13s"),
        ("target_id", "3s"),
        ("program_id", "B"),
        ("lease_s", "H"),
        ("seed", "I"),
        ("flags", "B"),
        ("params", "8s"),
    ],
    "NbTimeQuality": [
        ("h", "13s"),
        ("utc_s", "I"),
        ("sub_ms", "H"),
        ("source", "B"),
        ("hops", "B"),
        ("age_s", "H"),
        ("uncert_ms", "H"),
        ("boot_id", "H"),
        ("flags", "B"),
        ("reserved", "B"),
    ],
    "NbProfile": [
        ("h", "13s"),
        ("target_id", "3s"),
        ("profile", "B"),
        ("flags", "B"),
    ],
    "NbNeighborEntry": [("id", "3s"), ("med_dbm", "b"), ("n", "B"), ("flags", "B")],
    "NbNeighborReport": [
        ("h", "13s"),
        ("count", "B"),
        ("n_expected", "H"),
        ("entries", "96s"),  # NbNeighborEntry[16], 6 B each
    ],
    "NbEvent": [
        ("h", "13s"),
        ("event_id", "I"),
        ("fire_in_ms", "I"),
        ("kind", "B"),
        ("hop_limit", "B"),
        ("params", "16s"),
    ],
    "NbNeighborSet": [
        ("h", "13s"),
        ("target_id", "3s"),
        ("flags", "B"),
        ("count", "B"),
        ("neighbor_ids", "24s"),  # uint8[8][3]
    ],
    # ---- Cambium types 25/26 -----------------------------------------------
    "NbDirectEntry": [("id", "3s"), ("r", "B"), ("g", "B"), ("b", "B"), ("w", "B")],
    "NbDirectFrame": [
        ("h", "13s"),
        ("flags", "B"),  # bit0=10s micro-lease grant, bit1=hard-cut/skip-slew
        ("count", "B"),
        ("entries", "126s"),  # NbDirectEntry[18], 7 B each; wire len = 15 + 7n
    ],
    "NbForceLifecycle": [
        ("h", "13s"),
        ("target_id", "3s"),
        ("mode", "B"),  # 0=force day, 1=force night, 2=auto
        ("flags", "B"),  # reserved 0
    ],
}


def _field_size(fmt: str) -> int:
    # '<' prefix so struct never inserts alignment padding -- the wire structs
    # are __attribute__((packed)) little-endian.
    return struct.calcsize("<" + fmt)


def struct_offsetof(name: str, field_name: str) -> int:
    """Python-side offsetof(), for parity checks against the firmware pins."""
    off = 0
    for fname, fmt in STRUCT_FIELDS[name]:
        if fname == field_name:
            return off
        off += _field_size(fmt)
    raise KeyError(
        f"{name} has no field {field_name!r}; add it to STRUCT_FIELDS in "
        f"cambium/wire/packets.py (mirroring packet.h declaration order)"
    )


def struct_sizeof(name: str) -> int:
    """Python-side sizeof(), for parity checks against the firmware pins."""
    return sum(_field_size(fmt) for _, fmt in STRUCT_FIELDS[name])


HEADER_LEN = 13
_HEADER_FMT = "<BB3sII"

# Send-side truncation boundaries (mirror packet.h macros).
NB_HB_SHORT_LEN = struct_offsetof("NbHeartbeat", "supply_good") + 1  # 29
NB_HB_FULL_LEN = struct_sizeof("NbHeartbeat")  # 148

NB_DIRECT_FRAME_MAX_ENTRIES = 18
_DIRECT_ENTRY_LEN = 7


# ---------------------------------------------------------------------------
# Short-id helpers. The fleet identifies nodes by the last 3 MAC bytes.
# ---------------------------------------------------------------------------

def short_id_to_str(sid: bytes) -> str:
    """3-byte short id -> 'F2BDB4' (uppercase hex, no separators)."""
    if len(sid) != 3:
        raise ValueError(
            f"short id must be exactly 3 bytes, got {len(sid)}; "
            f"pass the last 3 MAC bytes, e.g. bytes.fromhex('F2BDB4')"
        )
    return sid.hex().upper()


def short_id_from_str(s: str) -> bytes:
    """'F2BDB4' / 'f2:bd:b4' / 'F2-BD-B4' -> 3 bytes."""
    hexstr = s.replace(":", "").replace("-", "")
    try:
        sid = bytes.fromhex(hexstr)
    except ValueError:
        raise ValueError(
            f"short id {s!r} is not hex; use 6 hex digits like 'F2BDB4' or 'F2:BD:B4'"
        ) from None
    if len(sid) != 3:
        raise ValueError(
            f"short id {s!r} decodes to {len(sid)} bytes, need exactly 3; "
            f"use the last 3 MAC bytes like 'F2BDB4'"
        )
    return sid


BROADCAST_ID = b"\x00\x00\x00"  # target_id 00:00:00 = all nodes


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

@dataclass
class NbHeader:
    ver: int = NB_PROTO_VER
    type: int = 0
    src_id: bytes = b"\x00\x00\x00"
    seq: int = 0
    uptime_ms: int = 0

    def pack(self) -> bytes:
        if len(self.src_id) != 3:
            raise ValueError(
                f"src_id must be 3 bytes, got {len(self.src_id)}; "
                f"use short_id_from_str('F2BDB4') to build one"
            )
        return struct.pack(
            _HEADER_FMT, self.ver, self.type, self.src_id,
            self.seq & 0xFFFFFFFF, self.uptime_ms & 0xFFFFFFFF,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "NbHeader":
        if len(raw) < HEADER_LEN:
            raise ValueError(
                f"header needs {HEADER_LEN} bytes, got {len(raw)}; "
                f"length-gate with parse_packet() instead of unpacking directly"
            )
        ver, ptype, src_id, seq, uptime_ms = struct.unpack_from(_HEADER_FMT, raw)
        return cls(ver=ver, type=ptype, src_id=src_id, seq=seq, uptime_ms=uptime_ms)


def _header_bytes(h: NbHeader, ptype: NbType) -> bytes:
    # Builders stamp the correct type themselves so a stale h.type can never
    # emit a mislabeled packet.
    if len(h.src_id) != 3:
        raise ValueError(
            f"src_id must be 3 bytes, got {len(h.src_id)}; "
            f"use short_id_from_str('F2BDB4') to build one"
        )
    return struct.pack(
        _HEADER_FMT, h.ver, int(ptype), h.src_id,
        h.seq & 0xFFFFFFFF, h.uptime_ms & 0xFFFFFFFF,
    )


def _check_target(target: bytes) -> bytes:
    if len(target) != 3:
        raise ValueError(
            f"target_id must be 3 bytes (00:00:00 = all), got {len(target)}; "
            f"use short_id_from_str('F2BDB4') or packets.BROADCAST_ID"
        )
    return target


def _check_u8(name: str, value: int) -> int:
    if not 0 <= value <= 255:
        raise ValueError(f"{name}={value} does not fit uint8; clamp to 0..255 before building")
    return value


# ---------------------------------------------------------------------------
# Builders -- each returns complete packet bytes ready for the radio.
# ---------------------------------------------------------------------------

def show_frame(
    h: NbHeader,
    phase: int,
    hue: int,
    flags: int = 0,
    *,
    val: int = 255,
    bright: int = 255,
    effect: int = 0,
    beat_phase: int = 0,
    energy: int = 0,
    legacy: bool = False,
) -> bytes:
    """NB_SHOWFRAME. legacy=True emits the 17 B net_bench form (no tail).

    Receivers treat val==0 as 255 for parity with old masters, so val=0 and
    val=255 render identically on era-18+ fixtures.
    """
    out = _header_bytes(h, NbType.SHOWFRAME) + struct.pack(
        "<HBB", phase & 0xFFFF, _check_u8("hue", hue), _check_u8("flags", flags)
    )
    if legacy:
        return out
    return out + struct.pack(
        "<BBBBB",
        _check_u8("val", val),
        _check_u8("bright", bright),
        _check_u8("effect", effect),
        _check_u8("beat_phase", beat_phase),
        _check_u8("energy", energy),
    )


def identify(
    h: NbHeader,
    target: bytes,
    secs: int,
    color: int = 0,
    blink: int = 0,
) -> bytes:
    """NB_IDENTIFY, 19 B color-tail form (era 18+ peers use the tail; legacy
    peers ignore it and blink '..-' as before).

    secs=0 cancels an active identify. color: 0=none(blink pattern) 1=R 2=G
    3=B 4=Y 5=W. blink: 0=solid 1=blinking.
    """
    if not 0 <= color <= 5:
        raise ValueError(f"color={color} invalid; use 0=none 1=R 2=G 3=B 4=Y 5=W")
    return _header_bytes(h, NbType.IDENTIFY) + struct.pack(
        "<3sBBB", _check_target(target), _check_u8("secs", secs),
        color, _check_u8("blink", blink),
    )


def program_set(
    h: NbHeader,
    target: bytes,
    program_id: int,
    lease_s: int,
    seed: int = 0,
    flags: int = 0,
    params: bytes = b"",
) -> bytes:
    """NB_PROGRAM_SET (bridge lease). program_id=0 + lease_s=0 releases the
    lease (return to autonomous). params is zero-padded to 8 bytes.
    """
    if len(params) > 8:
        raise ValueError(
            f"params is {len(params)} bytes, max 8; trim it or split across leases"
        )
    return _header_bytes(h, NbType.PROGRAM_SET) + struct.pack(
        "<3sBHIB8s",
        _check_target(target),
        _check_u8("program_id", program_id),
        lease_s & 0xFFFF,
        seed & 0xFFFFFFFF,
        _check_u8("flags", flags),
        params.ljust(8, b"\x00"),
    )


def set_rate(h: NbHeader, hz: int) -> bytes:
    """NB_SET_RATE (NbCmd shell): heartbeat/frame rate in Hz."""
    return _header_bytes(h, NbType.SET_RATE) + struct.pack("<B", _check_u8("hz", hz))


def enter_maint(h: NbHeader) -> bytes:
    """NB_ENTER_MAINT (NbCmd shell, arg unused -> 0): all peers enter
    shared-WiFi maintenance per ADR-0010."""
    return _header_bytes(h, NbType.ENTER_MAINT) + b"\x00"


def resume(h: NbHeader) -> bytes:
    """NB_RESUME (NbCmd shell, arg unused -> 0): leave maintenance."""
    return _header_bytes(h, NbType.RESUME) + b"\x00"


def direct_frame(
    h: NbHeader,
    entries: list[tuple[bytes, int, int, int, int]],
    flags: int = 0,
) -> bytes:
    """NB_DIRECT_FRAME (type 25): per-fixture RGBW from the browser
    sim. entries = [(short_id 3 bytes, r, g, b, w), ...], max 18 (ESP-NOW
    payload budget). flags bit0=10s micro-lease grant, bit1=hard-cut/skip-slew.
    Wire length is always 15 + 7*len(entries).
    """
    if len(entries) > NB_DIRECT_FRAME_MAX_ENTRIES:
        raise ValueError(
            f"direct_frame has {len(entries)} entries, max {NB_DIRECT_FRAME_MAX_ENTRIES}; "
            f"split the fixture list across multiple frames"
        )
    out = bytearray(_header_bytes(h, NbType.DIRECT_FRAME))
    out += struct.pack("<BB", _check_u8("flags", flags), len(entries))
    for sid, r, g, b, w in entries:
        if len(sid) != 3:
            raise ValueError(
                f"entry id must be 3 bytes, got {len(sid)}; "
                f"use short_id_from_str('F2BDB4')"
            )
        out += struct.pack(
            "<3sBBBB", sid, _check_u8("r", r), _check_u8("g", g),
            _check_u8("b", b), _check_u8("w", w),
        )
    assert len(out) == 15 + _DIRECT_ENTRY_LEN * len(entries)
    return bytes(out)


def force_lifecycle(h: NbHeader, target: bytes, mode: int, flags: int = 0) -> bytes:
    """NB_FORCE_LIFECYCLE (type 26). mode: 0=force day, 1=force
    night, 2=auto. target 00:00:00 = all. flags reserved (send 0)."""
    if mode not in (0, 1, 2):
        raise ValueError(f"mode={mode} invalid; use 0=force day, 1=force night, 2=auto")
    return _header_bytes(h, NbType.FORCE_LIFECYCLE) + struct.pack(
        "<3sBB", _check_target(target), mode, _check_u8("flags", flags)
    )


# ---------------------------------------------------------------------------
# Parse results
# ---------------------------------------------------------------------------

@dataclass
class Unknown:
    """Anything we don't (yet) decode: unknown/reserved types, foreign
    protocol versions, or bodies shorter than their legacy minimum. Kept, not
    raised, so one weird packet never kills an uplink loop."""
    type: int
    raw: bytes


@dataclass
class Heartbeat:
    h: NbHeader
    # base block (always present)
    batt_mv: int = 0
    batt_ma: int = 0
    soc_pct: int = 0
    reset_reason: int = 0
    ca_state: int = 0
    mode: int = 0
    dl_pdr_x1000: int = 0
    dl_rssi: int = 0
    # append-only tails: None = absent from this packet's length (hb-short
    # stops after supply_good; bench-era packets stop before tail 13).
    supply_mv: int | None = None
    supply_ma: int | None = None
    supply_good: int | None = None
    lux_x10: int | None = None
    light_ch0: int | None = None
    light_ch1: int | None = None
    ptemp_cx10: int | None = None
    prh_pct: int | None = None
    btemp_cx10: int | None = None
    ina_pv_mv: int | None = None
    ina_pa_ma: int | None = None
    ina_bv_mv: int | None = None
    ina_ba_ma: int | None = None
    cfg_cap_mah: int | None = None
    cfg_charge_ma: int | None = None
    drawdown_mah_x10: int | None = None
    drawdown_budget_mah: int | None = None
    drawdown_active: int | None = None
    fw_rev: str | None = None
    maint_status: int | None = None
    field_phase: int | None = None
    field_reason: int | None = None
    field_cycle: int | None = None
    field_elapsed_s: int | None = None
    field_charge_mah: int | None = None
    field_discharge_mah: int | None = None
    field_min_mv: int | None = None
    field_max_mv: int | None = None
    bq_vindpm_mv: int | None = None
    bq_ichg_ma: int | None = None
    bq_vreg_mv: int | None = None
    bq_reg16: int | None = None
    bq_reg18: int | None = None
    bq_stat0: int | None = None
    bq_stat1: int | None = None
    bq_fault0: int | None = None
    bq_flag0: int | None = None
    bq_flag1: int | None = None
    bq_fault_flag0: int | None = None
    bq_part: int | None = None
    field_charge_wh_x10: int | None = None
    field_discharge_wh_x10: int | None = None
    field_peak_panel_w_x100: int | None = None
    field_peak_charge_w_x100: int | None = None
    field_peak_draw_w_x100: int | None = None
    field_low_s: int | None = None
    field_charge_min: int | None = None
    field_wait_min: int | None = None
    field_draw_min: int | None = None
    field_protect_min: int | None = None
    mppt_status: int | None = None
    mppt_reason: int | None = None
    mppt_runs: int | None = None
    mppt_active_v10: int | None = None
    mppt_best_v10: int | None = None
    mppt_last_v10: int | None = None
    mppt_p46_w_x100: int | None = None
    mppt_p48_w_x100: int | None = None
    mppt_p50_w_x100: int | None = None
    field_load_dimmed: int | None = None
    field_protect_latched: int | None = None
    profile: int | None = None
    life_state: int | None = None
    power_tier: int | None = None
    active_program: int | None = None
    night_min: int | None = None


@dataclass
class ShowFrame:
    h: NbHeader
    phase: int
    hue: int
    flags: int
    # Tail defaults mirror receiver behavior for a legacy 17 B frame: val
    # absent -> 255 (and receivers also render a *present* val of 0 as 255).
    val: int = 255
    bright: int = 255
    effect: int = 0
    beat_phase: int = 0
    energy: int = 0


@dataclass
class Identify:
    h: NbHeader
    target_id: bytes
    secs: int  # 0 = cancel
    color: int = 0  # absent tail (legacy 17 B) -> 0 = blink-pattern identify
    blink: int = 0


@dataclass
class ChoreoState:
    h: NbHeader
    program_id: int
    generation: int
    state: int
    intensity: int
    phase_ms: int
    flags: int
    reserved: int


@dataclass
class ProgramSet:
    h: NbHeader
    target_id: bytes
    program_id: int
    lease_s: int
    seed: int
    flags: int
    params: bytes


@dataclass
class DirectEntry:
    id: bytes
    r: int
    g: int
    b: int
    w: int


@dataclass
class DirectFrame:
    h: NbHeader
    flags: int
    count: int  # declared count byte (may exceed len(entries) on a short body)
    entries: list[DirectEntry]


@dataclass
class ForceLifecycle:
    h: NbHeader
    target_id: bytes
    mode: int
    flags: int


# Heartbeat parse table: (name, offset, fmt) for everything after the header,
# derived from the same layout table the parity tests pin.
_HB_PARSE: list[tuple[str, int, str]] = []
_off = 0
for _name, _fmt in STRUCT_FIELDS["NbHeartbeat"]:
    if _name != "h":
        _HB_PARSE.append((_name, _off, _fmt))
    _off += _field_size(_fmt)
del _off, _name, _fmt

_HB_BASE_END = struct_offsetof("NbHeartbeat", "supply_mv")  # 24


def _cstr(raw: bytes) -> str:
    # char[N] NUL-terminated; tolerate junk after the NUL and non-ASCII bytes
    # (a corrupted fw_rev must not kill telemetry decode).
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _parse_heartbeat(h: NbHeader, raw: bytes) -> Heartbeat:
    hb = Heartbeat(h=h)
    for name, off, fmt in _HB_PARSE:
        size = _field_size(fmt)
        if len(raw) < off + size:
            break  # append-only: everything past here is absent (stays None)
        (value,) = struct.unpack_from("<" + fmt, raw, off)
        if name == "fw_rev":
            value = _cstr(value)
        setattr(hb, name, value)
    return hb


def parse_packet(raw: bytes):
    """Decode one ESP-NOW payload into a typed result.

    Never raises: unknown types, reserved types, foreign protocol versions and
    bodies shorter than their legacy minimum all come back as Unknown(type,
    raw) -- the same "length-gate and ignore" posture the firmware takes, so a
    single malformed packet can't take down the uplink.
    """
    if len(raw) < HEADER_LEN:
        return Unknown(type=raw[1] if len(raw) >= 2 else 0, raw=raw)
    h = NbHeader.unpack(raw)
    if h.ver != NB_PROTO_VER:
        # A ver bump is a flag day; we cannot trust any offsets past byte 0.
        return Unknown(type=h.type, raw=raw)
    n = len(raw)

    if h.type == NbType.HEARTBEAT and n >= _HB_BASE_END:
        return _parse_heartbeat(h, raw)

    if h.type == NbType.SHOWFRAME and n >= 17:
        phase, hue, flags = struct.unpack_from("<HBB", raw, 13)
        sf = ShowFrame(h=h, phase=phase, hue=hue, flags=flags)
        # append-only tail, gated per field (a truncated tail is valid wire)
        if n >= 18:
            sf.val = raw[17]
        if n >= 19:
            sf.bright = raw[18]
        if n >= 20:
            sf.effect = raw[19]
        if n >= 21:
            sf.beat_phase = raw[20]
        if n >= 22:
            sf.energy = raw[21]
        return sf

    if h.type == NbType.IDENTIFY and n >= 17:
        target_id, secs = struct.unpack_from("<3sB", raw, 13)
        return Identify(
            h=h, target_id=target_id, secs=secs,
            color=raw[17] if n >= 18 else 0,
            blink=raw[18] if n >= 19 else 0,
        )

    if h.type == NbType.CHOREO_STATE and n >= 22:
        program_id, generation, state, intensity, phase_ms, flags, reserved = (
            struct.unpack_from("<BHBBHBB", raw, 13)
        )
        return ChoreoState(
            h=h, program_id=program_id, generation=generation, state=state,
            intensity=intensity, phase_ms=phase_ms, flags=flags, reserved=reserved,
        )

    if h.type == NbType.PROGRAM_SET and n >= 32:
        target_id, program_id, lease_s, seed, flags, params = struct.unpack_from(
            "<3sBHIB8s", raw, 13
        )
        return ProgramSet(
            h=h, target_id=target_id, program_id=program_id,
            lease_s=lease_s, seed=seed, flags=flags, params=params,
        )

    if h.type == NbType.DIRECT_FRAME and n >= 15:
        flags, count = raw[13], raw[14]
        # Liberal on receive: decode as many whole entries as the length
        # actually carries, even if the count byte disagrees.
        avail = (n - 15) // _DIRECT_ENTRY_LEN
        entries = []
        for i in range(min(count, avail)):
            sid, r, g, b, w = struct.unpack_from(
                "<3sBBBB", raw, 15 + i * _DIRECT_ENTRY_LEN
            )
            entries.append(DirectEntry(id=sid, r=r, g=g, b=b, w=w))
        return DirectFrame(h=h, flags=flags, count=count, entries=entries)

    if h.type == NbType.FORCE_LIFECYCLE and n >= 18:
        target_id, mode, flags = struct.unpack_from("<3sBB", raw, 13)
        return ForceLifecycle(h=h, target_id=target_id, mode=mode, flags=flags)

    return Unknown(type=h.type, raw=raw)


def target_matches(target: bytes, my_id: bytes) -> bool:
    """Mirror of nbTargetMatches(): 00:00:00 targets everyone."""
    return target == BROADCAST_ID or target == my_id
