"""Transport ABC: the daemon's link to the bridge board.

Transports carry FRAMES (cambium.wire.framing), not packets -- the
RADIO_TX/RX wrapping lives here so everything above (downlink, uplink, api)
thinks in Nb packets and everything below (a serial port, an in-process
loopback) thinks in COBS frames. Nothing above this layer touches framing;
nothing below it touches packet structs.

Uplink is push-based: the daemon registers one handler via
set_frame_handler() and the transport calls it with every complete,
CRC-valid inbound (ftype, payload). The handler runs on the transport's RX
path, so it must be quick and non-blocking.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from cambium.wire.framing import FTYPE_CTRL, FTYPE_RADIO_TX

# fn(ftype, payload) for each complete inbound frame.
FrameHandler = Callable[[int, bytes], None]


@dataclass
class TransportStats:
    frames_tx: int = 0
    frames_rx: int = 0
    crc_errors: int = 0
    connected: bool = False


class Transport(ABC):
    def __init__(self) -> None:
        self._handler: FrameHandler | None = None
        self._stats = TransportStats()

    @abstractmethod
    async def start(self) -> None:
        """Bring the link up (or start trying to)."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear the link down; safe to call when never started."""

    @abstractmethod
    async def _send_frame(self, ftype: int, payload: bytes) -> None:
        """Encode and transmit one frame; each subclass owns the byte path."""

    async def send_packet(self, raw_nb: bytes) -> None:
        """Broadcast one raw Nb packet: wraps it in RADIO_TX framing."""
        await self._send_frame(FTYPE_RADIO_TX, raw_nb)

    async def send_ctrl(self, cmd: int, args: bytes = b"") -> None:
        """Send a bridge control frame (CTRL_* constants in wire.framing)."""
        if not 0 <= cmd <= 0xFF:
            raise ValueError(
                f"cmd={cmd} does not fit uint8; use the CTRL_* constants "
                f"from cambium.wire.framing"
            )
        await self._send_frame(FTYPE_CTRL, bytes([cmd]) + args)

    def set_frame_handler(self, fn: FrameHandler | None) -> None:
        """Register the daemon's uplink hook (None to detach)."""
        self._handler = fn

    @property
    def stats(self) -> TransportStats:
        return self._stats

    def _dispatch(self, ftype: int, payload: bytes) -> None:
        """Count one inbound frame and hand it to the handler, if any."""
        # Counted before the handler runs so stats stay truthful even if a
        # buggy handler raises.
        self._stats.frames_rx += 1
        if self._handler is not None:
            self._handler(ftype, payload)
