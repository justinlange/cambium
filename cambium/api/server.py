"""The ONE aiohttp app: HTTP endpoints + /ws, served on config.port (8600).

Handlers are thin: parse/validate the request, call one services callable
(or MappingMode), encode the reply. State lives behind DaemonServices;
mapping arbitration lives in MappingMode; the WS vocabulary lives in
ws_protocol. Nothing here touches wire bytes.
"""

from __future__ import annotations

import asyncio
from importlib import metadata

from aiohttp import WSMsgType, web

from cambium.api.constellate import MappingMode
from cambium.api.ws_protocol import WsHub, handle_down, send_err


# Typed app keys (aiohttp's recommended alternative to string keys); the
# daemon and tests reach the built pieces through these.
HUB_KEY = web.AppKey("hub", WsHub)
MAPPING_KEY = web.AppKey("mapping", MappingMode)
SERVICES_KEY = web.AppKey("services", object)
CONFIG_KEY = web.AppKey("config", object)


def _version() -> str:
    try:
        return metadata.version("cambium")
    except metadata.PackageNotFoundError:
        return "0+unknown"  # running from a checkout without an install


def _err(status: int, msg: str) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def _u8_query(request: web.Request, name: str) -> int:
    raw = request.query.get(name, "0")
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(
            f"query param {name}={raw!r} is not an integer; use {name}=0..255"
        ) from None
    if not 0 <= v <= 255:
        raise ValueError(
            f"query param {name}={v} is out of range; use {name}=0..255"
        )
    return v


def build_app(
    services,
    config,
    *,
    mapping: MappingMode | None = None,
    bridge_status_interval_s: float = 1.0,
) -> web.Application:
    """Assemble the HTTP+WS surface. mapping/interval injectable for tests."""
    app = web.Application()
    hub = WsHub()
    if mapping is None:
        mapping = MappingMode(services)
    app[HUB_KEY] = hub
    app[MAPPING_KEY] = mapping
    app[SERVICES_KEY] = services
    app[CONFIG_KEY] = config

    # Fleet events ("hb", "evt", "charging", ...) fan out to every socket.
    services.fleet.add_listener(hub.broadcast_soon)

    async def healthz(request: web.Request) -> web.Response:
        status = await services.bridge_status()
        return web.json_response(
            {
                "ok": True,
                "version": _version(),
                "transport_connected": bool(status.get("connected", False)),
            }
        )

    async def fleet(request: web.Request) -> web.Response:
        return web.json_response(services.fleet.snapshot())

    async def constellate_light(request: web.Request) -> web.Response:
        raw = request.query.get("led")
        try:
            led = int(raw)
        except (TypeError, ValueError):
            return _err(
                400,
                f"query param led must be an integer index, got {raw!r}; "
                f"e.g. /constellate/light?led=3",
            )
        try:
            mac = await mapping.light(led)
        except LookupError as e:
            return _err(404, str(e))
        # Engaging mapping needs no 409 arbitration with a live sim stream:
        # mapping silently preempts sim frames (see MappingMode docstring).
        return web.json_response(
            {
                "ok": True,
                "led": led,
                "mac": mac,
                "sim_frames_dropped": mapping.sim_frames_dropped,
            }
        )

    async def constellate_off(request: web.Request) -> web.Response:
        await mapping.off()
        return web.json_response(
            {"ok": True, "sim_frames_dropped": mapping.sim_frames_dropped}
        )

    async def debug_solid(request: web.Request) -> web.Response:
        fid = request.query.get("id")
        if not fid:
            return _err(
                400,
                "query param id is required: a fixture id like 'B003' or a "
                "short mac like 'F2BDB4'; e.g. /debug/solid?id=B003&r=255&g=64&b=0&w=0",
            )
        try:
            r = _u8_query(request, "r")
            g = _u8_query(request, "g")
            b = _u8_query(request, "b")
            w = _u8_query(request, "w")
        except ValueError as e:
            return _err(400, str(e))
        try:
            await services.send_solid_debug(fid, r, g, b, w)
        except LookupError as e:
            return _err(404, str(e))
        return web.json_response({"ok": True, "id": fid, "rgbw": [r, g, b, w]})

    async def bridge(request: web.Request) -> web.Response:
        # The ops CLI's --daemon mode reads this instead of opening the
        # serial port (the daemon owns it exclusively).
        return web.json_response(await services.bridge_status())

    async def debug_night(request: web.Request) -> web.Response:
        raw = request.query.get("mode")
        if raw not in ("0", "1", "2"):
            return _err(
                400,
                f"query param mode must be 0 (force day), 1 (force night) or "
                f"2 (auto), got {raw!r}; e.g. /debug/night?mode=1",
            )
        mac = request.query.get("mac") or None
        await services.send_night(int(raw), mac)
        return web.json_response({"ok": True, "mode": int(raw), "mac": mac})

    async def debug_identify(request: web.Request) -> web.Response:
        mac = request.query.get("mac") or None  # absent = broadcast
        try:
            secs = _u8_query(request, "secs")
            color = _u8_query(request, "color")
            blink = int(request.query.get("blink", "0")) != 0
        except ValueError as e:
            return _err(400, str(e))
        await services.send_identify(mac, secs, color, blink)
        return web.json_response(
            {"ok": True, "mac": mac, "secs": secs, "color": color}
        )

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hub.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await handle_down(ws, msg.data, services, mapping)
                elif msg.type == WSMsgType.BINARY:
                    await send_err(
                        ws,
                        "binary frames are not supported; send one JSON "
                        "object per text frame",
                    )
        finally:
            hub.discard(ws)
        return ws

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/fleet", fleet)
    app.router.add_get("/bridge", bridge)
    app.router.add_get("/constellate/light", constellate_light)
    app.router.add_get("/constellate/off", constellate_off)
    app.router.add_get("/debug/solid", debug_solid)
    app.router.add_get("/debug/night", debug_night)
    app.router.add_get("/debug/identify", debug_identify)
    app.router.add_get("/ws", ws_handler)

    async def _lifecycle(app: web.Application):
        async def tick():
            # The 1 Hz push doubles as the on-change signal: status is polled
            # at the same cadence, so a change can never beat the next tick.
            while True:
                await hub.broadcast("bridge_status", await services.bridge_status())
                await asyncio.sleep(bridge_status_interval_s)

        task = asyncio.create_task(tick())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Never leave a fixture pinned white past our own shutdown.
        await mapping.off()

    app.cleanup_ctx.append(_lifecycle)
    return app
