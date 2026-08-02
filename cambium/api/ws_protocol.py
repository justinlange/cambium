"""Browser <-> cambium WebSocket JSON vocabulary: codec, dispatch, fan-out.

DOWN (browser -> cambium), one JSON object per text frame, discriminated by
"kind":

    frame     {seq, fixtures: [{id, rgb: [r, g, b]}]} -- sim colors are
              linear floats that MAY exceed 1.0; cambium never clamps or
              gammas here (normalize clamps, firmware gammas)
    drive     {on}
    night     {mode: 0|1|2, mac?}
    program   {mac?, programId, leaseS, seed?, hardCut?, params?}
    show      {phase, hue, flags?}
    identify  {mac?, seconds, color?, blink?}
    set_rate  {hbHz? | frameHz?}
    ruleset   accepted + logged only (see _h_ruleset for why)

UP (cambium -> browser), fanned out through WsHub: "hb", "evt", "charging",
"bridge_status", "err". Payload keys merge flat with the kind:
{"kind": "hb", ...payload}.

This module is a codec: each handler validates shape and calls exactly one
injected services callable (cambium.api.services.DaemonServices). No wire
packets, no business logic. A bad message earns a {"kind":"err"} reply --
NEVER a disconnect, because the sim streams at 8 Hz and one typo'd frame
must not drop the whole session.
"""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)


class ProtocolError(Exception):
    """A DOWN message failed validation; str() names the field and the fix."""


# ---------------------------------------------------------------------------
# Field validators. bool is excluded from int fields because JSON true/false
# decode to Python bool, which is an int subclass -- a sim bug sending
# seq=true must be told so, not silently treated as 1.
# ---------------------------------------------------------------------------

