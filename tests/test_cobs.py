"""COBS + CRC against the shared golden vectors (tests/golden/cobs_vectors.json).

The same vectors will be compiled into the bridge-board firmware's tests, so
both ends of the serial link are held to identical bytes.
"""

import json
from pathlib import Path

import pytest

from cambium.wire.cobs import cobs_decode, cobs_encode, crc16_ccitt

GOLDEN = Path(__file__).parent / "golden" / "cobs_vectors.json"
VECTORS = json.loads(GOLDEN.read_text())["vectors"]


def test_vectors_present():
    assert len(VECTORS) >= 8
    names = {v["name"] for v in VECTORS}
    # the shapes the task and the firmware side care about most
    for required in ("empty", "single_zero", "block_254_boundary", "sample_frame"):
        assert required in names


@pytest.mark.parametrize("vec", VECTORS, ids=lambda v: v["name"])
def test_golden_encode(vec):
    assert cobs_encode(bytes.fromhex(vec["decoded"])).hex() == vec["encoded"]


@pytest.mark.parametrize("vec", VECTORS, ids=lambda v: v["name"])
def test_golden_decode(vec):
    assert cobs_decode(bytes.fromhex(vec["encoded"])).hex() == vec["decoded"]


@pytest.mark.parametrize("vec", VECTORS, ids=lambda v: v["name"])
def test_golden_crc(vec):
    assert crc16_ccitt(bytes.fromhex(vec["decoded"])) == vec["crc16_ccitt"]


@pytest.mark.parametrize("vec", VECTORS, ids=lambda v: v["name"])
def test_encoded_has_no_zeros(vec):
    assert 0 not in bytes.fromhex(vec["encoded"])


def test_roundtrip_assorted():
    cases = [
        bytes(300),  # all zeros
        bytes(range(256)) * 4,  # every byte value, crosses 254-block boundaries
        b"\x01" * 254 * 3,  # multiple maximal blocks
        b"\x01" * 254 + b"\x00",  # maximal block then zero
        b"\x00" * 5 + b"\xff" * 260 + b"\x00" * 5,
    ]
    for data in cases:
        encoded = cobs_encode(data)
        assert 0 not in encoded
        assert cobs_decode(encoded) == data


def test_decode_rejects_garbage():
    with pytest.raises(ValueError):
        cobs_decode(b"")  # nothing between delimiters is not a frame
    with pytest.raises(ValueError):
        cobs_decode(b"\x05\x11\x22")  # code 5 promises 4 bytes, only 2 present
    with pytest.raises(ValueError):
        cobs_decode(b"\x03\x11\x00")  # embedded zero (unstripped delimiter)
    with pytest.raises(ValueError):
        cobs_decode(b"\x00\x11")  # leading zero code byte


def test_crc16_check_value():
    # The standard CRC-16/CCITT-FALSE check value pins the exact algorithm
    # (poly/init/reflection) independently of our own implementation.
    assert crc16_ccitt(b"123456789") == 0x29B1
    assert crc16_ccitt(b"") == 0xFFFF  # init value untouched
