"""map export: the canonical fixtures-map.json + Elliot's fixtures.measured.json.

fixtures-map.json (cambium.fixtures-map/1) is the single join everything
reads: mac ∥ fixture_id ∥ class ∥ sweep_index ∥ measured world xyz ∥
authored xyz ∥ status ∥ provenance.

fixtures.measured.json is emitted in Elliot's schema (resonance.fixtures/0.3)
so his app loads MEASURED REALITY with zero code changes: positions are
world xyz (already Z-up meters -- the align transform did the axis work),
authored slot attributes (role/zone/beam...) are copied when assigned, and
two extra keys his validator tolerates (mac, cambium_status) make the
provenance visible in his DataLog.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "cambium.fixtures-map/1"

_ROLE_BY_CLASS = {
    "DOWNLIGHT": "downlight",
    "UPLIGHT": "uplight",
    "CHANDELIER": "chandelier",
    # Elliot's schema has no perimeter role yet; export as downlight with the
    # honest led_type below until his schema grows one.
    "PERIMETER": "downlight",
}
_LED_TYPE_BY_CLASS = {"PERIMETER": "rgbw_hex37"}
_BEAM_BY_ROLE = {"downlight": 120, "uplight": 90, "chandelier": 60}
_AIM_BY_ROLE = {"downlight": [0, 0, -1], "uplight": [0, 0, 1], "chandelier": [0, 0, -1]}


def build_fixtures_map(
    measured_doc: dict,
    world_points: dict[str, list[float]],
    assignments: dict[str, dict],
    authored: dict[str, dict] | None,
    *,
    created: str,
    sources: dict,
) -> dict:
    """Join everything. authored: fixture_id -> full authored fixture dict."""
    authored = authored or {}
    fixtures = []
    for e in measured_doc["entries"]:
        mac = e["mac"]
        a = assignments.get(mac, {})
        fid = a.get("fixture_id") or e.get("fixture_id")
        measured_xyz = world_points.get(mac)
        authored_xyz = None
        if fid and fid in authored:
            authored_xyz = authored[fid].get("position")
        if measured_xyz is not None:
            status = "mapped" if fid else "unassigned"
        elif a.get("ambiguous_with"):
            status = "ambiguous"
        else:
            status = "unmapped"
        fixtures.append(
            {
                "fixture_id": fid,
                "mac": mac,
                "class": e["class"],
                "sweep_index": e["sweep_index"],
                "measured_xyz": measured_xyz,
                "authored_xyz": authored_xyz,
                "position_source": (
                    "measured" if measured_xyz is not None
                    else "authored" if authored_xyz is not None
                    else "none"
                ),
                "status": status,
                "assignment": {
                    "method": a.get("method"),
                    "confidence": a.get("confidence"),
                    "distance_m": a.get("distance_m"),
                    "ambiguous_with": a.get("ambiguous_with"),
                }
                if a
                else None,
            }
        )
    return {
        "schema": SCHEMA,
        "meta": {"generated": created, "sources": sources},
        "fixtures": fixtures,
    }


def to_elliot_doc(
    fixtures_map: dict,
    authored: dict[str, dict] | None,
    *,
    created: str,
    strict: bool = False,
) -> dict:
    """fixtures-map -> resonance.fixtures/0.3. strict=True omits fixtures
    without a measured position instead of falling back to authored."""
    authored = authored or {}
    out_fixtures = []
    fallbacks = 0
    for f in fixtures_map["fixtures"]:
        pos = f["measured_xyz"]
        status = "measured"
        if pos is None:
            if strict or f["authored_xyz"] is None:
                continue
            pos = f["authored_xyz"]
            status = "authored-fallback"
            fallbacks += 1
        fid = f["fixture_id"] or f"M{f['sweep_index']:03d}"  # unassigned: synthetic id
        slot = authored.get(f["fixture_id"] or "", {})
        role = slot.get("role") or _ROLE_BY_CLASS.get(f["class"], "downlight")
        out_fixtures.append(
            {
                "fixture_id": fid,
                "name": slot.get("name") or fid,
                "role": role,
                "position": [round(float(v), 4) for v in pos],
                "aim": slot.get("aim") or _AIM_BY_ROLE[role],
                "zone": slot.get("zone") or "bench",
                "led_type": slot.get("led_type")
                or _LED_TYPE_BY_CLASS.get(f["class"], "rgbw_4w"),
                "lumens_max": slot.get("lumens_max", 450),
                "beam_deg": slot.get("beam_deg", _BEAM_BY_ROLE[role]),
                "design_color": slot.get("design_color", [1, 1, 1]),
                # Extra keys -- tolerated by validateFixturesDoc, useful in
                # the app's DataLog:
                "mac": f["mac"],
                "cambium_status": status,
            }
        )
    if not out_fixtures:
        raise ValueError(
            "no fixtures to export -- every entry is unmapped with no "
            "authored fallback; run map align/assign first (see map status)"
        )
    xs, ys, zs = zip(*(f["position"] for f in out_fixtures))
    doc = {
        "meta": {
            "source": (
                f"cambium map export (measured {len(out_fixtures) - fallbacks}, "
                f"authored-fallback {fallbacks})"
            ),
            "exported": created,
            "up_axis": "Z",
            "units": "m",
            "count": len(out_fixtures),
            "bbox": {
                "min": [min(xs), min(ys), min(zs)],
                "max": [max(xs), max(ys), max(zs)],
            },
            "schema": "resonance.fixtures/0.3",
        },
        "fixtures": out_fixtures,
    }
    validate_elliot_doc(doc)  # self-check against the app's contract
    return doc


def validate_elliot_doc(doc: dict) -> None:
    """The load-bearing subset of the app's validateFixturesDoc (fixtures.ts):
    meta.count, meta.bbox, and per-fixture fixture_id/position[3]/beam_deg/
    zone must be present; extra keys are fine."""
    meta = doc.get("meta") or {}
    if meta.get("count") != len(doc.get("fixtures", [])):
        raise ValueError("meta.count != len(fixtures) -- export bug")
    if "bbox" not in meta:
        raise ValueError("meta.bbox missing -- export bug")
    for f in doc["fixtures"]:
        for key in ("fixture_id", "position", "beam_deg", "zone"):
            if key not in f or f[key] is None:
                raise ValueError(f"fixture {f.get('fixture_id')}: {key} missing")
        if len(f["position"]) != 3:
            raise ValueError(f"fixture {f['fixture_id']}: position is not [x,y,z]")


def load_authored(path: str | Path) -> dict[str, dict]:
    """An Elliot-schema fixtures.json -> {fixture_id: fixture dict}."""
    doc = json.loads(Path(path).read_text())
    return {f["fixture_id"]: f for f in doc.get("fixtures", [])}


def write_json(doc: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2) + "\n")
    return p
