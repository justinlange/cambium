"""FakeFleet: N VirtualFixtures plus a fake bridge persona on a loopback peer.

Wire-level honesty: everything crosses the LoopbackTransport as real framed
bytes, so the daemon exercises exactly the code paths it uses against the
physical bridge -- STATUS HELLO adoption, RADIO_RX heartbeat parsing, the
lot. The fake bridge persona answers CTRL STATUS_REQ and emits STATUS at
1 Hz just like cambium_bridge.ino.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path

from cambium.model import Fixture, FixtureClass
from cambium.transport.loopback import LoopbackPeer
from cambium.wire.framing import (
    FTYPE_CTRL,
    FTYPE_RADIO_RX,
    FTYPE_RADIO_TX,
    FTYPE_STATUS,
)

from .fixture_sim import VirtualFixture

# Same layout the real bridge packs (wire/framing.py _STATUS_FMT).
_STATUS_FMT = "<B6sBIIIIIH16s"
# Synthetic bridge MAC: last 3 bytes "FAB000" become the daemon's src_id.
_BRIDGE_MAC = bytes.fromhex("FAB000FAB000")

_ROLE_TO_CLASS = {
    "downlight": FixtureClass.DOWNLIGHT,
    "uplight": FixtureClass.UPLIGHT,
    "chandelier": FixtureClass.CHANDELIER,
    "perimeter": FixtureClass.PERIMETER,
}


def synthetic_fixtures(count: int) -> list[Fixture]:
    """A bench line: `count` downlights 1 m apart at z=2.5, macs FA0000..."""
    return [
        Fixture(
            mac=f"FA{i:04X}",
            fixture_id=f"B{i:03d}",
            cls=FixtureClass.DOWNLIGHT,
            xyz=(float(i), 0.0, 2.5),
        )
        for i in range(count)
    ]


def fixtures_from_file(path: str | Path) -> list[Fixture]:
    """Load an Elliot-schema fixtures json (resonance.fixtures/0.3 shape).

    Positions are kept verbatim (Blender Z-up meters -- fine for the
    viewer's plan/elevation projections). Roles map to firmware classes;
    unknown roles fall back to DOWNLIGHT with a note in the error contract:
    the file is display data, not a wire contract.
    """
    doc = json.loads(Path(path).read_text())
    fixtures = doc.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError(
            f"{path}: no 'fixtures' array -- expected an Elliot-schema "
            f"fixtures json like resonance-lighting/app/public/"
            f"fixtures-bench10.json"
        )
    out: list[Fixture] = []
    for i, f in enumerate(fixtures):
        pos = f.get("position") or (float(i), 0.0, 2.5)
        out.append(
            Fixture(
                mac=f"FA{i:04X}",  # synthetic; outside real fleet OUI ranges
                fixture_id=f.get("fixture_id") or f"B{i:03d}",
                cls=_ROLE_TO_CLASS.get(f.get("role", ""), FixtureClass.DOWNLIGHT),
                xyz=(float(pos[0]), float(pos[1]), float(pos[2])),
            )
        )
    return out


class FakeFleet:
    """Owns the virtual fixtures and speaks bridge on a loopback peer."""

    def __init__(
        self,
        fixtures: list[Fixture],
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        start_night: bool = False,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._boot_at = clock()
        self._rx_pkts = 0
        self._tx_ok = 0
        self.fixtures: dict[str, VirtualFixture] = {
            f.mac: VirtualFixture(
                f.mac, f.cls, f.xyz, clock=clock, start_night=start_night
            )
            for f in fixtures
        }
        self._by_mac_fixture = {f.mac: f for f in fixtures}
        self._peer: LoopbackPeer | None = None
        self._tasks: list[asyncio.Task] = []

    def fixture_id(self, mac: str) -> str | None:
        f = self._by_mac_fixture.get(mac)
        return f.fixture_id if f else None

    async def start(self, peer: LoopbackPeer) -> None:
        self._peer = peer
        self._inject_status()  # HELLO: the daemon adopts our src_id from this
        self._tasks = [
            asyncio.create_task(self._pump(), name="fakefleet-pump"),
            asyncio.create_task(self._tick(), name="fakefleet-tick"),
            asyncio.create_task(self._status_loop(), name="fakefleet-status"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------

    async def _pump(self) -> None:
        assert self._peer is not None
        while True:
            ftype, payload = await self._peer.recv()
            if ftype == FTYPE_RADIO_TX:
                # One broadcast reaches every fixture -- like the air does.
                self._tx_ok += 1
                for vf in self.fixtures.values():
                    vf.consume(payload)
            elif ftype == FTYPE_CTRL and payload[:1] == b"\x01":
                self._inject_status()  # STATUS_REQ

    async def _tick(self) -> None:
        # 10 Hz render + heartbeat collection (fixture.ino's 100 ms gate).
        assert self._peer is not None
        while True:
            now = self._clock()
            for vf in self.fixtures.values():
                vf.tick(now)
                for hb in vf.next_uplink(now):
                    self._rx_pkts += 1
                    mac6 = b"\x68\xee\x8f" + bytes.fromhex(vf.mac)
                    self._peer.inject(
                        FTYPE_RADIO_RX,
                        mac6 + struct.pack("<b", vf.dl_rssi) + hb,
                    )
            await self._sleep(0.1)

    async def _status_loop(self) -> None:
        while True:
            await self._sleep(1.0)
            self._inject_status()

    def _inject_status(self) -> None:
        assert self._peer is not None
        payload = struct.pack(
            _STATUS_FMT,
            1,
            _BRIDGE_MAC,
            11,  # the fleet channel; doctor checks this against config
            int((self._clock() - self._boot_at) * 1000) & 0xFFFFFFFF,
            self._tx_ok,
            0,
            self._rx_pkts,
            0,
            0,
            b"fake-bridge-0.1",
        )
        self._peer.inject(FTYPE_STATUS, payload)
