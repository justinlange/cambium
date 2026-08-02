"""THE end-to-end proof: the whole daemon in one process, zero hardware.

LoopbackTransport's peer plays the bridge board (frames cross the REAL
COBS+CRC codec in both directions); aiohttp's TestClient plays the browser
sim (WS) and Constellate (HTTP). The scheduler's sleep is replaced by a
test-controlled gate, so a radio tick happens exactly when the test says
tick() -- no wall-clock sleeps, no races, every packet accounted for.
"""

import asyncio
import json
import struct
import time
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from cambium.config import CambiumConfig
from cambium.daemon import Daemon
from cambium.downlink.packetize import FLAG_MICRO_LEASE
from cambium.roster import Roster
from cambium.transport.loopback import LoopbackTransport
from cambium.wire import framing
from cambium.wire.packets import (
    DirectFrame,
    Identify,
    NbHeader,
    NbType,
    parse_packet,
    short_id_from_str,
    short_id_to_str,
)

BENCH10 = Path(__file__).parent.parent / "config" / "roster-bench10.csv"
BRIDGE_MAC = bytes.fromhex("68EE8FF2BED4")  # the serial_bridge PowerFeather


class ManualClock:
    """Frozen monotonic clock: frames never go stale, uptime is pinned."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class TickGate:
    """Replaces the scheduler's sleep: run() parks here until the test
    releases exactly one iteration with tick()."""

    def __init__(self) -> None:
        self._evt = asyncio.Event()

    async def __call__(self, dt: float) -> None:
        await self._evt.wait()
        self._evt.clear()

    def tick(self) -> None:
        self._evt.set()


async def wait_until(pred, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not met within timeout")


async def recv_nb(peer):
    """Next Nb packet the daemon broadcast, decoded through parse_packet."""
    ftype, payload = await asyncio.wait_for(peer.recv(), 2.0)
    assert ftype == framing.FTYPE_RADIO_TX
    return parse_packet(payload)


def status_payload(tx_ok: int = 5) -> bytes:
    """A bridge STATUS HELLO (wire/framing.py contract, 46 bytes)."""
    return struct.pack(
        "<B6sBIIIIIH16s",
        1, BRIDGE_MAC, 11, 1234, tx_ok, 0, 7, 0, 0, b"bridge-0.1.0",
    )


def hb_packet(short_mac: str, batt_ma: int) -> bytes:
    """A base-block heartbeat (24 B): header + fields through dl_rssi."""
    h = NbHeader(type=int(NbType.HEARTBEAT), src_id=short_id_from_str(short_mac))
    #                     batt_mv  batt_ma  soc rst ca mode pdr  rssi
    return h.pack() + struct.pack("<hhBBBBHb", 3700, batt_ma, 80, 0, 0, 2, 950, -60)


def radio_rx(full_mac_hex: str, rssi: int, raw_nb: bytes) -> bytes:
    """RADIO_RX payload: mac[6] + rssi:i8 + raw Nb packet."""
    return bytes.fromhex(full_mac_hex) + struct.pack("<b", rssi) + raw_nb


async def test_daemon_loopback_smoke():
    clock = ManualClock()
    gate = TickGate()
    transport = LoopbackTransport()
    daemon = Daemon(
        CambiumConfig(),
        transport,
        Roster.load(BENCH10),
        clock=clock,
        sched_sleep=gate,
    )
    await daemon.start(serve_http=False)  # the TestServer mounts daemon.app
    client = TestClient(TestServer(daemon.app))
    await client.start_server()
    peer = transport.peer
    try:
        # Let the scheduler park at the gate (first iteration is idle: no
        # frame yet) so every later iteration is tick-driven -- deterministic.
        await wait_until(lambda: daemon.scheduler.stats.ticks_idle == 1)
        ws = await client.ws_connect("/ws")

        # ---- (a) drive on + sim frame -> exact RGBW on the radio ----------
        await ws.send_str(json.dumps({"kind": "drive", "on": True}))
        frame_msg = {
            "kind": "frame",
            "seq": 1,
            "fixtures": [
                {"id": "B000", "rgb": [1.0, 0.2, 0.2]},
                {"id": "B001", "rgb": [2.0, 1.0, 0.4]},  # 2.0 clamps to 1.0
                {"id": "B002", "rgb": [0.2, 0.2, 0.2]},  # pure gray -> all W
            ],
        }
        await ws.send_str(json.dumps(frame_msg))
        await wait_until(lambda: daemon.scheduler.stats.frames_in == 1)
        gate.tick()
        pkt = await recv_nb(peer)
        assert isinstance(pkt, DirectFrame)
        assert pkt.flags & FLAG_MICRO_LEASE  # the stream leases the fixtures
        assert pkt.count == 3
        # Entries follow the stable mac-sorted partition order, and each
        # carries clamp + subtract white-extraction results exactly:
        # 0.2 * 255 = 51, 0.4 -> 102, 1.0 -> 255; w = min(r, g, b).
        assert [
            (short_id_to_str(e.id), e.r, e.g, e.b, e.w) for e in pkt.entries
        ] == [
            ("9F2694", 204, 0, 0, 51),     # B000: (255,51,51) - w51
            ("F2BDB4", 153, 153, 0, 102),  # B001: (255,255,102) - w102
            ("F2BDC0", 0, 0, 0, 51),       # B002: (51,51,51) - w51
        ]
        seq_a = pkt.h.seq
        assert seq_a >= 1
        gate.tick()  # same fresh frame retransmits on the next tick
        pkt2 = await recv_nb(peer)
        assert isinstance(pkt2, DirectFrame)
        assert pkt2.h.seq == seq_a + 1  # one gap-free seq stream from us

        # ---- (b) bridge STATUS HELLO -> we adopt the bridge's identity ----
        peer.inject(framing.FTYPE_STATUS, status_payload())
        await wait_until(lambda: daemon.stamper.src_id == BRIDGE_MAC[3:])
        gate.tick()
        pkt3 = await recv_nb(peer)
        assert pkt3.h.src_id == BRIDGE_MAC[3:]  # visible in the next header
        body = await (await client.get("/healthz")).json()
        assert body["ok"] is True and body["transport_connected"] is True
        status = await daemon.services.bridge_status()
        assert status["bridge_mac"] == "68EE8FF2BED4" and status["tx_ok"] == 5

        # ---- (c) fleet heartbeats -> /fleet + WS broadcasts ---------------
        peer.inject(  # B003, charging (batt_ma > 0)
            framing.FTYPE_RADIO_RX,
            radio_rx("68EE8FF2BE38", -55, hb_packet("F2BE38", 250)),
        )
        peer.inject(  # B004, also charging
            framing.FTYPE_RADIO_RX,
            radio_rx("68EE8FF2BE60", -60, hb_packet("F2BE60", 120)),
        )
        hbs, chargings = [], []
        while len(hbs) < 2 or len(chargings) < 1:
            msg = await ws.receive_json(timeout=2.0)
            if msg.get("kind") == "hb":
                hbs.append(msg)
            elif msg.get("kind") == "charging":
                chargings.append(msg)
        assert {m["mac"] for m in hbs} == {"F2BE38", "F2BE60"}
        assert {m["rssi"] for m in hbs} == {-55, -60}
        # The 1/s rate limit emitted exactly one charging change (count 0->1);
        # the 1->2 change inside the window stayed pending.
        assert chargings == [{"kind": "charging", "count": 1, "macs": ["F2BE38"]}]
        assert daemon.fleet.charging_count() == 2
        assert daemon.fleet.listener_errors == 0  # sync hub listener is legal
        snap = await (await client.get("/fleet")).json()
        assert set(snap) == {"F2BE38", "F2BE60"}
        assert snap["F2BE38"]["online"] is True
        assert snap["F2BE38"]["fixture_id"] == "B003"
        assert snap["F2BE38"]["telemetry"]["batt_ma"] == 250
        assert snap["F2BE60"]["fixture_id"] == "B004"

        # ---- (d) Constellate mapping preempts the sim stream --------------
        resp = await client.get("/constellate/light", params={"led": "0"})
        body = await resp.json()
        assert body["ok"] is True
        assert body["mac"] == "9F2694"  # mac-sorted-first bench fixture
        ident = await recv_nb(peer)  # oneshot: no tick needed
        assert isinstance(ident, Identify)
        assert ident.target_id == short_id_from_str("9F2694")
        assert (ident.secs, ident.color) == (10, 5)  # held white for 10 s

        tx_before = transport.stats.frames_tx
        await ws.send_str(json.dumps({**frame_msg, "seq": 2}))
        await wait_until(lambda: daemon.mapping.sim_frames_dropped == 1)
        assert daemon.scheduler.stats.frames_in == 1  # never hit the mailbox
        # Even the mailbox's held pre-mapping frame must not transmit: a tick
        # while mapping holds the tree emits nothing at all.
        gate.tick()
        for _ in range(20):
            await asyncio.sleep(0)
        assert transport.stats.frames_tx == tx_before

        resp = await client.get("/constellate/off")
        assert (await resp.json())["sim_frames_dropped"] == 1
        cancel = await recv_nb(peer)
        assert isinstance(cancel, Identify)
        assert cancel.target_id == short_id_from_str("9F2694")
        assert cancel.secs == 0  # the hold is released, not left to expire

        # Sim frames flow again after mapping releases.
        await ws.send_str(json.dumps({**frame_msg, "seq": 3}))
        await wait_until(lambda: daemon.scheduler.stats.frames_in == 2)
        gate.tick()
        pkt4 = await recv_nb(peer)
        assert isinstance(pkt4, DirectFrame)
        assert pkt4.count == 3

        # ---- (e) clean stop ----------------------------------------------
        await ws.close()
    finally:
        await client.close()  # app cleanup (mapping.off) runs on live transport
        await daemon.stop()
    assert transport.stats.connected is False
    # Every packet the scheduler emitted reached the transport, and nothing
    # else was ever transmitted: 3 stream ticks + identify + cancel + 1 tick.
    assert daemon.scheduler.stats.packets_out == transport.stats.frames_tx == 6
