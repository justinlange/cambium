"""LoopbackTransport: every frame here rides the REAL encode_frame /
FrameDecoder byte path, so these tests double as an end-to-end check that the
transport layer and the production codec agree."""

import asyncio
import random

import pytest

from cambium.transport.loopback import LoopbackTransport
from cambium.wire import framing
from cambium.wire.packets import NbHeader, direct_frame, show_frame


async def test_send_packet_peer_sees_radio_tx_exact_bytes():
    t = LoopbackTransport()
    await t.start()
    raw = direct_frame(
        NbHeader(src_id=b"\xf2\xbe\xd4", seq=7),
        [(b"\x9e\x5a\xe8", 255, 128, 0, 10), (b"\xf2\xbd\xb4", 0, 0, 0, 255)],
        flags=1,
    )
    await t.send_packet(raw)
    ftype, payload = await asyncio.wait_for(t.peer.recv(), 1)
    assert ftype == framing.FTYPE_RADIO_TX
    assert payload == raw
    await t.stop()


async def test_send_ctrl_wraps_cmd_and_args():
    t = LoopbackTransport()
    await t.start()
    await t.send_ctrl(framing.CTRL_SET_CHANNEL, bytes([11]))
    ftype, payload = await asyncio.wait_for(t.peer.recv(), 1)
    assert ftype == framing.FTYPE_CTRL
    assert payload == bytes([framing.CTRL_SET_CHANNEL, 11])
    await t.stop()


async def test_inject_status_reaches_handler():
    t = LoopbackTransport()
    got: list[tuple[int, bytes]] = []
    t.set_frame_handler(lambda ft, pl: got.append((ft, pl)))
    await t.start()
    status = bytes(46)  # all-zero payload also proves COBS handles zeros
    t.peer.inject(framing.FTYPE_STATUS, status)
    assert got == [(framing.FTYPE_STATUS, status)]
    await t.stop()


async def test_loss_rate_honored_with_seeded_rng():
    seed, loss, n = 20260802, 0.4, 120
    t = LoopbackTransport(loss_rate=loss, rng=random.Random(seed))
    await t.start()
    frames = [show_frame(NbHeader(seq=i), phase=i, hue=i % 256) for i in range(n)]
    for raw in frames:
        await t.send_packet(raw)
    # Mirror the transport's rng consumption exactly: one draw per send.
    clone = random.Random(seed)
    expected = [raw for raw in frames if not (clone.random() < loss)]
    assert 0 < len(expected) < n  # the seed exercises both keep and drop
    delivered = []
    for _ in range(len(expected)):
        ftype, payload = await asyncio.wait_for(t.peer.recv(), 1)
        assert ftype == framing.FTYPE_RADIO_TX
        delivered.append(payload)
    assert delivered == expected
    # nothing extra arrives: the dropped frames are really gone
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(t.peer.recv(), 0.05)
    # tx counts what the daemon sent, not what survived the "air"
    assert t.stats.frames_tx == n
    await t.stop()


async def test_default_is_lossless():
    t = LoopbackTransport()
    await t.start()
    for i in range(50):
        await t.send_packet(bytes([1, 2, i]))
    for i in range(50):
        _, payload = await asyncio.wait_for(t.peer.recv(), 1)
        assert payload == bytes([1, 2, i])
    await t.stop()


async def test_stats_count_and_connected():
    t = LoopbackTransport()
    assert (t.stats.frames_tx, t.stats.frames_rx, t.stats.crc_errors) == (0, 0, 0)
    assert not t.stats.connected
    t.set_frame_handler(lambda ft, pl: None)
    await t.start()
    assert t.stats.connected
    await t.send_packet(b"\x01\x01\x00")
    await t.send_ctrl(framing.CTRL_STATUS_REQ)
    t.peer.inject(framing.FTYPE_LOG, b"boot ok")
    t.peer.inject(framing.FTYPE_STATUS, bytes(46))
    assert t.stats.frames_tx == 2
    assert t.stats.frames_rx == 2
    assert t.stats.crc_errors == 0
    await t.stop()
    assert not t.stats.connected


async def test_not_started_link_semantics():
    t = LoopbackTransport()
    got: list[int] = []
    t.set_frame_handler(lambda ft, pl: got.append(ft))
    # the daemon sending before start is a harness bug -> loud
    with pytest.raises(RuntimeError):
        await t.send_packet(b"\x01")
    # the peer can't know the daemon's state -> a frame into a down link
    # just vanishes, like any unacked broadcast
    t.peer.inject(framing.FTYPE_LOG, b"hello")
    assert got == []
    assert t.stats.frames_tx == 0
    assert t.stats.frames_rx == 0
