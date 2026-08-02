"""Sweep roster freezing: pin sweep index -> MAC before any camera fires.

The frozen roster is the contract that makes a Constellate sweep replayable:
`light(n)` during the sweep and `map ingest` afterwards both resolve n
through THIS file, so a later roster/registry edit can never silently shift
identities between capture and reconstruction.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cambium.roster import Roster

SCHEMA = "cambium.sweep-roster/1"


def freeze_roster(
    roster: Roster,
    out_dir: str | Path,
    *,
    created: str,
    roster_path: str | Path | None = None,
    espnow_channel: int = 11,
    online_macs: set[str] | None = None,
) -> Path:
    """Write sweeps/<stamp>/roster.json; refuses to overwrite (immutable).

    online_macs: when the daemon is reachable, macs heard recently -- others
    are still listed (absent is data) but flagged alive_at_freeze=False so a
    sweep against a dead lantern is a visible decision, not a surprise.
    """
    out = Path(out_dir)
    path = out / "roster.json"
    if path.exists():
        raise FileExistsError(
            f"{path} already exists -- sweep rosters are immutable; start a "
            f"new sweep dir (one per physical sweep) instead of editing"
        )
    out.mkdir(parents=True, exist_ok=True)

    source: dict = {"roster_csv": str(roster_path) if roster_path else None}
    if roster_path and Path(roster_path).exists():
        source["roster_sha256"] = hashlib.sha256(
            Path(roster_path).read_bytes()
        ).hexdigest()

    ordered = sorted(roster.fixtures, key=lambda f: f.mac)
    doc = {
        "schema": SCHEMA,
        "created": created,
        "source": source,
        "espnow_channel": espnow_channel,
        "entries": [
            {
                "index": i,
                "mac": f.mac,
                "fixture_id": f.fixture_id,
                "class": f.cls.name,
                "alive_at_freeze": (
                    None if online_macs is None else f.mac in online_macs
                ),
            }
            for i, f in enumerate(ordered)
        ],
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def load_frozen(sweep_dir: str | Path) -> dict:
    path = Path(sweep_dir) / "roster.json"
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{path} not found -- run `cambium sweep start` first (it freezes "
            f"the roster this sweep's indexes resolve against)"
        ) from None
    if doc.get("schema") != SCHEMA:
        raise ValueError(
            f"{path}: schema {doc.get('schema')!r} != {SCHEMA!r}; this file "
            f"was not written by `cambium sweep start`"
        )
    return doc


def constellate_command(sweep_dir: str | Path, *, port: int = 8600) -> str:
    """The exact command to paste after `cambium sweep start` (FTUX)."""
    doc = load_frozen(sweep_dir)
    n = len(doc["entries"])
    return (
        "constellate serve --driver http \\\n"
        f'  --http-light-url "http://localhost:{port}/constellate/light?led={{led}}" \\\n'
        f'  --http-off-url  "http://localhost:{port}/constellate/off" \\\n'
        f"  --leds {n}"
    )
