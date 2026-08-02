"""The ops commands behind `cambium doctor/blink/night/identify`.

Each returns (exit_code, lines) so the CLI layer stays a thin printer and
tests can assert on content. Every failure line contains its fix -- the
doctor is the universal "it's broken, now what" entry point and it must
never leave the operator without a next command.
"""

from __future__ import annotations

import asyncio

from cambium.roster import Roster

WHITE = 5


async def doctor(
    link, roster: Roster, *, expect_channel: int = 11, listen_s: float = 10.0
) -> tuple[int, list[str]]:
    lines: list[str] = []

    # Stage 1+2: link open + bridge status. (Serial open errors and HTTP
    # connection errors raise with their own fix text before we get here.)
    status = await link.bridge_status()
    if status is None:
        lines.append(
            "FAIL bridge: no STATUS heard -- wrong serial device, or the "
            "board is not running cambium_bridge (flash it: "
            "firmware/cambium_bridge/build.sh --port <dev>)"
        )
        return 1, lines
    lines.append(
        f"ok   bridge: fw={status.get('fw')} mac={status.get('mac')} "
        f"channel={status.get('channel')}"
    )

    # Stage 3: channel discipline -- the classic silent killer.
    ch = status.get("channel")
    if ch != expect_channel:
        lines.append(
            f"FAIL channel: bridge is on {ch}, the fleet is commissioned on "
            f"{expect_channel} -- it hears NOTHING; rebuild with --channel "
            f"{expect_channel} or send CTRL SET_CHANNEL"
        )
        return 1, lines
    lines.append(f"ok   channel: {ch}")

    # Stage 4: heartbeat census vs the roster.
    lines.append(f"...  listening {listen_s:.0f}s for heartbeats")
    census = await link.census(listen_s)
    for mac in sorted(census):
        c = census[mac]
        fixture = roster.by_mac.get(mac)
        fid = fixture.fixture_id if fixture else "NOT IN ROSTER"
        lines.append(
            f"     {mac} {fid or '-'}: batt={c.get('batt_mv')}mV "
            f"soc={c.get('soc_pct')}% rssi={c.get('rssi')} "
            f"life={_life_str(c.get('life_state'))}"
        )
    missing = sorted(set(roster.by_mac) - set(census))
    if not census:
        lines.append(
            "FAIL fleet: zero heartbeats -- power a lantern near the bridge "
            "and re-run; if it stays silent, check the channel above and the "
            "lantern's battery"
        )
        return 1, lines
    lines.append(f"ok   fleet: {len(census)} heard, {len(missing)} missing")
    if missing:
        lines.append(
            f"warn missing from roster: {', '.join(missing)} -- battery dead, "
            f"out of range, or the roster lists a lantern not on the bench"
        )

    # Stage 5: the night gate (warn-only: mapping works in daylight).
    day = [m for m, c in census.items() if c.get("life_state") in (0, None)]
    if day:
        lines.append(
            f"warn night gate: {len(day)}/{len(census)} fixtures are in DAY "
            f"lifecycle -- they will IGNORE show frames. Run `cambium night "
            f"on`; on stock firmware (no NB_FORCE_LIFECYCLE yet) type N1 "
            f"over each lantern's own USB serial instead"
        )
    else:
        lines.append("ok   night gate: fleet is in NIGHT, shows will render")

    lines.append("READY")
    return 0, lines


def _life_str(v) -> str:
    return {0: "day", 1: "night"}.get(v, "?")


async def blink(
    link,
    roster: Roster,
    *,
    mac: str | None = None,
    color: int = WHITE,
    secs: int = 1,
    delay_s: float = 1.2,
    broadcast: bool = False,
    sleep=asyncio.sleep,
) -> tuple[int, list[str]]:
    """Roll-call: flash each roster fixture in index order (mac ascending) --
    the same order a Constellate sweep uses, proven before any camera."""
    lines: list[str] = []
    if broadcast:
        await link.send_identify(None, secs, color, False)
        lines.append(f"broadcast identify: every fixture flashes for {secs}s")
        return 0, lines
    ordered = sorted(roster.fixtures, key=lambda f: f.mac)
    if mac:
        ordered = [f for f in ordered if f.mac == mac.upper() or f.fixture_id == mac]
        if not ordered:
            lines.append(
                f"FAIL {mac!r} is not in the roster (macs are 6 hex digits "
                f"like F2BDB4, ids like B003) -- see the roster CSV"
            )
            return 1, lines
    lines.append("index  fixture  mac     -- call out any mismatch, then STOP")
    for i, f in enumerate(ordered):
        lines.append(f"{i:5d}  {f.fixture_id or '-':7s}  {f.mac}")
        await link.send_identify(f.mac, secs, color, False)
        await sleep(delay_s)
        await link.send_identify(f.mac, 0, 0, False)  # cancel before the next
    lines.append(f"done: {len(ordered)} fixtures flashed in sweep order")
    return 0, lines


async def night(link, mode_word: str, *, mac: str | None = None) -> tuple[int, list[str]]:
    modes = {"on": 1, "off": 0, "auto": 2}
    if mode_word not in modes:
        return 2, [f"night takes on|off|auto, got {mode_word!r}"]
    await link.send_night(modes[mode_word], mac)
    return 0, [
        f"sent force-lifecycle {mode_word}" + (f" to {mac}" if mac else " (all)"),
        "note: stock firmware (without the cambium-direct-frames branch) "
        "ignores this packet -- there the only override is typing N1/N0/N2 "
        "over each lantern's own USB serial",
    ]
