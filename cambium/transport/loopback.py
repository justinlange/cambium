"""LoopbackTransport: in-process transport pair for tests and the fake fleet.

The daemon side implements Transport; .peer exposes the far end (what the
bridge board would see). Frames pass through the REAL encode_frame /
FrameDecoder bytes in both directions: the loopback exercises the production
codec so a framing bug cannot hide behind a shortcut that hands payloads
across directly.

Optional loss_rate drops daemon->peer frames pseudo-randomly (injectable rng
for deterministic tests) to imitate the unacked broadcast radio: any packet
can vanish and nobody retries.
"""

from __future__ import annotations

import asyncio
import random

from cambium.wire.framing import FrameDecoder, encode_frame

from .base import Transport


class LoopbackPeer:
    """The far end of the loopback (the fake bridge/fleet side)."""

    def __init__(self, transport: "LoopbackTransport") -> None:
        self._transport = transport
        self._decoder = FrameDecoder()  # decodes frames the daemon sent
        self._rx: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()

    async def recv(self) -> tuple[int, bytes]:
        """Next (ftype, payload) frame the daemon transmitted."""
        return await self._rx.get()

    def inject(self, ftype: int, payload: bytes) -> None:
        """Deliver a frame TO the daemon's handler, through the real codec."""
        self._transport._inject_from_peer(ftype, payload)


class LoopbackTransport(Transport):
    def __init__(
        self,
        loss_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__()
        if not 0.0 <= loss_rate <= 1.0:
            raise ValueError(
                f"loss_rate={loss_rate} is not a probability; use a value "
                f"in 0.0..1.0 (0 = lossless)"
            )
        self._loss_rate = loss_rate
        self._rng = rng or random.Random()
        self._rx_decoder = FrameDecoder()  # decodes frames the peer injected
        self._peer = LoopbackPeer(self)

    @property
    def peer(self) -> LoopbackPeer:
        return self._peer

    async def start(self) -> None:
        self._stats.connected = True

    async def stop(self) -> None:
        self._stats.connected = False

    async def _send_frame(self, ftype: int, payload: bytes) -> None:
        if not self._stats.connected:
            # Unlike a serial link dropping out (a normal runtime state), a
            # never-started loopback is always a harness bug -- be loud.
            raise RuntimeError(
                "LoopbackTransport is not started; await start() before "
                "sending (and do not send after stop())"
            )
        # frames_tx counts what the daemon transmitted; loss happens "in the
        # air", downstream of tx -- same as the real radio.
        self._stats.frames_tx += 1
        if self._loss_rate > 0.0 and self._rng.random() < self._loss_rate:
            return  # dropped in the air -- broadcast is unacked, nobody retries
        wire = encode_frame(ftype, payload)
        for ft, pl in self._peer._decoder.feed(wire):
            self._peer._rx.put_nowait((ft, pl))

    def _inject_from_peer(self, ftype: int, payload: bytes) -> None:
        if not self._stats.connected:
            return  # link down: an unacked broadcast frame just vanishes
        wire = encode_frame(ftype, payload)
        frames = self._rx_decoder.feed(wire)
        # Mirror the decoder's count (stays 0 unless the codec itself is
        # broken, which is exactly what this loopback exists to catch).
        self._stats.crc_errors = self._rx_decoder.crc_errors
        for ft, pl in frames:
            self._dispatch(ft, pl)
