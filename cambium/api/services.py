"""The seam between the API surface and the daemon.

The api modules are deliberately THIN: they validate/encode JSON and call one
of these injected callables. DaemonServices is constructed by the daemon
composition root (phase Integrate) and handed to build_app(); tests hand in
recording fakes. Fields are typed loosely on purpose -- the api must not
import daemon internals, and duck-typed fields keep the dependency arrow
pointing one way (daemon -> api, never back).

Expected shapes (every submit_*/set_*/send_*/bridge_status field is an async
callable):

    roster                 cambium.roster.Roster (the active roster)
    fleet                  object with snapshot() -> dict,
                           add_listener(cb: (kind: str, payload: dict) -> None),
                           charging_count() -> int
    submit_sim_frame(fixtures: list[dict], seq: int) -> list[str]
                           queue one sim frame; returns the unknown fixture ids
    set_drive(on: bool)    start/stop driving the real fleet
    send_show(phase: int, hue: int, flags: int)
    send_identify(mac: str | None, secs: int, color: int, blink: bool)
                           mac None = all fixtures; secs 0 cancels
    send_night(mode: int, mac: str | None)
                           0=force day, 1=force night, 2=auto
    send_program(mac: str | None, program_id: int, lease_s: int,
                 seed: int, hard_cut: bool, params)
                           params is passed through opaquely; the downlink
                           encoder validates it
    send_set_rate(hz: int)
    send_solid_debug(id_or_mac: str, r: int, g: int, b: int, w: int)
                           raises LookupError (message contains the fix) when
                           the fixture is unknown
    bridge_status() -> dict with at least {"connected": bool}; must not raise
                           (report a dead bridge as connected=False, not as an
                           exception, or the 1 Hz status ticker dies)
"""

from dataclasses import dataclass
from typing import Any

from cambium.roster import Roster


@dataclass
class DaemonServices:
    roster: Roster
    fleet: Any
    submit_sim_frame: Any
    set_drive: Any
    send_show: Any
    send_identify: Any
    send_night: Any
    send_program: Any
    send_set_rate: Any
    send_solid_debug: Any
    bridge_status: Any
