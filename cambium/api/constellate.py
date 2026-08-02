"""Constellate mapping mode: hold one fixture lit white for the camera.

MappingMode is the arbitration point between a Constellate mapping sweep and
the browser sim: while mapping is engaged, sim "frame" submissions are
DROPPED (mapping preempts sim -- a sweep must never race pattern frames, or
the camera would attribute a stray sim color to the swept fixture). The drop
count lives here so the /constellate endpoints can surface it.

Index resolution, v1: light(n) resolves n against the ACTIVE roster sorted
by mac -- the same stable order tx_partition() uses. The W5 sweep pipeline
will emit a frozen roster artifact per sweep and pin indexes to that file;
until then, do not edit the roster mid-sweep.
"""

from __future__ import annotations

import asyncio
import time

# NbIdentify color code 5 = white (see cambium.wire.packets.identify).
_WHITE = 5


class MappingMode:
    """One held identify target at a time, with refresh and idle auto-release.

    clock/sleep are injectable for tests (constructor injection,
    Constellate-style); production uses time.monotonic and asyncio.sleep.
    """

    def __init__(
        self,
        services,
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        identify_secs: int = 10,
        refresh_s: float = 4.0,
        idle_s: float = 60.0,
    ) -> None:
        self._services = services
        self._clock = clock
        self._sleep = sleep
        self._identify_secs = identify_secs
        self._refresh_s = refresh_s
        self._idle_s = idle_s
        self.active = False
        self.sim_frames_dropped = 0
        self._target_mac: str | None = None
        self._keeper_task: asyncio.Task | None = None
        self._last_light = 0.0
        # Serializes light()/off(): concurrent aiohttp handlers could both
        # read the same previous target across the awaits and leave the
        # loser lit un-cancelled for identify_secs.
        self._lock = asyncio.Lock()

    def drop_sim_frame(self) -> None:
        """Called by the WS layer for each sim frame preempted by mapping."""
        self.sim_frames_dropped += 1

    async def light(self, index: int) -> str:
        """Hold roster fixture #index (sorted by mac) lit white; return its mac.

        Returns once the transport write completes. Broadcast has no radio
        ack, so returning means "handed to the bridge", not "the fixture
        lit" -- the bridge STATUS tx counters are the delivery signal, and
        reading them is the doctor's job (W5).
        """
        ordered = sorted(self._services.roster.fixtures, key=lambda f: f.mac)
        if not 0 <= index < len(ordered):
            raise LookupError(
                f"led index {index} is out of range: the active roster has "
                f"{len(ordered)} fixtures (valid indexes 0..{len(ordered) - 1}); "
                f"check the sweep plan against the roster CSV"
            )
        mac = ordered[index].mac
        async with self._lock:
            if self._target_mac is not None and self._target_mac != mac:
                # secs=0 cancels the previous hold before the new one starts, so
                # the camera never sees two fixtures lit at once.
                await self._services.send_identify(self._target_mac, 0, 0, False)
            self._target_mac = mac
            self.active = True
            self._last_light = self._clock()
            await self._services.send_identify(mac, self._identify_secs, _WHITE, False)
            if self._keeper_task is None or self._keeper_task.done():
                self._keeper_task = asyncio.create_task(self._keeper())
            return mac

    async def off(self) -> None:
        """Cancel the current hold and release mapping mode."""
        async with self._lock:
            task, self._keeper_task = self._keeper_task, None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._release()

    async def _release(self) -> None:
        self.active = False
        mac, self._target_mac = self._target_mac, None
        if mac is not None:
            await self._services.send_identify(mac, 0, 0, False)

    async def _keeper(self) -> None:
        # The identify hold is deliberately short (identify_secs), so a dead
        # cambium can never leave a fixture pinned white. While alive we
        # refresh the hold; after idle_s without a light() call we let go so
        # a crashed sweep driver doesn't freeze the sim out forever.
        while self.active:
            await self._sleep(self._refresh_s)
            if not self.active:
                break
            if self._clock() - self._last_light >= self._idle_s:
                await self._release()
                break
            await self._services.send_identify(
                self._target_mac, self._identify_secs, _WHITE, False
            )
