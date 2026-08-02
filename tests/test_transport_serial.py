"""SerialCobsTransport against injected fake stream reader/writer pairs
(Constellate FakeSerial style): framing roundtrips, reconnect-forever, and
garbage resync -- no real serial port anywhere."""

import asyncio

from cambium.transport.serial_cobs import SerialCobsTransport
from cambium.wire import framing
from cambium.wire.framing import FrameDecoder, encode_frame
from cambium.wire.packets import NbHeader, show_frame


class FakeReader:
    """Feed-controlled stand-in for asyncio.StreamReader."""

    def __init__(self):
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, data: bytes) -> None:
        self._q.put_nowait(data)

    def eof(self) -> None:
        self._q.put_nowait(b"")  # read() returning b"" = EOF, like the real one

    async def read(self, n: int) -> bytes:
        return await self._q.get()


class FakeWriter:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeSerialFactory:
    """Injectable connection factory: counts open attempts, can fail first N."""

    def __init__(self, fail_first: int = 0):
        self.calls = 0
        self.fail_first = fail_first
        self.connections: list[tuple[FakeReader, FakeWriter]] = []

    async def __call__(self, port: str, baud: int):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise OSError(f"could not open {port}")
        pair = (FakeReader(), FakeWriter())
        self.connections.append(pair)
        return pair


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "timed out waiting for condition"
        await asyncio.sleep(0.001)


def make(factory, **kw) -> SerialCobsTransport:
    return SerialCobsTransport(
        "/dev/ttyFAKE", reconnect_s=0.001, connection_factory=factory, **kw
    )


async def test_tx_roundtrips_through_real_framing():
    factory = FakeSerialFactory()
    t = make(factory)
    await t.start()
    try:
        await until(lambda: t.stats.connected)
        _, writer = factory.connections[0]
        raw = show_frame(NbHeader(src_id=b"\xf2\xbe\xd4", seq=3), phase=500, hue=17)
        await t.send_packet(raw)
        await t.send_ctrl(framing.CTRL_SET_CHANNEL, bytes([11]))
        # each encoded frame ends in exactly one 0x00 delimiter
        await until(lambda: writer.written.count(0) >= 2)
        dec = FrameDecoder()
        assert dec.feed(bytes(writer.written)) == [
            (framing.FTYPE_RADIO_TX, raw),
            (framing.FTYPE_CTRL, bytes([framing.CTRL_SET_CHANNEL, 11])),
        ]
        assert t.stats.frames_tx == 2
    finally:
        await t.stop()


async def test_rx_dispatches_to_handler():
    factory = FakeSerialFactory()
    t = make(factory)
    got: list[tuple[int, bytes]] = []
    t.set_frame_handler(lambda ft, pl: got.append((ft, pl)))
    await t.start()
    try:
        await until(lambda: t.stats.connected)
        reader, _ = factory.connections[0]
        payload = bytes(6) + b"\xb5" + b"\x01\x01" + bytes(11)  # mac+rssi+nb
        reader.feed(encode_frame(framing.FTYPE_RADIO_RX, payload))
        await until(lambda: got)
        assert got == [(framing.FTYPE_RADIO_RX, payload)]
        assert t.stats.frames_rx == 1
    finally:
        await t.stop()


async def test_disconnect_then_reconnect_via_factory():
    factory = FakeSerialFactory()
    t = make(factory)
    await t.start()
    try:
        await until(lambda: t.stats.connected)
        assert factory.calls == 1
        reader1, writer1 = factory.connections[0]
        reader1.eof()  # bridge unplugged / rebooted
        await until(lambda: factory.calls >= 2 and t.stats.connected)
        assert writer1.closed  # old connection torn down, not leaked
        # the new connection is live: a frame sent now lands on writer #2
        _, writer2 = factory.connections[1]
        await t.send_packet(b"\x01\x02")
        await until(lambda: len(writer2.written) > 0)
        assert len(writer1.written) == 0
    finally:
        await t.stop()
    assert not t.stats.connected


async def test_open_failures_retry_forever_until_success():
    factory = FakeSerialFactory(fail_first=3)
    logs: list[str] = []
    t = make(factory, log=logs.append)
    await t.start()
    try:
        await until(lambda: t.stats.connected)
        assert factory.calls == 4  # 3 failures + 1 success
        assert any("retry" in m for m in logs)  # logged via callback, not print
    finally:
        await t.stop()


async def test_send_while_disconnected_drops_not_queues():
    factory = FakeSerialFactory(fail_first=10**9)  # the port never appears
    t = make(factory)
    await t.start()
    try:
        await until(lambda: factory.calls >= 2)  # it is retrying
        await t.send_packet(b"\x01\x02\x03")  # must not raise
        # doctrine: a stale frame delivered after reconnect is worse than a
        # dropped one, so nothing is queued and nothing counts as tx
        assert t.stats.frames_tx == 0
    finally:
        await t.stop()


async def test_garbage_resync_counted():
    factory = FakeSerialFactory()
    t = make(factory)
    got: list[tuple[int, bytes]] = []
    t.set_frame_handler(lambda ft, pl: got.append((ft, pl)))
    await t.start()
    try:
        await until(lambda: t.stats.connected)
        reader, _ = factory.connections[0]
        good = encode_frame(framing.FTYPE_LOG, b"survived")
        # truncated-COBS junk, then a valid frame in the same read
        reader.feed(b"\x37\x01\x02\x00" + good)
        await until(lambda: got)
        assert got == [(framing.FTYPE_LOG, b"survived")]
        assert t.stats.crc_errors == 1
        assert t.stats.frames_rx == 1
    finally:
        await t.stop()
