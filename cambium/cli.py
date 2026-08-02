"""The `cambium` console script.

`cambium serve` runs the daemon today; the mapping/ops subcommands are
declared now (so the help text shows the intended shape) but land in phase
W5 -- invoking one exits 2 with a pointer to the README status table.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from cambium.config import CambiumConfig
from cambium.daemon import Daemon
from cambium.roster import Roster
from cambium.transport.loopback import LoopbackTransport
from cambium.transport.serial_cobs import SerialCobsTransport

# Subcommand -> one-line help; all of these are phase W5 (mapping pipeline +
# ops tooling). Declared here so `cambium --help` already shows the shape.
_PLANNED = {
    "doctor": "check bridge + fleet health end to end",
    "blink": "blink one fixture for physical identification",
    "night": "force night/day lifecycle override (the night gate is real)",
    "sweep": "drive a Constellate mapping sweep",
    "map": "build fixtures-map.json from sweep results",
    "fakefleet": "run the in-process fake fleet (no hardware)",
    "identify": "hold an identify pattern on a fixture",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium",
        description="Middleware between the lighting simulator and the "
        "ESP-NOW lantern fleet.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_serve = sub.add_parser(
        "serve", help="run the daemon (WS/HTTP api + radio downlink)"
    )
    p_serve.add_argument(
        "--config",
        default="config/cambium.toml",
        help="path to cambium.toml (default: %(default)s)",
    )
    p_serve.add_argument(
        "--transport",
        choices=("serial", "loopback"),
        default="serial",
        help="serial = real bridge board; loopback = in-process, no hardware "
        "(default: %(default)s)",
    )
    p_serve.add_argument(
        "--port",
        help="serial device of the bridge board (e.g. /dev/tty.usbmodem101); "
        "required with --transport serial",
    )

    for name, help_text in _PLANNED.items():
        p = sub.add_parser(name, help=f"{help_text} (coming in phase W5)")
        # Swallow any arguments so a W5-shaped invocation reaches our
        # "coming soon" message instead of an argparse usage error.
        p.add_argument("argv", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    return parser


def _serve(args: argparse.Namespace) -> int:
    if args.transport == "serial" and not args.port:
        print(
            "cambium serve: --port is required with --transport serial "
            "(the bridge board's USB device, e.g. /dev/tty.usbmodem101); "
            "use --transport loopback to run without hardware",
            file=sys.stderr,
        )
        return 2
    try:
        config = CambiumConfig.load(args.config)
    except FileNotFoundError:
        print(
            f"cambium serve: config file {args.config!r} not found; pass "
            f"--config path/to/cambium.toml (see config/cambium.toml in the "
            f"repo) or run from the repo root",
            file=sys.stderr,
        )
        return 2
    except ValueError as e:  # unknown section/key -- message names the fix
        print(f"cambium serve: {e}", file=sys.stderr)
        return 2
    try:
        roster = Roster.load(config.roster)
    except FileNotFoundError:
        print(
            f"cambium serve: roster CSV {config.roster!r} not found; fix "
            f"[paths] roster in {args.config} or run from the repo root",
            file=sys.stderr,
        )
        return 2
    except ValueError as e:  # bad row -- message names file, line, and fix
        print(f"cambium serve: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    if args.transport == "serial":
        transport = SerialCobsTransport(
            args.port, log=logging.getLogger("cambium.serial").info
        )
    else:
        transport = LoopbackTransport()
    asyncio.run(_run(Daemon(config, transport, roster)))
    return 0


async def _run(daemon: Daemon) -> None:
    await daemon.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        # One shutdown path for both signals: release any mapping hold, then
        # go silent -- the fleet's own ladder takes over from here.
        await daemon.stop()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    print(
        f"cambium {args.command}: coming in phase W5 -- see the README "
        f"status table",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
