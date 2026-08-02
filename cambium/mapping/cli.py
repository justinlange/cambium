"""`cambium sweep ...` and `cambium map ...` -- the mapping pipeline CLI.

Site layout (one dir per deployment, --site picks it):
    site/<name>/sweeps/<sweep>/roster.json     frozen index->mac contract
    site/<name>/map/measured-points.json       ingest output
    site/<name>/map/transform.json             align output (versioned)
    site/<name>/map/assignments.json           assign output
    site/<name>/map/assignments-overrides.json hand-written, always wins
    site/<name>/out/fixtures-map.json          the canonical join
    site/<name>/out/fixtures.measured.json     Elliot-schema, app-loadable
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from cambium.config import CambiumConfig
from cambium.roster import Roster

from . import align as align_mod
from . import assign as assign_mod
from . import export as export_mod
from . import ingest as ingest_mod
from . import sweep as sweep_mod


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _site(args) -> Path:
    return Path("site") / args.site


def _fail(msg: str) -> int:
    print(f"cambium: {msg}", file=sys.stderr)
    return 2


def add_sweep_parser(sub) -> None:
    p = sub.add_parser("sweep", help="freeze a sweep roster for Constellate")
    s = p.add_subparsers(dest="sweep_command", required=True)
    start = s.add_parser("start", help="freeze the roster + print the constellate command")
    start.add_argument("--site", default="bench")
    start.add_argument("--config", default="config/cambium.toml")
    start.add_argument("--name", help="sweep dir name (default: UTC timestamp)")
    start.add_argument(
        "--daemon",
        help="running daemon URL (e.g. http://localhost:8600): marks fixtures "
        "not heard recently as alive_at_freeze=false",
    )


def cmd_sweep(args) -> int:
    try:
        config = CambiumConfig.load(args.config)
        roster = Roster.load(config.roster)
    except (FileNotFoundError, ValueError) as e:
        return _fail(str(e))
    name = args.name or _now_iso().replace(":", "").replace("-", "")[:15]
    sweep_dir = _site(args) / "sweeps" / name

    online = None
    if args.daemon:
        import asyncio

        from cambium.ops.oplink import HttpOpsLink

        async def fetch() -> set[str]:
            link = HttpOpsLink(args.daemon)
            await link.open()
            try:
                census = await link.census(2.0)
                return {m for m, c in census.items() if c.get("online")}
            finally:
                await link.close()

        try:
            online = asyncio.run(fetch())
        except ConnectionError as e:
            return _fail(str(e))

    try:
        path = sweep_mod.freeze_roster(
            roster,
            sweep_dir,
            created=_now_iso(),
            roster_path=config.roster,
            espnow_channel=config.channel,
            online_macs=online,
        )
    except FileExistsError as e:
        return _fail(str(e))
    print(f"frozen: {path}")
    if online is not None:
        dead = [f.mac for f in roster.fixtures if f.mac not in online]
        if dead:
            print(f"warning: not heard recently (alive_at_freeze=false): {', '.join(sorted(dead))}")
    print("\nnow start cambium (if not running) and paste:\n")
    print(sweep_mod.constellate_command(sweep_dir, port=config.port))
    print(f"\nafter the sweep + Constellate export:\n"
          f"  cambium map ingest <session-dir> --sweep {name} --site {args.site}")
    return 0


def add_map_parser(sub) -> None:
    p = sub.add_parser("map", help="sweep results -> fixtures-map + Elliot fixtures.json")
    s = p.add_subparsers(dest="map_command", required=True)

    ing = s.add_parser("ingest", help="join a Constellate export to the frozen roster")
    ing.add_argument("session", help="Constellate session dir (or bare points.json)")
    ing.add_argument("--sweep", required=True, help="sweep name under site/<s>/sweeps/")
    ing.add_argument("--site", default="bench")
    ing.add_argument("--allow-unscaled", action="store_true")
    ing.add_argument("--assert-scaled", action="store_true")

    al = s.add_parser("align", help="fit camera->world from operator anchors")
    al.add_argument("--anchors", required=True,
                    help='json: [{"index":3,"world":[x,y,z]} | {"index":3,"fixture_id":"B004"}]')
    al.add_argument("--authored", help="Elliot fixtures.json for fixture_id anchors")
    al.add_argument("--site", default="bench")

    asn = s.add_parser("assign", help="measured lanterns -> authored slots (mutual NN)")
    asn.add_argument("--authored", help="Elliot fixtures.json (slot positions)")
    asn.add_argument("--overrides", help="assignments-overrides.json (always wins)")
    asn.add_argument("--from-calibration", help="Elliot calibration export (confirmed = truth)")
    asn.add_argument("--site", default="bench")

    ex = s.add_parser("export", help="write fixtures-map.json + fixtures.measured.json")
    ex.add_argument("--authored", help="Elliot fixtures.json (slot attributes)")
    ex.add_argument("--strict", action="store_true",
                    help="omit unmapped fixtures instead of authored-fallback")
    ex.add_argument("--calibration",
                    help="also write cambium's hypotheses in Elliot's calibration shape")
    ex.add_argument("--site", default="bench")

    st = s.add_parser("status", help="where the pipeline stands")
    st.add_argument("--site", default="bench")


def cmd_map(args) -> int:
    site = _site(args)
    mp = site / "map"
    try:
        if args.map_command == "ingest":
            doc = ingest_mod.ingest(
                args.session,
                site / "sweeps" / args.sweep,
                mp / "measured-points.json",
                created=_now_iso(),
                allow_unscaled=args.allow_unscaled,
                assert_scaled=args.assert_scaled,
            )
            measured = sum(1 for e in doc["entries"] if e["status"] == "measured")
            print(f"ingested: {measured} measured, "
                  f"{len(doc['entries']) - measured} unmapped -> {mp/'measured-points.json'}")
            print(f"next: cambium map align --anchors anchors.json --site {args.site}")
            return 0

        measured_doc = _read(mp / "measured-points.json",
                             "run `cambium map ingest` first")

        if args.map_command == "align":
            anchors = json.loads(Path(args.anchors).read_text())
            authored_pts = None
            if args.authored:
                authored_pts = {
                    fid: f["position"]
                    for fid, f in export_mod.load_authored(args.authored).items()
                }
            measured = {
                e["sweep_index"]: e["xyz_camera"]
                for e in measured_doc["entries"] if e["xyz_camera"] is not None
            }
            scaled = measured_doc["provenance"]["scaled"]
            fit = align_mod.fit_anchors(
                anchors, measured, with_scale=not scaled, authored=authored_pts
            )
            path = align_mod.write_transform(
                fit, mp / "transform.json", created=_now_iso(),
                session_ref=measured_doc["provenance"]["points"],
                scaled_input=scaled,
            )
            print(f"transform -> {path}")
            print(f"residuals (m): {fit['residuals_m']}")
            for w in align_mod.sanity_report(fit, scaled_input=scaled):
                print(f"warning: {w}")
            print(f"next: cambium map assign --site {args.site}"
                  + (f" --authored {args.authored}" if args.authored else ""))
            return 0

        tf = _read(mp / "transform.json", "run `cambium map align` first")
        world = {
            e["mac"]: align_mod.apply_transform(tf, e["xyz_camera"])
            for e in measured_doc["entries"] if e["xyz_camera"] is not None
        }

        if args.map_command == "assign":
            authored_pts = {}
            if args.authored:
                authored_pts = {
                    fid: f["position"]
                    for fid, f in export_mod.load_authored(args.authored).items()
                }
            else:
                # No authored layout: keep the roster's own fixture_ids.
                authored_pts = {}
            overrides = (
                json.loads(Path(args.overrides).read_text()) if args.overrides
                else _maybe(mp / "assignments-overrides.json")
            )
            confirmed = (
                assign_mod.load_calibration(args.from_calibration)
                if args.from_calibration else None
            )
            if authored_pts:
                assignments = assign_mod.assign(
                    world, authored_pts, overrides=overrides, confirmed=confirmed
                )
            else:
                # Trivial path: trust the roster's fixture_id per mac.
                assignments = {
                    e["mac"]: {
                        "fixture_id": e.get("fixture_id"), "method": "roster",
                        "confidence": 1.0, "distance_m": None, "ambiguous_with": None,
                    }
                    for e in measured_doc["entries"]
                }
            export_mod.write_json(
                {"created": _now_iso(), "assignments": assignments},
                mp / "assignments.json",
            )
            ambiguous = [m for m, a in assignments.items() if a.get("ambiguous_with")]
            print(f"assigned: {sum(1 for a in assignments.values() if a['fixture_id'])} "
                  f"of {len(assignments)}; ambiguous: {ambiguous or 'none'}")
            if ambiguous:
                print("resolve ambiguity in map/assignments-overrides.json "
                      '([{"mac": "...", "fixture_id": "...", "note": "..."}]) '
                      "or confirm in Elliot's commissioning UI and re-run with "
                      "--from-calibration")
            print(f"next: cambium map export --site {args.site}"
                  + (f" --authored {args.authored}" if args.authored else ""))
            return 0

        assignments = _read(mp / "assignments.json",
                            "run `cambium map assign` first")["assignments"]

        if args.map_command == "status":
            counts: dict[str, int] = {}
            for e in measured_doc["entries"]:
                a = assignments.get(e["mac"], {}) if assignments else {}
                if e["xyz_camera"] is None:
                    s = "unmapped"
                elif a.get("ambiguous_with"):
                    s = "ambiguous"
                elif a.get("fixture_id"):
                    s = "mapped"
                else:
                    s = "unassigned"
                counts[s] = counts.get(s, 0) + 1
                print(f"  {e['mac']}  idx={e['sweep_index']:3d}  "
                      f"{a.get('fixture_id') or '-':8s} {s}")
            print("summary: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
            return 0

        authored = export_mod.load_authored(args.authored) if args.authored else None

        if args.map_command == "export":
            fmap = export_mod.build_fixtures_map(
                measured_doc, world, assignments, authored,
                created=_now_iso(),
                sources={
                    "measured": str(mp / "measured-points.json"),
                    "transform": str(mp / "transform.json"),
                    "authored": args.authored,
                },
            )
            out = site / "out"
            export_mod.write_json(fmap, out / "fixtures-map.json")
            doc = export_mod.to_elliot_doc(
                fmap, authored, created=_now_iso(), strict=args.strict
            )
            export_mod.write_json(doc, out / "fixtures.measured.json")
            print(f"wrote {out/'fixtures-map.json'}")
            print(f"wrote {out/'fixtures.measured.json'} "
                  f"({doc['meta']['count']} fixtures, {doc['meta']['source']})")
            if args.calibration:
                export_mod.write_json(
                    assign_mod.to_calibration(assignments, at=_now_iso()),
                    args.calibration,
                )
                print(f"wrote {args.calibration} (import in Elliot's commissioning panel)")
            print("load it in the simulator: replace app/public/fixtures.json "
                  "(back it up first) or serve with ?fixtures=")
            return 0
    except (FileNotFoundError, ValueError, FileExistsError) as e:
        return _fail(str(e))
    return _fail(f"unknown map command {args.map_command!r}")


def _read(path: Path, fix: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- {fix}")
    return json.loads(path.read_text())


def _maybe(path: Path):
    return json.loads(path.read_text()) if path.exists() else None
