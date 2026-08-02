"""map ingest: join a Constellate export to the frozen sweep roster.

The scale gate lives here: Constellate silently exports in ARBITRARY UNITS
when no tape measurement (scale_measure) was set -- the numbers look
identical and nothing downstream can tell. Refusing unscaled sessions at
the door (with the fix in the error) is cheaper than debugging a tree
that came out 40% too small.
"""

from __future__ import annotations

import json
from pathlib import Path

from .sweep import load_frozen

SCHEMA = "cambium.measured-points/1"


def ingest(
    session_or_points: str | Path,
    sweep_dir: str | Path,
    out_path: str | Path,
    *,
    created: str,
    allow_unscaled: bool = False,
    assert_scaled: bool = False,
) -> dict:
    """Write map/measured-points.json; returns the doc. See module docstring."""
    src = Path(session_or_points)
    scale_measure = None
    if src.is_dir():
        points_path = src / "export" / "points.json"
        session_path = src / "session.json"
        if not points_path.exists():
            raise FileNotFoundError(
                f"{points_path} not found -- run the export step in "
                f"Constellate (or `constellate export --session {src}`) first"
            )
        session = json.loads(session_path.read_text()) if session_path.exists() else {}
        scale_measure = session.get("scale_measure")
        if scale_measure is None and not allow_unscaled:
            raise ValueError(
                f"{session_path}: no scale_measure -- the export is in "
                f"ARBITRARY UNITS. In Constellate, set a tape measurement "
                f"(led_pair, meters) and re-export; or pass --allow-unscaled "
                f"to proceed and let `map align` fit scale from >=3 "
                f"tape-measured anchors"
            )
    else:
        # A bare points.json: nothing to verify scale against.
        points_path = src
        if not (assert_scaled or allow_unscaled):
            raise ValueError(
                f"{src} is a bare points.json, so the tape-scale cannot be "
                f"verified. Pass the whole session dir instead; or "
                f"--assert-scaled if you know a scale_measure was set; or "
                f"--allow-unscaled to fit scale during align"
            )

    points: dict[str, list[float]] = json.loads(Path(points_path).read_text())
    roster = load_frozen(sweep_dir)
    n = len(roster["entries"])

    bad = [k for k in points if not k.isdigit() or int(k) >= n]
    if bad:
        raise ValueError(
            f"{points_path}: indexes {sorted(bad)} exceed the frozen roster "
            f"({n} entries in {Path(sweep_dir)/'roster.json'}) -- this export "
            f"belongs to a DIFFERENT sweep; check the session/sweep pairing"
        )

    entries = []
    for e in roster["entries"]:
        xyz = points.get(str(e["index"]))
        entries.append(
            {
                "mac": e["mac"],
                "fixture_id": e.get("fixture_id"),
                "class": e["class"],
                "sweep_index": e["index"],
                "xyz_camera": xyz,  # None = unseen by >=2 cameras: absent, not wrong
                "status": "measured" if xyz is not None else "unmapped",
            }
        )

    doc = {
        "schema": SCHEMA,
        "created": created,
        "provenance": {
            "points": str(points_path),
            "sweep": str(Path(sweep_dir) / "roster.json"),
            "scale_measure": scale_measure,  # copied verbatim; None if unscaled
            "scaled": scale_measure is not None or assert_scaled,
        },
        "entries": entries,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n")
    return doc
