"""Cambium <-> bridge-board serial framing contract (defined HERE).

Cambium owns this contract; the bridge-board firmware (firmware/cambium_bridge)
implements the other end. It is deliberately independent of the ESP-NOW packet
layer: the serial link carries opaque payloads, some of which happen to be Nb
packets.

Frame on the wire:

    COBS( [ftype:u8][payload...][crc16:u16 LE] ) 0x00

  - crc16 is CRC-16/CCITT-FALSE over ftype + payload (cambium.wire.cobs).
  - The whole [ftype][payload][crc] blob is COBS-encoded, then a single 0x00
    delimiter follows. COBS output contains no zeros, so a receiver that lost
    sync just skips to the next 0x00 (resync is free).
  - Bare 0x00 bytes between frames are idle/keepalive and are ignored.

Frame types (ftype):
  0x01 RADIO_TX  cambium -> bridge: payload = raw Nb packet to broadcast
  0x02 RADIO_RX  bridge -> cambium: payload = mac[6] + rssi:i8 + raw Nb packet
  0x03 CTRL      cambium -> bridge: cmd:u8 (0x01 STATUS_REQ,
                 0x02 SET_CHANNEL + ch:u8, 0x03 REBOOT)
  0x04 STATUS    bridge -> cambium: packed BridgeStatus (see parse_status)
  0x05 LOG       bridge -> cambium: ASCII log line

STATUS payload is append-only like the Nb structs: parse_status accepts any
length >= the known 46 bytes and ignores unknown tails, so the bridge firmware
can grow it without a flag day.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .cobs import cobs_decode, cobs_encode, crc16_ccitt

FTYPE_RADIO_TX = 0x01
FTYPE_RADIO_RX = 0x02
FTYPE_CTRL = 0x03
FTYPE_STATUS = 0x04
FTYPE_LOG = 0x05

CTRL_STATUS_REQ = 0x01
CTRL_SET_CHANNEL = 0x02
CTRL_REBOOT = 0x03

DELIMITER = b"\x00"

# A frame that grows past this without a delimiter means the link is spraying
# garbage or the delimiter got lost; drop and resync rather than buffer
# forever. Real frames are tiny (ESP-NOW payload max 250 B -> ~265 B encoded).
_MAX_CHUNK = 4096


def encode_frame(ftype: int, payload: bytes) -> bytes:
    """[ftype][payload][crc16 LE] -> COBS -> + 0x00 delimiter."""
    if not 0 <= ftype <= 0xFF:
        raise ValueError(f"ftype={ftype} does not fit uint8; use the FTYPE_* constants")
    body = bytes([ftype]) + payload
    body += struct.pack("<H", crc16_ccitt(body))
    return cobs_encode(body) + DELIMITER


class FrameDecoder:
    """Incremental decoder for the serial byte stream.

    feed() returns the list of complete, CRC-valid (ftype, payload) frames the
    new bytes finished. Garbage between delimiters (failed COBS decode, frames
    too short to hold a CRC, CRC mismatch) is dropped, counted in crc_errors,
    and decoding resumes at the next delimiter -- one bad byte never wedges
    the stream.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        # Single counter on purpose: COBS damage and CRC damage are both "the
        # link corrupted a frame" and the operator response is identical.
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        frames: list[tuple[int, bytes]] = []
        while True:
            idx = self._buf.find(0)
            if idx < 0:
                if len(self._buf) > _MAX_CHUNK:
                    # No delimiter in sight: dump the buffer, count it once.
                    self._buf.clear()
                    self.crc_errors += 1
                break
            chunk = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if not chunk:
                continue  # idle 0x00 between frames, not an error
            try:
                body = cobs_decode(chunk)
            except ValueError:
                self.crc_errors += 1
                continue
            if len(body) < 3:  # need at least ftype + crc16
                self.crc_errors += 1
                continue
            (crc,) = struct.unpack_from("<H", body, len(body) - 2)
            if crc16_ccitt(body[:-2]) != crc:
                self.crc_errors += 1
                continue
            frames.append((body[0], body[1:-2]))
        return frames


@dataclass
class BridgeStatus:
    proto: int
    mac: bytes  # full 6-byte ESP32 MAC
    channel: int
    uptime_ms: int
    tx_ok: int
    tx_fail: int
    rx_pkts: int
    rx_drop: int
    crc_err: int
    fw: str


_STATUS_FMT = "<B6sBIIIIIH16s"
STATUS_LEN = struct.calcsize(_STATUS_FMT)  # 46


def parse_status(payload: bytes) -> BridgeStatus:
    """Decode an FTYPE_STATUS payload. Accepts appended (unknown) tails."""
    if len(payload) < STATUS_LEN:
        raise ValueError(
            f"STATUS payload is {len(payload)} bytes, need >= {STATUS_LEN}; "
            f"the bridge firmware and cambium disagree on the contract -- "
            f"update whichever is older"
        )
    proto, mac, channel, uptime_ms, tx_ok, tx_fail, rx_pkts, rx_drop, crc_err, fw = (
        struct.unpack_from(_STATUS_FMT, payload)
    )
    return BridgeStatus(
        proto=proto, mac=mac, channel=channel, uptime_ms=uptime_ms,
        tx_ok=tx_ok, tx_fail=tx_fail, rx_pkts=rx_pkts, rx_drop=rx_drop,
        crc_err=crc_err,
        fw=fw.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
    )
