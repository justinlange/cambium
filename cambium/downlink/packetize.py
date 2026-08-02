"""FixtureFrame -> NB_DIRECT_FRAME packet bytes. Pure functions, no I/O.

Chunking follows Roster.tx_partition() (stable, mac-sorted, <= 18 per
packet) so a lost packet always affects the same predictable subset of
fixtures -- see the WHY on tx_partition.
"""

import time

from cambium.model import FixtureFrame
from cambium.roster import Roster
from cambium.wire.packets import NbHeader, direct_frame, short_id_from_str

# NbDirectFrame.flags bit0: 10 s micro-lease grant. Set by default so the
# stream itself keeps the fixtures leased -- all overrides expire, and a
# stalled cambium releases the tree back to autonomy within 10 s.
FLAG_MICRO_LEASE = 0x01


class HeaderStamper:
    """Stamps outgoing NbHeaders: one monotonic seq stream, one identity.

    seq starts at 1 and is shared across all packet types so receivers see a
    single gap-free counter from us (gaps = radio loss, not interleaving).
    src_id starts as 00:00:00 and is adopted from the bridge's STATUS HELLO
    once its MAC is known -- on air, the bridge is who we are.

    The clock is injected (Constellate-style) so tests pin uptime_ms exactly.
    """

    def __init__(self, clock=time.monotonic, src_id: bytes = b"\x00\x00\x00"):
        self._clock = clock
        self._t0 = clock()
        self._seq = 0
        self.src_id = src_id

    def set_src_id(self, src_id: bytes) -> None:
        if len(src_id) != 3:
            raise ValueError(
                f"src_id must be 3 bytes, got {len(src_id)}; pass the last 3 "
                f"bytes of the bridge MAC, e.g. BridgeStatus.mac[3:]"
            )
        self.src_id = src_id

    def stamp(self) -> NbHeader:
        self._seq += 1
        return NbHeader(
            src_id=self.src_id,
            seq=self._seq,
            uptime_ms=int((self._clock() - self._t0) * 1000),
        )


def frame_to_packets(
    frame: FixtureFrame,
    roster: Roster,
    stamper: HeaderStamper,
    flags: int = FLAG_MICRO_LEASE,
) -> list[bytes]:
    """Split one canonical frame into ready-to-send NB_DIRECT_FRAME packets.

    Each roster chunk carries entries ONLY for macs present in frame.colors:
    absence means "no opinion, stay autonomous", which is different from
    black (r=g=b=w=0, "off"). A chunk with no addressed fixtures produces no
    packet at all -- broadcast airtime is the scarce resource.
    """
    out: list[bytes] = []
    for chunk in roster.tx_partition():
        entries = [
            (short_id_from_str(f.mac), c.r, c.g, c.b, c.w)
            for f in chunk
            if (c := frame.colors.get(f.mac)) is not None
        ]
        if not entries:
            continue
        out.append(direct_frame(stamper.stamp(), entries, flags))
    return out
