"""Hold cambium's Python layouts to the firmware's golden pins.

packet_pins.json is machine-extracted from the firmware's
test_packet_layout.cpp by tools/extract_packet_goldens.py. Every pin must
check out against our STRUCT_FIELDS tables -- if the firmware moves a byte,
this is the tripwire on the Python side.

Types 25/26 are part of the same extracted firmware pins as every other
packet. The focused tests below keep their complete field layouts obvious.
"""

import json
import re
from pathlib import Path

import pytest

from cambium.wire import packets

GOLDEN = Path(__file__).parent / "golden" / "packet_pins.json"
PINS: dict[str, int] = json.loads(GOLDEN.read_text())["pins"]

_SIZEOF = re.compile(r"^sizeof\((\w+)\)$")
_OFFSETOF = re.compile(r"^offsetof\((\w+), (\w+)\)$")


def test_goldens_were_extracted():
    # Guard against a silently-empty extraction: these anchor pins must exist.
    assert "sizeof(NbHeartbeat)" in PINS
    assert "offsetof(NbHeartbeat, profile)" in PINS
    assert len(PINS) >= 30


@pytest.mark.parametrize("key,expected", sorted(PINS.items()))
def test_firmware_pin(key, expected):
    if m := _SIZEOF.match(key):
        assert packets.struct_sizeof(m.group(1)) == expected, key
    elif m := _OFFSETOF.match(key):
        assert packets.struct_offsetof(m.group(1), m.group(2)) == expected, key
    elif key == "NB_HB_SHORT_LEN":
        assert packets.NB_HB_SHORT_LEN == expected
    else:
        pytest.fail(
            f"unrecognized pin key {key!r}; the firmware layout test gained a "
            f"shape this test doesn't know -- teach test_packet_parity.py about it"
        )


def test_hb_full_len():
    assert packets.NB_HB_FULL_LEN == 148


# ---- focused pins for Cambium types 25/26 ----------------------------------

def test_direct_frame_pins():
    assert packets.struct_sizeof("NbDirectEntry") == 7
    assert packets.struct_sizeof("NbDirectFrame") == 141  # 15 + 18*7
    assert packets.struct_offsetof("NbDirectFrame", "flags") == 13
    assert packets.struct_offsetof("NbDirectFrame", "count") == 14
    assert packets.struct_offsetof("NbDirectFrame", "entries") == 15


def test_force_lifecycle_pins():
    assert packets.struct_sizeof("NbForceLifecycle") == 18
    assert packets.struct_offsetof("NbForceLifecycle", "target_id") == 13
    assert packets.struct_offsetof("NbForceLifecycle", "mode") == 16
    assert packets.struct_offsetof("NbForceLifecycle", "flags") == 17


def test_built_packet_lengths_match_pins():
    # The builders must emit exactly the pinned wire sizes.
    h = packets.NbHeader(src_id=b"\xaa\xbb\xcc")
    assert len(packets.show_frame(h, 0, 0, legacy=True)) == 17
    assert len(packets.show_frame(h, 0, 0)) == PINS["sizeof(NbShowFrame)"]
    assert len(packets.identify(h, packets.BROADCAST_ID, 5)) == PINS["sizeof(NbIdentify)"]
    assert len(packets.program_set(h, packets.BROADCAST_ID, 1, 60)) == PINS["sizeof(NbProgramSet)"]
    assert len(packets.set_rate(h, 5)) == PINS["sizeof(NbCmd)"]
    assert len(packets.enter_maint(h)) == PINS["sizeof(NbCmd)"]
    assert len(packets.resume(h)) == PINS["sizeof(NbCmd)"]
    assert len(packets.force_lifecycle(h, packets.BROADCAST_ID, 2)) == 18
    for n in (0, 1, 10, 18):
        entries = [(b"\x01\x02\x03", 1, 2, 3, 4)] * n
        assert len(packets.direct_frame(h, entries)) == 15 + 7 * n
    # full direct frame == its sizeof pin
    full = [(b"\x01\x02\x03", 0, 0, 0, 0)] * 18
    assert len(packets.direct_frame(h, full)) == packets.struct_sizeof("NbDirectFrame")
