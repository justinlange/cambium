"""Bridge-frame demux: the ONLY place raw uplink bytes become objects.

The transport layer hands us (ftype, payload) pairs straight off the
FrameDecoder; everything above this module (FleetState, the WS API) sees
only the typed events defined here. Keeping the byte -> object boundary in
one module means the wire-garbage policy lives in exactly one place:
malformed or unknown input is counted and dropped, never raised, because
the uplink loop must survive anything the radio happens to hear.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from cambium.wire import framing
from cambium.wire.framing import BridgeStatus
from cambium.wire.packets import ChoreoState, Heartbeat, parse_packet, short_id_to_str


@dataclass
class PeerPacket:
    """One decoded ESP-NOW packet heard from a fleet node."""

    mac_short: str  # last 3 MAC bytes as 6 uppercase hex digits, e.g. '9E5AE8'
    rssi: int  # uplink RSSI measured at the bridge, dBm (signed)
    packet: Heartbeat | ChoreoState


@dataclass
class BridgeStatusEvent:
    """FTYPE_STATUS wrapped with a receipt timestamp.

    The payload decode is framing.parse_status (that contract is owned by
    wire/framing.py); this wrapper only adds received_at so staleness math
    upstream never has to guess when the counters were true.
    """

    status: BridgeStatus
    received_at: float  # injected clock() at receipt -- monotonic, not wall time


@dataclass
class BridgeLog:
    """One ASCII log line from the bridge board."""

    text: str


UplinkEvent = PeerPacket | BridgeStatusEvent | BridgeLog

# RADIO_RX payload contract (wire/framing.py): mac[6] + rssi:i8 + raw Nb packet.
_RX_PREFIX_LEN = 7


class UplinkParser:
    """Demux CRC-valid bridge frames into typed uplink events.

    Stateless per frame; the only state is the drop counters. The clock is
    injected so tests (and replay tools) control received_at.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Exposed drop counters -- the operator's only window into a lossy,
        # unacked uplink, so every drop path increments exactly one of them.
        self.malformed = 0  # RADIO_RX shorter than mac+rssi, or STATUS contract mismatch
        self.dropped = 0  # well-framed RADIO_RX whose inner packet we don't surface
        self.unknown_ftypes = 0  # ftype we don't demux (bridge echo or version skew)

    def feed(self, ftype: int, payload: bytes) -> UplinkEvent | None:
        """Turn one bridge frame into an event, or None if it was dropped.

        Never raises on wire content: if this throws, it is a cambium bug,
        not bad radio -- check the counters to see why frames vanish.
        """
        if ftype == framing.FTYPE_RADIO_RX:
            return self._radio_rx(payload)
        if ftype == framing.FTYPE_STATUS:
            try:
                status = framing.parse_status(payload)
            except ValueError:
                # Bridge firmware speaking an older STATUS layout; the fix is
                # syncing firmware/cambium versions, not crashing the uplink.
                self.malformed += 1
                return None
            return BridgeStatusEvent(status=status, received_at=self._clock())
        if ftype == framing.FTYPE_LOG:
            # errors="replace" because a corrupted log line must not kill the
            # loop; trailing CR/LF stripped so lines log cleanly upstream.
            return BridgeLog(text=payload.decode("ascii", errors="replace").rstrip("\r\n"))
        # RADIO_TX/CTRL are downlink-only; seeing one here (or a brand-new
        # ftype) means a bridge echo or version skew. Count it, keep going.
        self.unknown_ftypes += 1
        return None

    def _radio_rx(self, payload: bytes) -> PeerPacket | None:
        if len(payload) < _RX_PREFIX_LEN:
            self.malformed += 1
            return None
        (rssi,) = struct.unpack_from("<b", payload, 6)
        packet = parse_packet(payload[_RX_PREFIX_LEN:])
        if not isinstance(packet, (Heartbeat, ChoreoState)):
            # Unknown types, short bodies, and overheard downlink traffic
            # (our own broadcasts echo back off the air) all land here.
            self.dropped += 1
            return None
        # Fleet identity is the LAST 3 MAC bytes (docs/ARCHITECTURE.md), so
        # the short id comes from the sender MAC ESP-NOW actually saw, not
        # from the (spoofable, possibly stale) header src_id.
        return PeerPacket(
            mac_short=short_id_to_str(payload[3:6]), rssi=rssi, packet=packet
        )
