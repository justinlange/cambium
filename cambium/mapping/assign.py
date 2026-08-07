"""map assign: which measured lantern occupies which authored slot.

Mutual nearest-neighbor with an explicit ambiguity test -- at bench-10 the
report is 10 obvious lines; at 130 the ambiguous rows are exactly the ones
a human (or Elliot's commissioning UI) must confirm. Overrides always win.

Interop, not duplication: this module reads and writes the calibration map
shape Elliot's app already uses (calibration.ts v2: {version: 2, entries:
[{mac, fixtureId, at, stage, confidence, method}]}), so cambium's NN
hypotheses can flow into his SelfMap/Commissioning UI for confirmation and
his confirmed assignments can flow back as truth.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

# Ambiguity thresholds (plan §7): a match is only trusted when it is clearly
# the best AND physically close.
AMBIGUOUS_RATIO = 1.5  # 2nd-nearest closer than 1.5x nearest -> ambiguous
MAX_DIST_M = 0.75      # nearest farther than this -> ambiguous


def _dist(a: list[float], b: list[float]) -> float:
    return math.dist(a, b)


def assign(
    measured: dict[str, list[float]],
    authored: dict[str, list[float]],
    *,
    overrides: list[dict] | None = None,
    confirmed: dict[str, str] | None = None,
) -> dict[str, dict]:
    """mac -> {fixture_id|None, method, confidence, distance_m, ambiguous_with}.

    measured: mac -> world xyz. authored: fixture_id -> world xyz.
    overrides: [{"mac": ..., "fixture_id": ..., "note": ...}] -- always win.
    confirmed: mac -> fixture_id from an imported calibration map (stage
    "confirmed"/"locked" in Elliot's app) -- treated as truth after overrides.
    """
    out: dict[str, dict] = {}
    taken: set[str] = set()

    for ov in overrides or []:
        out[ov["mac"]] = {
            "fixture_id": ov["fixture_id"],
            "method": "manual",
            "confidence": 1.0,
            "distance_m": None,
            "ambiguous_with": None,
        }
        taken.add(ov["fixture_id"])

    for mac, fid in (confirmed or {}).items():
        if mac in out or fid in taken:
            continue
        out[mac] = {
            "fixture_id": fid,
            "method": "confirmed",
            "confidence": 1.0,
            "distance_m": None,
            "ambiguous_with": None,
        }
        taken.add(fid)

    todo = [m for m in measured if m not in out]
    slots = {f: p for f, p in authored.items() if f not in taken}
    for mac in sorted(todo):
        ranked = sorted(slots, key=lambda f: _dist(measured[mac], slots[f]))
        if not ranked:
            out[mac] = {
                "fixture_id": None, "method": "nn", "confidence": 0.0,
                "distance_m": None, "ambiguous_with": None,
            }
            continue
        best = ranked[0]
        d0 = _dist(measured[mac], slots[best])
        d1 = _dist(measured[mac], slots[ranked[1]]) if len(ranked) > 1 else math.inf
        ambiguous = d0 > MAX_DIST_M or d1 < AMBIGUOUS_RATIO * d0
        if ambiguous:
            out[mac] = {
                "fixture_id": None,
                "method": "nn",
                "confidence": 0.0,
                "distance_m": round(d0, 3),
                "ambiguous_with": (
                    [best, ranked[1]] if d1 < AMBIGUOUS_RATIO * d0 else [best]
                ),
            }
        else:
            # Greedy mutual claim: a slot goes to the first mac that wins it
            # cleanly (macs iterate sorted -- deterministic).
            out[mac] = {
                "fixture_id": best,
                "method": "nn",
                "confidence": round(max(0.0, 1.0 - d0 / MAX_DIST_M), 2),
                "distance_m": round(d0, 3),
                "ambiguous_with": None,
            }
            del slots[best]
    return out


# ---------------------------------------------------------------------------
# Elliot calibration-map interop (calibration.ts v2)
# ---------------------------------------------------------------------------

def load_calibration(path: str | Path) -> dict[str, str]:
    """Elliot's export -> {mac: fixture_id} for confirmed/locked entries."""
    doc = json.loads(Path(path).read_text())
    if doc.get("version") != 2:
        raise ValueError(
            f"{path}: calibration version {doc.get('version')!r} != 2 -- "
            f"re-export from the app's commissioning panel (calibration.ts v2)"
        )
    return {
        e["mac"].upper(): e["fixtureId"]
        for e in doc.get("entries", [])
        if e.get("stage") in ("confirmed", "locked") and e.get("fixtureId")
    }


def to_calibration(assignments: dict[str, dict], *, at: str) -> dict:
    """cambium hypotheses -> Elliot's shape, stage 'hypothesis', method
    'photo' (camera-mapping-derived) so his UI can confirm them."""
    entries = []
    for mac, a in sorted(assignments.items()):
        if not a["fixture_id"]:
            continue
        entries.append(
            {
                "mac": mac,
                "fixtureId": a["fixture_id"],
                "at": at,
                "stage": "confirmed" if a["method"] in ("manual", "confirmed") else "hypothesis",
                "confidence": a["confidence"],
                "method": "manual" if a["method"] == "manual" else "photo",
            }
        )
    return {"version": 2, "entries": entries}
