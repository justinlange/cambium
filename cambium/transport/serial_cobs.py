"""SerialCobsTransport: COBS frames over the bridge board's USB serial port.

Owns the port exclusively (nothing else may hold it open) and reconnects
forever: the bridge rebooting or the cable being reseated must never require
a daemon restart. While the link is down cambium simply stops transmitting --
the fleet's own silence ladder (1 s hold -> 3 s autonomous fallback) takes
over, and cambium never fights it by replaying a backlog of stale frames on
reconnect.

Testability: the connection factory is injectable (Constellate FakeSerial
style), so every code path here runs against fake stream reader/writer pairs
with no real serial port.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import serial_asyncio

from cambium.wire.framing import FrameDecoder, encode_frame

from .base import Transport

# factory(port, baud) -> (reader, writer); the default opens a real port.
ConnectionFactory = Callable[
    [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]
LogFn = Callable[[str], None]


async def _open_serial(port: str, baud: int):
    return await serial_asyncio.open_serial_connection(url=port, baudrate=baud)


class SerialCobsTransport(Transport):
    def __init__(
        self,
        port: str,
        baud: int = 115200,
        reconnect_s: float = 2.0,
        *,
        connection_factory: ConnectionFactory | None = None,
        log: LogFn | None = None,
    ) -> None:
        super().__init__()
        self._port = port
        self._baud = baud
        self._reconnect_s = reconnect_s
        self._open = connection_factory or _open_serial
        # Callback, not print: the daemon decides where transport noise goes
        # (its logger, the api event stream, a test list).
        self._log = log or (lambda msg: None)
        # Single writer task drains this queue, so concurrent send_packet()
        # calls can never interleave bytes mid-frame on the port.
        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(
                "SerialCobsTransport is already started; call stop() first "
                "if you meant to restart it"
            )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._stats.connected = False

    async def _send_frame(self, ftype: int, payload: bytes) -> None:
        # Doctrine: when the link is down we DROP, not queue. Broadcast is
        # unacked and every packet must be independently meaningful, so a
        # frame delivered late (after reconnect) is worse than one never
        # sent -- it would fight the fixtures' own silence-ladder fallback.
        if not self._stats.connected:
            return
        self._tx_q.put_nowait(encode_frame(ftype, payload))

    async def _run(self) -> None:
        """Connect -> pump both directions -> on any error, retry forever."""
        while True:
            try:
                reader, writer = await self._open(self._port, self._baud)
            except Exception as exc:
                self._log(
                    f"serial open {self._port} failed ({exc!r}); retrying "
                    f"every {self._reconnect_s}s -- check the cable and the "
                    f"device path"
                )
                await asyncio.sleep(self._reconnect_s)
                continue

            self._log(f"serial {self._port} connected")
            self._stats.connected = True
            reader_task = asyncio.create_task(self._reader_loop(reader))
            writer_task = asyncio.create_task(self._writer_loop(writer))
            try:
                # Either loop dying (EOF, USB error, write failure, a raising
                # frame handler) ends the connection; the survivor is
                # cancelled in the finally below.
                done, _ = await asyncio.wait(
                    {reader_task, writer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    if (exc := task.exception()) is not None:
                        self._log(
                            f"serial {self._port} link error ({exc!r}); "
                            f"reconnecting -- check the cable and the "
                            f"device path"
                        )
            finally:
                self._stats.connected = False
                reader_task.cancel()
                writer_task.cancel()
                await asyncio.gather(
                    reader_task, writer_task, return_exceptions=True
                )
                try:
                    writer.close()
                except Exception:
                    pass  # a dead port may refuse close(); we abandon it anyway
                # Drop anything still queued: replaying a backlog after
                # reconnect would deliver stale frames (see _send_frame).
                while not self._tx_q.empty():
                    self._tx_q.get_nowait()
            await asyncio.sleep(self._reconnect_s)

    async def _reader_loop(self, reader) -> None:
        # Fresh decoder per connection: a half-received frame from the old
        # connection must not be spliced onto the new one's bytes.
        decoder = FrameDecoder()
        seen_errors = 0
        while True:
            data = await reader.read(4096)
            if not data:
                raise ConnectionError(
                    "serial EOF -- the bridge rebooted or was unplugged; "
                    "reconnect will keep retrying"
                )
            frames = decoder.feed(data)
            self._stats.crc_errors += decoder.crc_errors - seen_errors
            seen_errors = decoder.crc_errors
            for ftype, payload in frames:
                self._dispatch(ftype, payload)

    async def _writer_loop(self, writer) -> None:
        while True:
            frame = await self._tx_q.get()
            writer.write(frame)
            await writer.drain()
            self._stats.frames_tx += 1
