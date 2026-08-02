"""Daemon: the asyncio composition root. Wiring only, no business logic.

Every module keeps its single responsibility; this file just connects them:

    transport rx --> UplinkParser --> FleetState --> WsHub (via build_app)
                              \\--> BridgeStatus cache + HeaderStamper src_id
    WS/HTTP (build_app) --> DaemonServices methods here --> normalize/wire
                        --> TxScheduler --> transport tx

The only decisions made here are arbitration ones that belong to the
composition root because they span modules: sim frames reach the scheduler
only while drive is on and mapping is off, and the scheduler's packetizer
emits nothing while either condition fails (the latest-wins mailbox may hold
a pre-mapping frame for up to stale_input_s -- the camera must never see it,
and silence hands the tree back to its own hold/fallback ladder).

Injectables (constructor injection, Constellate-style): clock feeds the
stamper/scheduler/uplink/fleet so tests run on one fake timeline; sched_sleep
lets tests turn scheduler ticks into explicit events.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from cambium.api.constellate import MappingMode
from cambium.api.server import build_app
from cambium.api.services import DaemonServices
from cambium.config import CambiumConfig
from cambium.downlink.packetize import FLAG_MICRO_LEASE, HeaderStamper, frame_to_packets
from cambium.downlink.scheduler import TxScheduler
from cambium.model import Fixture, FixtureFrame
from cambium.normalize.frames import sim_to_canonical
from cambium.roster import Roster
from cambium.transport.base import Transport
from cambium.uplink.fleetstate import FleetState
from cambium.uplink.parse import BridgeLog, BridgeStatusEvent, UplinkParser
from cambium.wire.packets import (
    BROADCAST_ID,
    direct_frame,
    force_lifecycle,
    identify,
    program_set,
    set_rate,
    short_id_from_str,
    show_frame,
)

log = logging.getLogger(__name__)

_HEX = set("0123456789ABCDEF")


class Daemon:
    def __init__(
        self,
        config: CambiumConfig,
        transport: Transport,
        roster: Roster,
        *,
        clock=time.monotonic,
        sched_sleep=asyncio.sleep,
    ) -> None:
        self._config = config
        self._transport = transport
        self._roster = roster
        self._drive_on = False
        self._bridge: BridgeStatusEvent | None = None

        self.stamper = HeaderStamper(clock=clock)
        self.scheduler = TxScheduler(
            transport.send_packet,
            self._packetize,
            config.tx_hz,
            config.stale_input_s,
            clock=clock,
            sleep=sched_sleep,
        )
        self.uplink = UplinkParser(clock=clock)
        self.fleet = FleetState(clock=clock, roster=roster)

        self.services = DaemonServices(
            roster=roster,
            fleet=self.fleet,
            submit_sim_frame=self._submit_sim_frame,
            set_drive=self._set_drive,
            send_show=self._send_show,
            send_identify=self._send_identify,
            send_night=self._send_night,
            send_program=self._send_program,
            send_set_rate=self._send_set_rate,
            send_solid_debug=self._send_solid_debug,
            bridge_status=self._bridge_status,
        )
        self.mapping = MappingMode(self.services)
        self.app = build_app(self.services, config, mapping=self.mapping)

        # Uplink frames arrive on the transport's rx path (sync, must not
        # block); a queue + pump task moves them onto our own async footing
        # while preserving arrival order.
        self._rx_q: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, serve_http: bool = True) -> None:
        """Bring everything up. serve_http=False lets tests mount self.app in
        aiohttp's TestServer instead of binding config.host:config.port."""
        # Handler registered before start() so no early rx frame is missed.
        self._transport.set_frame_handler(self._on_frame)
        await self._transport.start()
        self._tasks.append(asyncio.create_task(self._pump(), name="cambium-uplink-pump"))
        self._tasks.append(asyncio.create_task(self.scheduler.run(), name="cambium-tx-scheduler"))
        if serve_http:
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            await web.TCPSite(self._runner, self._config.host, self._config.port).start()

    async def stop(self) -> None:
        # Web surface first: its cleanup (mapping.off -> identify cancel)
        # still needs a live transport to release any held fixture.
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.scheduler.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._transport.set_frame_handler(None)
        await self._transport.stop()

    # ------------------------------------------------------------------
    # Uplink: transport frames -> typed events -> fleet state / caches
    # ------------------------------------------------------------------

    def _on_frame(self, ftype: int, payload: bytes) -> None:
        self._rx_q.put_nowait((ftype, payload))

    async def _pump(self) -> None:
        while True:
            ftype, payload = await self._rx_q.get()
            event = self.uplink.feed(ftype, payload)
            if event is None:
                continue  # counted by the parser's drop counters
            if isinstance(event, BridgeStatusEvent):
                self._bridge = event
                # On air, the bridge is who we are: adopt its short id so
                # fixtures see one consistent sender identity.
                self.stamper.set_src_id(event.status.mac[3:])
            elif isinstance(event, BridgeLog):
                log.info("bridge: %s", event.text)
            else:
                await self.fleet.update(event)

    # ------------------------------------------------------------------
    # Downlink arbitration (the one cross-module decision this root owns)
    # ------------------------------------------------------------------

    def _packetize(self, frame: FixtureFrame) -> list[bytes]:
        # Checked at emit time, not just at submit time: the latest-wins
        # mailbox may still hold a frame accepted before mapping engaged or
        # drive was switched off, and retransmitting it would race the
        # camera / fight the operator for up to stale_input_s.
        if not self._drive_on or self.mapping.active:
            return []
        return frame_to_packets(frame, self._roster, self.stamper)

    # ------------------------------------------------------------------
    # DaemonServices implementation (thin bridges to normalize/wire/scheduler)
    # ------------------------------------------------------------------

    async def _submit_sim_frame(self, fixtures: list[dict], seq: int) -> list[str]:
        frame, unknown = sim_to_canonical(
            fixtures, self._roster, self._config.white_extract, seq
        )
        # The WS layer already drops frames while mapping is active; this
        # re-check keeps the invariant local so no future caller can bypass it.
        if self._drive_on and not self.mapping.active:
            self.scheduler.set_frame(frame)
        return unknown

    async def _set_drive(self, on: bool) -> None:
        self._drive_on = on

    async def _send_show(self, phase: int, hue: int, flags: int) -> None:
        await self.scheduler.send_oneshot(
            show_frame(self.stamper.stamp(), phase, hue, flags)
        )

    async def _send_identify(
        self, mac: str | None, secs: int, color: int, blink: bool
    ) -> None:
        await self.scheduler.send_oneshot(
            identify(self.stamper.stamp(), self._target(mac), secs, color, 1 if blink else 0)
        )

    async def _send_night(self, mode: int, mac: str | None) -> None:
        await self.scheduler.send_oneshot(
            force_lifecycle(self.stamper.stamp(), self._target(mac), mode)
        )

    async def _send_program(
        self,
        mac: str | None,
        program_id: int,
        lease_s: int,
        seed: int,
        hard_cut: bool,
        params,
    ) -> None:
        # NbProgramSet.flags bit0 = hard-cut / no crossfade (packet.h).
        await self.scheduler.send_oneshot(
            program_set(
                self.stamper.stamp(),
                self._target(mac),
                program_id,
                lease_s,
                seed,
                0x01 if hard_cut else 0x00,
                self._params_bytes(params),
            )
        )

    async def _send_set_rate(self, hz: int) -> None:
        await self.scheduler.send_oneshot(set_rate(self.stamper.stamp(), hz))

    async def _send_solid_debug(
        self, id_or_mac: str, r: int, g: int, b: int, w: int
    ) -> None:
        fixture = self._resolve_fixture(id_or_mac)
        if not fixture.cls.is_rgbw:
            w = 0  # GRB hex has no W emitter -- keep the PERIMETER w=0 wire invariant
        await self.scheduler.send_oneshot(
            direct_frame(
                self.stamper.stamp(),
                [(short_id_from_str(fixture.mac), r, g, b, w)],
                FLAG_MICRO_LEASE,
            )
        )

    async def _bridge_status(self) -> dict:
        # Contract (api/services.py): never raise -- a dead bridge is
        # connected=False, not an exception, or the 1 Hz status ticker dies.
        stats = self._transport.stats
        out: dict = {
            "connected": stats.connected,
            "frames_tx": stats.frames_tx,
            "frames_rx": stats.frames_rx,
            "crc_errors": stats.crc_errors,
        }
        if self._bridge is not None:
            s = self._bridge.status
            out.update(
                {
                    "bridge_mac": s.mac.hex().upper(),
                    "channel": s.channel,
                    "uptime_ms": s.uptime_ms,
                    "tx_ok": s.tx_ok,
                    "tx_fail": s.tx_fail,
                    "rx_pkts": s.rx_pkts,
                    "rx_drop": s.rx_drop,
                    "bridge_crc_err": s.crc_err,
                    "fw": s.fw,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Small shared lookups
    # ------------------------------------------------------------------

    @staticmethod
    def _target(mac: str | None) -> bytes:
        return BROADCAST_ID if mac is None else short_id_from_str(mac)

    def _resolve_fixture(self, id_or_mac: str) -> Fixture:
        """Same forgiving join as normalize: fixture_id first, then short mac."""
        fixture = self._roster.by_id.get(id_or_mac)
        if fixture is None:
            m = id_or_mac.strip().upper()
            if len(m) == 6 and all(c in _HEX for c in m):
                fixture = self._roster.by_mac.get(m)
        if fixture is None:
            raise LookupError(
                f"no fixture {id_or_mac!r} in the active roster "
                f"({self._config.roster}); use a fixture id like 'B003' or a "
                f"short mac like 'F2BDB4' from the roster CSV"
            )
        return fixture

    @staticmethod
    def _params_bytes(params) -> bytes:
        """JSON-shaped program params -> the wire's byte blob (max 8)."""
        if params is None:
            return b""
        if isinstance(params, (bytes, bytearray)):
            return bytes(params)
        try:
            return bytes(params)
        except (TypeError, ValueError):
            raise ValueError(
                f"program params must be a list of 0..255 ints (max 8), got "
                f"{params!r}; e.g. \"params\": [1, 2, 3]"
            ) from None
