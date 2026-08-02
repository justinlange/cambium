"""The `cambium` console script.

`cambium serve` / `cambium fakefleet run` run the daemon; `cambium sweep` /
`cambium map` drive the spatial-commissioning pipeline; `cambium doctor` /
`blink` / `night` / `identify` are the bench ops tools.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from cambium.config import CambiumConfig
from cambium.daemon import Daemon
from cambium.mapping.cli import add_map_parser, add_sweep_parser, cmd_map, cmd_sweep
from cambium.ops.cli import add_ops_parsers, run_ops
from cambium.roster import Roster
from cambium.transport.loopback import LoopbackTransport
from cambium.transport.serial_cobs import SerialCobsTransport


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
    p_serve.add_argument(
        "--fake-fleet",
        type=int,
        metavar="N",
        help="attach N synthetic fixtures behind a loopback transport "
        "(implies --transport loopback)",
    )

    p_fake = sub.add_parser(
        "fakefleet", help="run the in-process fake fleet (no hardware)"
    )
    fake_sub = p_fake.add_subparsers(dest="fake_command", required=True)
    p_fake_run = fake_sub.add_parser(
        "run", help="daemon + N virtual fixtures + browser viewer"
    )
    p_fake_run.add_argument(
        "--config", default="config/cambium.toml",
        help="path to cambium.toml (default: %(default)s)",
    )
    p_fake_run.add_argument(
        "--fixtures",
        help="Elliot-schema fixtures json (e.g. resonance-lighting/app/"
        "public/fixtures-bench10.json); default: --count synthetic line",
    )
    p_fake_run.add_argument(
        "--count", type=int, default=10,
        help="synthetic fixture count when no --fixtures file (default: %(default)s)",
    )
    p_fake_run.add_argument(
        "--start-night", action="store_true",
        help="boot the virtual fixtures in NIGHT (skip the day-gate rehearsal)",
    )
    p_fake_run.add_argument(
        "--loss", type=float, default=0.0, metavar="P",
        help="drop daemon->fleet frames with probability P (rehearse radio loss)",
    )
    p_fake_run.add_argument(
        "--seed", type=int, default=0,
        help="rng seed for --loss determinism (default: %(default)s)",
    )

    add_sweep_parser(sub)
    add_map_parser(sub)
    add_ops_parsers(sub)
    return parser


def _load_config(path: str, cmd: str) -> CambiumConfig | None:
    try:
        return CambiumConfig.load(path)
    except FileNotFoundError:
        print(
            f"cambium {cmd}: config file {path!r} not found; pass "
            f"--config path/to/cambium.toml (see config/cambium.toml in the "
            f"repo) or run from the repo root",
            file=sys.stderr,
        )
    except ValueError as e:
        print(f"cambium {cmd}: {e}", file=sys.stderr)
    return None


def _fakefleet_run(args: argparse.Namespace) -> int:
    import random

    from cambium.fakefleet.runner import (
        FakeFleet,
        fixtures_from_file,
        synthetic_fixtures,
    )
    from cambium.fakefleet.viewer import attach_viewer
    from cambium.roster import Roster

    config = _load_config(args.config, "fakefleet run")
    if config is None:
        return 2
    if args.fixtures:
        try:
            fixtures = fixtures_from_file(args.fixtures)
        except (OSError, ValueError) as e:
            print(f"cambium fakefleet run: {e}", file=sys.stderr)
            return 2
    else:
        fixtures = synthetic_fixtures(args.count)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    transport = LoopbackTransport(
        loss_rate=args.loss, rng=random.Random(args.seed)
    )
    fleet = FakeFleet(fixtures, start_night=args.start_night)
    # The daemon addresses the SAME fixtures the fake fleet embodies.
    daemon = Daemon(config, transport, Roster(list(fixtures)))
    attach_viewer(daemon.app, fleet)

    print(f"fake fleet: {len(fixtures)} fixtures | viewer: "
          f"http://localhost:{config.port}/fakefleet/")
    if not args.start_night:
        print('fixtures boot DAY-gated (like real hardware): run '
              '"cambium night on" (W5) or pass --start-night')
    asyncio.run(_run_with_fleet(daemon, fleet, transport))
    return 0


async def _run_with_fleet(daemon: Daemon, fleet, transport: LoopbackTransport) -> None:
    await daemon.start()
    await fleet.start(transport.peer)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await fleet.stop()
        await daemon.stop()


def _serve(args: argparse.Namespace) -> int:
    if args.fake_fleet:
        # serve --fake-fleet N == fakefleet run --count N (one wiring path).
        args.count = args.fake_fleet
        args.fixtures = None
        args.start_night = False
        args.loss = 0.0
        args.seed = 0
        return _fakefleet_run(args)
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
    if args.command == "fakefleet":
        return _fakefleet_run(args)
    if args.command == "sweep":
        return cmd_sweep(args)
    if args.command == "map":
        return cmd_map(args)
    if args.command in ("doctor", "blink", "night", "identify"):
        return run_ops(args)
    print(f"cambium: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
