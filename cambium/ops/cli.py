"""`cambium doctor/blink/night/identify` -- ops CLI over an OpsLink.

Link selection: --daemon URL talks to a running daemon (which owns the
serial port; also how these commands reach the fake fleet); --port opens
the bridge board's serial directly (daemon must NOT be running). Exactly
one is required, and the error says so with both examples.
"""

from __future__ import annotations

import asyncio
import sys

from cambium.config import CambiumConfig
from cambium.roster import Roster

from . import commands
from .oplink import HttpOpsLink, SerialOpsLink


def add_ops_parsers(sub) -> None:
    def link_args(p) -> None:
        p.add_argument("--daemon", metavar="URL",
                       help="running daemon, e.g. http://localhost:8600")
        p.add_argument("--port", metavar="DEV",
                       help="bridge board serial device (daemon must not be running)")
        p.add_argument("--config", default="config/cambium.toml")

    d = sub.add_parser("doctor", help="check bridge + fleet health end to end")
    link_args(d)
    d.add_argument("--listen", type=float, default=10.0,
                   help="heartbeat census seconds (default: %(default)s)")

    b = sub.add_parser("blink", help="roll-call: flash each roster fixture in sweep order")
    link_args(b)
    b.add_argument("--fixture", help="one fixture only (mac like F2BDB4 or id like B003)")
    b.add_argument("--color", type=int, default=commands.WHITE,
                   help="1=R 2=G 3=B 4=Y 5=W (default white)")
    b.add_argument("--secs", type=int, default=1)
    b.add_argument("--delay-ms", type=int, default=1200)
    b.add_argument("--broadcast", action="store_true",
                   help="flash every fixture at once (gross liveness check)")

    n = sub.add_parser("night", help="force night/day lifecycle (the night gate is real)")
    link_args(n)
    n.add_argument("mode", choices=("on", "off", "auto"))
    n.add_argument("--mac", help="one fixture (default: broadcast to all)")

    i = sub.add_parser("identify", help="hold an identify color on one fixture")
    link_args(i)
    i.add_argument("id_or_mac")
    i.add_argument("--secs", type=int, default=10)
    i.add_argument("--color", type=int, default=commands.WHITE)
    i.add_argument("--blink", action="store_true")


def run_ops(args) -> int:
    if bool(args.daemon) == bool(args.port):
        print(
            "cambium: pass exactly one of --daemon URL (a running daemon, "
            "e.g. --daemon http://localhost:8600) or --port DEV (the bridge "
            "board's serial, e.g. --port /dev/tty.usbmodem101, with no "
            "daemon running -- the port is exclusive)",
            file=sys.stderr,
        )
        return 2
    try:
        config = CambiumConfig.load(args.config)
        roster = Roster.load(config.roster)
    except (FileNotFoundError, ValueError) as e:
        print(f"cambium: {e}", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, config, roster))


async def _run(args, config: CambiumConfig, roster: Roster) -> int:
    link = HttpOpsLink(args.daemon) if args.daemon else SerialOpsLink(args.port)
    try:
        await link.open()
    except (ConnectionError, OSError) as e:
        print(f"cambium: {e}", file=sys.stderr)
        return 1
    try:
        if args.command == "doctor":
            code, lines = await commands.doctor(
                link, roster, expect_channel=config.channel, listen_s=args.listen
            )
        elif args.command == "blink":
            code, lines = await commands.blink(
                link, roster, mac=args.fixture, color=args.color,
                secs=args.secs, delay_s=args.delay_ms / 1000.0,
                broadcast=args.broadcast,
            )
        elif args.command == "night":
            code, lines = await commands.night(link, args.mode, mac=args.mac)
        elif args.command == "identify":
            fixture = roster.by_id.get(args.id_or_mac)
            mac = fixture.mac if fixture else args.id_or_mac.upper()
            await link.send_identify(mac, args.secs, args.color, args.blink)
            code, lines = 0, [
                f"identify {mac}: color={args.color} secs={args.secs}"
                + (" blink" if args.blink else "")
            ]
        else:
            return 2
    finally:
        await link.close()
    for line in lines:
        print(line)
    return code
