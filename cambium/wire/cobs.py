"""COBS (Consistent Overhead Byte Stuffing) + CRC-16/CCITT-FALSE.

Standard COBS per Cheshire & Baker: encoded output contains no 0x00 bytes, so
0x00 can delimit frames on the cambium <-> bridge-board serial link
(cambium.wire.framing). Encoding matches the canonical reference exactly --
including the boundary case where a maximal 254-byte non-zero block at end of
input gets NO phantom trailing group -- because the C firmware side will run
the same golden vectors (tests/golden/cobs_vectors.json).

crc16_ccitt is CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection,
no final XOR. check value: crc16_ccitt(b"123456789") == 0x29B1.
"""

from __future__ import annotations


def cobs_encode(data: bytes) -> bytes:
    """Encode; output has no zero bytes. Empty input encodes to b'\\x01'."""
    out = bytearray()
    i = 0
    n = len(data)
    while True:
        # A block is up to 254 non-zero bytes, terminated by a zero (consumed)
        # or by the 254-byte cap (code 0xFF = "no zero after this block").
        limit = min(i + 254, n)
        zero_at = data.find(0, i, limit)
        if zero_at < 0:
            block = data[i:limit]
            out.append(len(block) + 1)
            out += block
            i = limit
            if i >= n:
                break
        else:
            block = data[i:zero_at]
            out.append(len(block) + 1)
            out += block
            i = zero_at + 1
            if i >= n:
                # Input ended ON a zero: emit the empty final block for it.
                out.append(0x01)
                break
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Decode one COBS-encoded chunk (no 0x00 delimiter included).

    Raises ValueError on malformed input (embedded zero, truncated block) --
    the framing layer catches this and resyncs on the next delimiter.
    """
    out = bytearray()
    i = 0
    n = len(data)
    if n == 0:
        raise ValueError(
            "empty COBS chunk; feed the bytes between 0x00 delimiters, not the delimiters"
        )
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError(
                f"zero byte inside COBS data at index {i}; "
                f"strip 0x00 frame delimiters before decoding"
            )
        i += 1
        end = i + code - 1
        if end > n:
            raise ValueError(
                f"truncated COBS block: code {code} at index {i - 1} promises "
                f"{code - 1} bytes but only {n - i} remain; the frame was cut short"
            )
        block = data[i:end]
        if 0 in block:
            raise ValueError(
                f"zero byte inside COBS block starting at index {i}; "
                f"strip 0x00 frame delimiters before decoding"
            )
        out += block
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect/xorout)."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc
