"""FleetState: cambium's memory of what the fleet last said.

Consumes typed UplinkEvents (never raw bytes -- that boundary is
uplink/parse.py) and keeps per-MAC telemetry + choreo state, ready to serve
as JSON on GET /fleet and to push to WS listeners. It is a passive mirror:
it never transmits, so it can never fight the fixtures' own silence ladder.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from cambium.model import TelemetryUpdate
from cambium.roster import Roster
from cambium.wire.packets import ChoreoState, Heartbeat

from .parse import PeerPacket, UplinkEvent

# Sync return (None) is allowed alongside async: the api layer registers the
# WsHub's sync broadcast_soon as a listener, and awaiting its None return
# would otherwise count a phantom listener_error on every single emit.
Listener = Callable[[str, dict], "Awaitable[None] | None"]

# Charging-count pushes are capped at 1/s: on a bench of solar sims the count
# can flap every heartbeat, and Elliot's UI only needs the trend.
_CHARGING_MIN_INTERVAL_S = 1.0


class FleetState:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        roster: Roster | None = None,
        offline_after_s: float = 30.0,
    ) -> None:
        self._clock = clock
        self._roster = roster
        self.offline_after_s = offline_after_s
        self.telemetry: dict[str, TelemetryUpdate] = {}
        self._choreo: dict[str, dict] = {}  # mac -> program/generation/state/intensity
        self._last_seen: dict[str, float] = {}  # any peer packet counts as "seen"
        self._listeners: list[Listener] = []
        self.listener_errors = 0
        # Charging edge detector: compare against the last count we EMITTED
        # (not the last computed), so a change suppressed by the rate limit
        # still gets flushed by the next heartbeat after the window.
        self._charging_emitted_count = 0
        self._charging_emit_t = float("-inf")

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    async def update(self, event: UplinkEvent) -> None:
        """Fold one uplink event into fleet state, notifying listeners."""
        if not isinstance(event, PeerPacket):
            return  # bridge status/log are transport-health concerns, not fleet state
        now = self._clock()
        self._last_seen[event.mac_short] = now
        pkt = event.packet
        if isinstance(pkt, Heartbeat):
            await self._apply_heartbeat(event.mac_short, event.rssi, pkt, now)
        elif isinstance(pkt, ChoreoState):
            await self._apply_choreo(event.mac_short, pkt)

    async def _apply_heartbeat(
        self, mac: str, rssi: int, hb: Heartbeat, now: float
    ) -> None:
        prev = self.telemetry.get(mac)

        def keep(new: int | None, old: int | None) -> int | None:
            # hb-short omits the tail-13 fields; keep the last full-heartbeat
            # truth instead of flapping back to None every short beat.
            return new if new is not None else old

        t = TelemetryUpdate(
            mac=mac,
            batt_mv=hb.batt_mv,
            batt_ma=hb.batt_ma,  # signed: negative = discharging
            soc_pct=hb.soc_pct,
            dl_rssi=hb.dl_rssi,
            mode=hb.mode,
            last_seen=now,
            life_state=keep(hb.life_state, prev.life_state if prev else None),
            program=keep(hb.active_program, prev.program if prev else None),
            power_tier=keep(hb.power_tier, prev.power_tier if prev else None),
        )
        self.telemetry[mac] = t
        await self._emit("hb", {"rssi": rssi, **asdict(t)})

        count = self.charging_count()
        if (
            count != self._charging_emitted_count
            and now - self._charging_emit_t >= _CHARGING_MIN_INTERVAL_S
        ):
            self._charging_emitted_count = count
            self._charging_emit_t = now
            await self._emit("charging", {"count": count, "macs": self.charging_macs()})

    async def _apply_choreo(self, mac: str, cs: ChoreoState) -> None:
        prev = self._choreo.get(mac)
        self._choreo[mac] = {
            "program": cs.program_id,
            "generation": cs.generation,
            "state": cs.state,
            "intensity": cs.intensity,
        }
        # Edge-triggered on the state field only; first sighting counts as an
        # edge (unknown -> state) so the browser learns about a node it has
        # never heard choreo from.
        if prev is None or prev["state"] != cs.state:
            await self._emit(
                "evt",
                {
                    "mac": mac,
                    "state": cs.state,
                    "prev_state": prev["state"] if prev else None,
                    "program": cs.program_id,
                    "generation": cs.generation,
                    "intensity": cs.intensity,
                },
            )

    async def _emit(self, kind: str, payload: dict) -> None:
        for fn in list(self._listeners):
            try:
                res = fn(kind, payload)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                # One broken listener (a half-closed WS, say) must never sever
                # telemetry for the others; count it so it is still visible.
                self.listener_errors += 1

    def charging_macs(self) -> list[str]:
        """Elliot's solarPanelsCharging spec: nodes-with-battMa>0.

        Positive batt_ma = current flowing INTO the battery. Sorted for a
        stable JSON diff on the browser side.
        """
        return sorted(m for m, t in self.telemetry.items() if t.batt_ma > 0)

    def charging_count(self) -> int:
        return len(self.charging_macs())

    def snapshot(self) -> dict:
        """JSON-ready per-mac state for GET /fleet.

        The fixture_id key appears only when a Roster is attached, so the
        API layer can tell "no roster loaded" apart from "mac not in roster"
        (which shows as fixture_id: null).
        """
        now = self._clock()
        out: dict[str, dict] = {}
        for mac in sorted(self._last_seen):
            last_seen = self._last_seen[mac]
            t = self.telemetry.get(mac)
            entry: dict = {
                "online": (now - last_seen) <= self.offline_after_s,
                "last_seen": last_seen,
                "telemetry": asdict(t) if t is not None else None,
                "choreo": dict(self._choreo[mac]) if mac in self._choreo else None,
            }
            if self._roster is not None:
                fixture = self._roster.by_mac.get(mac)
                entry["fixture_id"] = fixture.fixture_id if fixture else None
            out[mac] = entry
        return out