def _as_int(v, kind: str, key: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError(
            f"'{kind}'.{key} must be an integer, got {v!r}; send a plain "
            f"number like \"{key}\": 8"
        )
    return v


def _req_int(msg: dict, kind: str, key: str) -> int:
    if key not in msg:
        raise ProtocolError(
            f"'{kind}' is missing '{key}'; add an integer '{key}' field"
        )
    return _as_int(msg[key], kind, key)


def _opt_int(msg: dict, kind: str, key: str, default):
    if msg.get(key) is None:
        return default
    return _as_int(msg[key], kind, key)


def _opt_bool(msg: dict, kind: str, key: str, default: bool) -> bool:
    v = msg.get(key)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ProtocolError(
            f"'{kind}'.{key} must be true or false, got {v!r}"
        )
    return v


def _opt_mac(msg: dict, kind: str) -> str | None:
    mac = msg.get("mac")
    if mac is None or isinstance(mac, str):
        return mac
    raise ProtocolError(
        f"'{kind}'.mac must be a short-mac string like 'F2BDB4', or "
        f"null/omitted to target all fixtures; got {mac!r}"
    )


# ---------------------------------------------------------------------------
# DOWN handlers -- one per kind, signature (msg, services, mapping).
# ---------------------------------------------------------------------------

async def _h_frame(msg, services, mapping):
    seq = _req_int(msg, "frame", "seq")
    fixtures = msg.get("fixtures")
    if not isinstance(fixtures, list):
        raise ProtocolError(
            "'frame'.fixtures must be a list of {\"id\": \"B003\", "
            "\"rgb\": [r, g, b]} objects; got "
            f"{type(fixtures).__name__ if fixtures is not None else 'nothing'}"
        )
    for i, fx in enumerate(fixtures):
        if not isinstance(fx, dict) or not isinstance(fx.get("id"), str):
            raise ProtocolError(
                f"'frame'.fixtures[{i}] needs a string 'id' like 'B003'; "
                f"got {fx!r}"
            )
        rgb = fx.get("rgb")
        if (
            not isinstance(rgb, list)
            or len(rgb) != 3
            or any(isinstance(c, bool) or not isinstance(c, (int, float)) for c in rgb)
        ):
            raise ProtocolError(
                f"'frame'.fixtures[{i}].rgb must be exactly 3 numbers "
                f"(linear floats; values above 1.0 are fine, cambium clamps "
                f"downstream); got {rgb!r}"
            )
    if mapping is not None and mapping.active:
        # Arbitration: mapping preempts sim. A Constellate sweep must never
        # race pattern frames -- a stray sim color during light(n) would be
        # captured by the camera as the swept fixture. Dropped silently (no
        # err reply) because the sim keeps streaming harmlessly; the drop
        # counter is visible on the /constellate endpoints.
        mapping.drop_sim_frame()
        return
    unknown = await services.submit_sim_frame(fixtures, seq)
    if unknown:
        raise ProtocolError(
            f"frame seq {seq} named unknown fixture ids "
            f"{', '.join(unknown)}; add them to the roster CSV or remove "
            f"them from the scene"
        )


async def _h_drive(msg, services, mapping):
    on = msg.get("on")
    if not isinstance(on, bool):
        raise ProtocolError(
            f"'drive' needs boolean 'on', got {on!r}; send "
            f"{{\"kind\": \"drive\", \"on\": true}}"
        )
    await services.set_drive(on)


async def _h_night(msg, services, mapping):
    mode = _req_int(msg, "night", "mode")
    if mode not in (0, 1, 2):
        raise ProtocolError(
            f"'night'.mode={mode} is invalid; use 0=force day, "
            f"1=force night, 2=auto"
        )
    await services.send_night(mode, _opt_mac(msg, "night"))


async def _h_program(msg, services, mapping):
    await services.send_program(
        _opt_mac(msg, "program"),
        _req_int(msg, "program", "programId"),
        _req_int(msg, "program", "leaseS"),
        _opt_int(msg, "program", "seed", 0),
        _opt_bool(msg, "program", "hardCut", False),
        msg.get("params"),  # opaque here; the downlink encoder validates it
    )


async def _h_show(msg, services, mapping):
    await services.send_show(
        _req_int(msg, "show", "phase"),
        _req_int(msg, "show", "hue"),
        _opt_int(msg, "show", "flags", 0),
    )


async def _h_identify(msg, services, mapping):
    await services.send_identify(
        _opt_mac(msg, "identify"),
        _req_int(msg, "identify", "seconds"),
        _opt_int(msg, "identify", "color", 0),
        _opt_bool(msg, "identify", "blink", False),
    )


async def _h_set_rate(msg, services, mapping):
    # The firmware has ONE rate knob: NB_SET_RATE governs heartbeat and frame
    # cadence together (see cambium.wire.packets.set_rate). Both vocabulary
    # keys therefore land on the same command; frameHz wins when both appear.
    frame_hz = _opt_int(msg, "set_rate", "frameHz", None)
    hb_hz = _opt_int(msg, "set_rate", "hbHz", None)
    hz = frame_hz if frame_hz is not None else hb_hz
    if hz is None:
        raise ProtocolError(
            "'set_rate' needs 'frameHz' or 'hbHz' (integer Hz); e.g. "
            "{\"kind\": \"set_rate\", \"frameHz\": 8}"
        )
    await services.send_set_rate(hz)


async def _h_ruleset(msg, services, mapping):
    """Accept and log; never transmitted, never an error.

    The fixture firmware has no ruleset packet type (packet.h assigns types
    1..26 and none carries rules), so there is nothing to put on the wire.
    The sim sends rulesets as part of its normal scene vocabulary; replying
    err would make a healthy connection look broken to the operator, so we
    ack by silence and keep a log line as the paper trail.
    """
    log.info("ruleset accepted but not transmitted (no firmware support): %r", msg)


_HANDLERS = {
    "frame": _h_frame,
    "drive": _h_drive,
    "night": _h_night,
    "program": _h_program,
    "show": _h_show,
    "identify": _h_identify,
    "set_rate": _h_set_rate,
    "ruleset": _h_ruleset,
}


async def send_err(ws, msg: str) -> None:
    """Best-effort err frame; a socket that died mid-reply is not our problem
    (the ws read loop notices the close and drops it)."""
    try:
        await ws.send_str(json.dumps({"kind": "err", "msg": msg}))
    except Exception:
        pass


async def handle_down(ws, raw: str, services, mapping=None) -> None:
    """Decode + dispatch one DOWN text message. Never raises, never closes ws."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        await send_err(
            ws,
            f"message is not valid JSON ({e}); send one JSON object per text "
            f"frame, like {{\"kind\": \"drive\", \"on\": true}}",
        )
        return
    if not isinstance(msg, dict):
        await send_err(
            ws,
            f"message must be a JSON object with a 'kind' field, got "
            f"{type(msg).__name__}; e.g. {{\"kind\": \"drive\", \"on\": true}}",
        )
        return
    kind = msg.get("kind")
    handler = _HANDLERS.get(kind) if isinstance(kind, str) else None
    if handler is None:
        await send_err(
            ws, f"unknown kind {kind!r}; valid: {', '.join(_HANDLERS)}"
        )
        return
    try:
        await handler(msg, services, mapping)
    except ProtocolError as e:
        await send_err(ws, str(e))
    except Exception:
        # A services bug must not kill the sim's session; surface it and
        # keep the socket alive.
        log.exception("handler for kind %r failed", kind)
        await send_err(
            ws,
            f"internal error handling {kind!r}; see the cambium log for the "
            f"traceback",
        )


class WsHub:
    """Fan-out for UP frames to every connected browser socket.

    Sockets that error are dropped, not retried: the browser reconnects on
    its own, and the hub must never let one dead tab stall frames to the
    living ones.
    """

    def __init__(self) -> None:
        self._clients: set = set()
        # broadcast_soon tasks are kept referenced until done so the event
        # loop can't garbage-collect them mid-send.
        self._tasks: set[asyncio.Task] = set()

    def add(self, ws) -> None:
        self._clients.add(ws)

    def discard(self, ws) -> None:
        self._clients.discard(ws)

    async def broadcast(self, kind: str, payload: dict) -> None:
        text = json.dumps({"kind": kind, **payload})
        for ws in list(self._clients):
            if getattr(ws, "closed", False):
                self._clients.discard(ws)
                continue
            try:
                await ws.send_str(text)
            except Exception:
                self._clients.discard(ws)

    def broadcast_soon(self, kind: str, payload: dict) -> None:
        """Sync entry point for callback contexts (e.g. fleet listeners)."""
        task = asyncio.get_running_loop().create_task(self.broadcast(kind, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
