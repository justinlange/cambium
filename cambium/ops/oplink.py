"""OpsLink: how the ops CLI (doctor/blink/night/identify) reaches the fleet.

Two flavors, one shape:

- SerialOpsLink opens the bridge board's USB port DIRECTLY -- for bench work
  before the daemon is running. The port is exclusive: if `cambium serve` is
  up, this will fail to open (or starve it); use --daemon instead.
- HttpOpsLink talks to a RUNNING daemon's HTTP surface (/bridge, /fleet,
  /debug/*) -- the daemon keeps owning the port. This is also how the ops
  commands reach the fake fleet (`--daemon http://localhost:8600`).
"""

from __future__ import annotations

import asyncio
import time

import aiohttp

from cambium.downlink.packetize import HeaderStamper
from cambium.transport.serial_cobs import SerialCobsTransport
from cambium.uplink.parse import BridgeStatusEvent, PeerPacket, UplinkParser
from cambium.wire.packets import (
    BROADCAST_ID,
    force_lifecycle,
    identify,
    short_id_from_str,
)


class SerialOpsLink:
    def __init__(self, port: str) -> None:
        self._transport = SerialCobsTransport(port, log=lambda *_: None)
        self._parser = UplinkParser()
        self._stamper = HeaderStamper()
        self._bridge: dict | None = None
        self._census: dict[str, dict] = {}
        self._events = asyncio.Queue()

    async def open(self) -> None:
        self._transport.set_frame_handler(self._on_frame)
        await self._transport.start()

    async def close(self) -> None:
        await self._transport.stop()

    def _on_frame(self, ftype: int, payload: bytes) -> None:
        event = self._parser.feed(ftype, payload)
        if isinstance(event, BridgeStatusEvent):
            st = event.status
            self._stamper.set_src_id(st.mac[3:])
            self._bridge = {
                "connected": True,
                "mac": st.mac.hex().upper(),
                "channel": st.channel,
                "fw": st.fw,
                "tx_ok": st.tx_ok,
                "tx_fail": st.tx_fail,
            }
        elif isinstance(event, PeerPacket):
            hb = event.packet
            entry = self._census.setdefault(event.mac_short, {})
            entry.update(
                {
                    "batt_mv": getattr(hb, "batt_mv", None),
                    "batt_ma": getattr(hb, "batt_ma", None),
                    "soc_pct": getattr(hb, "soc_pct", None),
                    "rssi": event.rssi,
                }
            )
            if getattr(hb, "fw_rev", None):
                entry["fw"] = hb.fw_rev
            if getattr(hb, "life_state", None) is not None:
                entry["life_state"] = hb.life_state

    async def bridge_status(self, timeout_s: float = 3.0) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._bridge is not None:
                return self._bridge
            await asyncio.sleep(0.05)
        return None

    async def census(self, listen_s: float = 10.0) -> dict[str, dict]:
        await asyncio.sleep(listen_s)
        return dict(self._census)

    async def send_identify(
        self, mac: str | None, secs: int, color: int, blink: bool
    ) -> None:
        target = short_id_from_str(mac) if mac else BROADCAST_ID
        await self._transport.send_packet(
            identify(self._stamper.stamp(), target, secs, color, int(blink))
        )

    async def send_night(self, mode: int, mac: str | None) -> None:
        target = short_id_from_str(mac) if mac else BROADCAST_ID
        await self._transport.send_packet(
            force_lifecycle(self._stamper.stamp(), target, mode)
        )


class HttpOpsLink:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                f"{self._base}/healthz", timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                resp.raise_for_status()
        except aiohttp.ClientError as e:
            await self._session.close()
            raise ConnectionError(
                f"no cambium daemon at {self._base} ({e}); start one with "
                f"`cambium serve` / `cambium fakefleet run`, or use --port "
                f"to open the bridge board's serial directly"
            ) from None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _get(self, path: str) -> dict:
        async with self._session.get(f"{self._base}{path}") as resp:
            body = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"{path}: {body.get('error', resp.status)}")
            return body

    async def bridge_status(self, timeout_s: float = 3.0) -> dict | None:
        status = await self._get("/bridge")
        return status if status.get("connected") else None

    async def census(self, listen_s: float = 10.0) -> dict[str, dict]:
        # The daemon has been listening since it started; a short settle is
        # enough for slow 1 Hz heartbeats to land.
        await asyncio.sleep(min(listen_s, 3.0))
        fleet = await self._get("/fleet")
        out: dict[str, dict] = {}
        for mac, entry in fleet.items():
            t = entry.get("telemetry") or {}
            out[mac] = {
                "batt_mv": t.get("batt_mv"),
                "batt_ma": t.get("batt_ma"),
                "soc_pct": t.get("soc_pct"),
                "rssi": t.get("dl_rssi"),
                "life_state": t.get("life_state"),
                "online": entry.get("online"),
            }
        return out

    async def send_identify(
        self, mac: str | None, secs: int, color: int, blink: bool
    ) -> None:
        q = f"secs={secs}&color={color}&blink={int(blink)}"
        if mac:
            q += f"&mac={mac}"
        await self._get(f"/debug/identify?{q}")

    async def send_night(self, mode: int, mac: str | None) -> None:
        q = f"mode={mode}" + (f"&mac={mac}" if mac else "")
        await self._get(f"/debug/night?{q}")
