"""make_sweep: a synthetic Constellate export for rehearsing the map pipeline.

Emits exactly what a real sweep produces -- a frozen sweep roster plus a
Constellate-shaped session dir (session.json + export/points.json) -- but
from known fixture geometry corrupted through a deterministic similarity
transform into an OpenCV-style camera frame (+Y down, arbitrary origin),
plus noise and optional dropout. truth.json keeps the ground truth so W5's
ingest -> align -> assign -> export pipeline can assert recovery without
Constellate installed.

Artifact shapes mirrored from Constellate (src/constellate/session.py and
pipeline/export.py) and the cambium sweep contract (cambium.sweep-roster/1).
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from cambium.model import Fixture


def _rotation(rng: random.Random) -> list[list[float]]:
    """A deterministic proper rotation that maps world +Z (up) near camera -Y
    (OpenCV: +Y is DOWN), with a random yaw -- i.e. a plausible phone pose."""
    yaw = rng.uniform(0.0, 2 * math.pi)
    cy, sy = math.cos(yaw), math.sin(yaw)
    # World: X east, Y north, Z up.  Camera: X right, Y down, Z forward.
    # Look roughly north with a small random pitch; up maps to -Y exactly
    # when pitch = 0.
    pitch = rng.uniform(-0.15, 0.15)
    cp, sp = math.cos(pitch), math.sin(pitch)
    # R = Rx(pitch + axis swap) . Rz(yaw): rows are camera axes in world coords.
    return [
        [cy, sy, 0.0],                       # camera X (right)
        [sy * sp, -cy * sp, -cp],            # camera Y (down ~ -Z_world)
        [-sy * cp, cy * cp, -sp],            # camera Z (forward)
    ]


def _apply(R: list[list[float]], t: list[float], p: tuple[float, float, float]) -> list[float]:
    return [
        R[i][0] * p[0] + R[i][1] * p[1] + R[i][2] * p[2] + t[i] for i in range(3)
    ]


def make_sweep(
    fixtures: list[Fixture],
    out_dir: str | Path,
    *,
    created: str,
    seed: int = 0,
    noise_m: float = 0.02,
    dropout: list[int] | None = None,
) -> dict:
    """Write roster.json + session/ + truth.json under out_dir; return paths.

    created: caller-supplied ISO timestamp (kept out of this module so runs
    are reproducible byte-for-byte for a given seed+created).
    """
    out = Path(out_dir)
    (out / "session" / "export").mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    dropout = dropout or []

    placed = sorted(
        (f for f in fixtures if f.xyz is not None), key=lambda f: f.mac
    )
    if len(placed) < 2:
        raise ValueError(
            f"make_sweep needs >= 2 fixtures with xyz to pin the tape scale; "
            f"got {len(placed)} -- give the fixtures positions first"
        )

    # The frozen sweep roster: index = mac-ascending order (the same stable
    # order MappingMode.light() resolves against).
    roster = {
        "schema": "cambium.sweep-roster/1",
        "created": created,
        "source": {"generator": "fakefleet.make_sweep", "seed": seed},
        "espnow_channel": 11,
        "entries": [
            {
                "index": i,
                "mac": f.mac,
                "class": f.cls.name,
                "alive_at_freeze": True,
            }
            for i, f in enumerate(placed)
        ],
    }

    # Similarity: world -> camera frame (scale 1.0: the tape pins meters).
    R = _rotation(rng)
    t = [rng.uniform(-0.5, 0.5), rng.uniform(-0.5, 0.5), rng.uniform(2.0, 4.0)]
    points: dict[str, list[float]] = {}
    for i, f in enumerate(placed):
        if i in dropout:
            continue  # unseen by >= 2 cameras: absent, not wrong
        cam = _apply(R, t, f.xyz)
        points[str(i)] = [
            round(c + rng.gauss(0.0, noise_m), 6) for c in cam
        ]

    d01 = math.dist(placed[0].xyz, placed[1].xyz)
    session = {
        "created": created,
        "led_count": len(placed),
        "driver": "fakefleet",
        # Constellate shape: without this the export is in arbitrary units
        # and W5's ingest must refuse it -- so the synthetic sweep always
        # carries a valid tape measurement.
        "scale_measure": {"kind": "led_pair", "a": 0, "b": 1, "meters": round(d01, 6)},
    }
    truth = {
        "world_points": {str(i): list(f.xyz) for i, f in enumerate(placed)},
        "camera_from_world": {"R": R, "t": t, "scale": 1.0},
        "noise_m": noise_m,
        "dropout": sorted(dropout),
    }

    paths = {
        "roster": out / "roster.json",
        "session": out / "session",
        "points": out / "session" / "export" / "points.json",
        "truth": out / "truth.json",
    }
    paths["roster"].write_text(json.dumps(roster, indent=2) + "\n")
    (out / "session" / "session.json").write_text(json.dumps(session, indent=2) + "\n")
    paths["points"].write_text(json.dumps(points, indent=2) + "\n")
    paths["truth"].write_text(json.dumps(truth, indent=2) + "\n")
    return {k: str(v) for k, v in paths.items()}
