"""TxScheduler: paces canonical frames onto the radio at the configured rate.

Latest-wins mailbox: the sim may push frames faster (or slower) than tx_hz;
each tick transmits the newest frame and intermediate ones are simply
dropped -- every packet is full state, so skipping frames loses nothing.

Stale input -> SILENCE, on purpose. The firmware's silence ladder (1 s hold,
3 s autonomous fallback with a 2 s crossfade, never blank) is the DESIGNED
loss behavior: when the simulator goes quiet, the correct move is to stop
transmitting and let the tree fall back to its autonomous life. Cambium
never fights that ladder by replaying or extrapolating stale frames.
"""

import asyncio
import time
from dataclasses import dataclass

from cambium.model import FixtureFrame


@dataclass
class TxStats:
    frames_in: int = 0    # frames handed to set_frame()
    packets_out: int = 0  # packets actually sent (stream + oneshots)
    ticks_idle: int = 0   # ticks where staleness/no-input meant silence


class TxScheduler:
    """Owns the downlink cadence. Single event loop, no locking needed.

    transport_send: async fn(raw_nb: bytes) -- raw Nb packet; the transport
    layer adds serial framing. packetize: fn(FixtureFrame) -> list[bytes] --
    injected (rather than roster+stamper) so the scheduler is pure timing;
    wire it to downlink.packetize.frame_to_packets in the daemon. clock and
    sleep are injected so tests run on a fake timeline.
    """

    def __init__(
        self,
        transport_send,
        packetize,
        tx_hz: float,
        stale_input_s: float,
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        if tx_hz <= 0:
            raise ValueError(
                f"tx_hz={tx_hz} must be > 0; set [radio] tx_hz in cambium.toml "
                f"(fixtures render at 10 Hz max, 8.0 is the intended rate)"
            )
        self._transport_send = transport_send
        self._packetize = packetize
        self._interval = 1.0 / tx_hz
        self._stale_input_s = stale_input_s
        self._clock = clock
        self._sleep = sleep
        self._frame: FixtureFrame | None = None
        self._frame_at = 0.0  # clock() when the current frame arrived
        self._running = False
        self.stats = TxStats()

    def set_frame(self, frame: FixtureFrame) -> None:
        """Latest-wins mailbox: replaces any frame the ticker hasn't sent."""
        self._frame = frame
        self._frame_at = self._clock()
        self.stats.frames_in += 1

    async def send_oneshot(self, raw_nb: bytes) -> None:
        """Send one packet NOW, bypassing the mailbox and the tick cadence.

        For identify / night-override / program commands from the api layer:
        these are one-shot commands, not stream state, so they must not wait
        for a tick or be dropped by latest-wins.
        """
        await self._transport_send(raw_nb)
        self.stats.packets_out += 1

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            frame = self._frame
            if frame is None or self._clock() - self._frame_at > self._stale_input_s:
                # No input, or input went stale: transmit NOTHING this tick
                # (see module docstring -- silence hands control back to the
                # fixtures' hold/fallback ladder).
                self.stats.ticks_idle += 1
                await self._sleep(self._interval)
                continue
            packets = self._packetize(frame)
            if not packets:
                # Fresh frame but nothing addressable (e.g. all ids unknown);
                # still pace the loop or we'd spin.
                await self._sleep(self._interval)
                continue
            # Stagger sends evenly across the tick: a burst of back-to-back
            # broadcasts is the worst case for ESP-NOW loss, and the spread
            # sleeps sum to one interval so the rate cap holds by structure.
            spacing = self._interval / len(packets)
            for raw in packets:
                if not self._running:
                    return
                await self._transport_send(raw)
                self.stats.packets_out += 1
                await self._sleep(spacing)
