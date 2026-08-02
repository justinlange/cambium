"""Serial framing contract: roundtrip, resync after garbage, CRC counting,
and STATUS payload decode (hand-packed bytes, not derived from the encoder)."""

import struct

import pytest

from cambium.wire.cobs import cobs_encode, crc16_ccitt
from cambium.wire import framing
from cambium.wire.framing import (
    FTYPE_CTRL,
    FTYPE_LOG,
    FTYPE_RADIO_RX,
    FTYPE_RADIO_TX,
    FTYPE_STATUS,
    FrameDecoder,
    encode_frame,
    parse_status,
)


def test_roundtrip_all_ftypes():
    payloads = [
        (FTYPE_RADIO_TX, b"\x01\x02\xaa\xbb\xcc" + bytes(8)),  # zeros inside
        (FTYPE_RADIO_RX, bytes(6) + b"\xb5" + b"\x01\x01" + bytes(11)),
        (FTYPE_CTRL, bytes([framing.CTRL_SET_CHANNEL, 6])),
        (FTYPE_STATUS, bytes(46)),
        (FTYPE_LOG, b"boot ok"),
    ]
    dec = FrameDecoder()
    wire = b"".join(encode_frame(ft, p) for ft, p in payloads)
    assert dec.feed(wire) == payloads
    assert dec.crc_errors == 0


def test_frame_structure_is_the_documented_contract():
    # Independently reconstruct the frame from the documented layout so the
    # encoder can't drift from its own docstring.
    payload = b"\x10\x00\x20"
    body = bytes([FTYPE_LOG]) + payload
    expected = cobs_encode(body + struct.pack("<H", crc16_ccitt(body))) + b"\x00"
    assert encode_frame(FTYPE_LOG, payload) == expected


def test_byte_at_a_time_feed():
    dec = FrameDecoder()
    wire = encode_frame(FTYPE_CTRL, bytes([framing.CTRL_STATUS_REQ]))
    got = []
    for i in range(len(wire)):
        got += dec.feed(wire[i : i + 1])
    assert got == [(FTYPE_CTRL, bytes([framing.CTRL_STATUS_REQ]))]
    assert dec.crc_errors == 0


def test_idle_zeros_are_not_errors():
    dec = FrameDecoder()
    assert dec.feed(b"\x00\x00\x00") == []
    assert dec.crc_errors == 0


def test_resync_after_garbage():
    dec = FrameDecoder()
    good = encode_frame(FTYPE_LOG, b"hello")
    # garbage chunk (truncated COBS), then a valid frame in the same feed
    assert dec.feed(b"\x37\x01\x02\x00" + good) == [(FTYPE_LOG, b"hello")]
    assert dec.crc_errors == 1


def test_crc_mismatch_dropped_and_counted():
    # Hand-build a frame whose CRC is off by one bit -- COBS is intact, so
    # only the CRC check can catch it.
    body = bytes([FTYPE_LOG]) + b"corrupt me"
    bad = cobs_encode(body + struct.pack("<H", crc16_ccitt(body) ^ 0x0001)) + b"\x00"
    dec = FrameDecoder()
    assert dec.feed(bad) == []
    assert dec.crc_errors == 1
    # decoder still works afterwards
    assert dec.feed(encode_frame(FTYPE_LOG, b"ok")) == [(FTYPE_LOG, b"ok")]
    assert dec.crc_errors == 1


def test_too_short_chunk_counted():
    dec = FrameDecoder()
    # decodes fine via COBS (to 2 bytes) but can't hold ftype + crc16
    assert dec.feed(b"\x03\x41\x42\x00") == []
    assert dec.crc_errors == 1


def test_runaway_buffer_resyncs():
    dec = FrameDecoder()
    assert dec.feed(b"\x55" * 5000) == []  # no delimiter anywhere
    assert dec.crc_errors == 1
    assert dec.feed(encode_frame(FTYPE_LOG, b"after")) == [(FTYPE_LOG, b"after")]


def test_parse_status_hand_packed():
    # Written by hand from the documented layout:
    # proto:u8 mac[6] channel:u8 uptime:u32 tx_ok:u32 tx_fail:u32 rx_pkts:u32
    # rx_drop:u32 crc_err:u16 fw:char[16] zero-padded
    payload = (
        bytes([1])
        + bytes([0x24, 0x6F, 0x28, 0xF2, 0xBD, 0xB4])
        + bytes([6])
        + (123456).to_bytes(4, "little")
        + (1000).to_bytes(4, "little")
        + (7).to_bytes(4, "little")
        + (2500).to_bytes(4, "little")
        + (3).to_bytes(4, "little")
        + (9).to_bytes(2, "little")
        + b"bridge-0.1.0".ljust(16, b"\x00")
    )
    assert len(payload) == 46
    st = parse_status(payload)
    assert st.proto == 1
    assert st.mac == bytes([0x24, 0x6F, 0x28, 0xF2, 0xBD, 0xB4])
    assert st.channel == 6
    assert st.uptime_ms == 123456
    assert st.tx_ok == 1000
    assert st.tx_fail == 7
    assert st.rx_pkts == 2500
    assert st.rx_drop == 3
    assert st.crc_err == 9
    assert st.fw == "bridge-0.1.0"
    # append-only doctrine: unknown tail bytes are ignored, not fatal
    assert parse_status(payload + b"\xde\xad").fw == "bridge-0.1.0"


def test_parse_status_too_short_raises():
    with pytest.raises(ValueError):
        parse_status(bytes(45))
