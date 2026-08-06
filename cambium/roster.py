"""Roster: which fixtures exist, where they are, and how we address them.

Roster CSVs are hand-edited on site, so every parse error names the file,
the line, and the fix -- a person at the playa with a headlamp should be
able to repair the file from the error message alone.
"""

import csv
from pathlib import Path

from cambium.model import Fixture, FixtureClass

# ESP-NOW payload budget caps how many per-fixture color slots fit in one
# downlink packet; 18 is the firmware-side limit per SHOWFRAME.
TX_CHUNK_SIZE = 18

ROSTER_COLUMNS = ["fixture_id", "mac", "class", "x", "y", "z", "notes"]

_CLASS_NAMES = {m.name.lower(): m for m in FixtureClass}
_CLASS_NAMES["trunk"] = FixtureClass.UPLIGHT
_CLASS_FIX = "use one of: " + ", ".join(
    m.name.lower() for m in FixtureClass if m is not FixtureClass.UNKNOWN
) + ", trunk (alias for legacy uplight class)"


def normalize_mac(raw: str, where: str) -> str:
    """Accept full '68:EE:8F:F2:BD:B4' or short 'F2BDB4'; return short-upper.

    `where` prefixes the error (file + line) so hand-edited CSVs are fixable.
    """
    s = raw.strip().upper()
    parts = s.split(":")
    if len(parts) == 6 and all(len(p) == 2 for p in parts):
        s = "".join(parts[3:])  # identity = last 3 MAC bytes
    if len(s) == 6 and all(c in "0123456789ABCDEF" for c in s):
        return s
    raise ValueError(
        f"{where}: bad mac {raw!r}; write the full MAC like '68:EE:8F:F2:BD:B4' "
        f"or the short id like 'F2BDB4' (6 hex digits)"
    )


class Roster:
    def __init__(self, fixtures: list[Fixture]):
        self.fixtures = fixtures
        self.by_mac: dict[str, Fixture] = {f.mac: f for f in fixtures}
        self.by_id: dict[str, Fixture] = {
            f.fixture_id: f for f in fixtures if f.fixture_id is not None
        }

    @classmethod
    def load(cls, csv_path: str | Path) -> "Roster":
        path = Path(csv_path)
        fixtures: list[Fixture] = []
        seen_mac: dict[str, int] = {}  # mac -> first line, for dup messages
        seen_id: dict[str, int] = {}
        header_seen = False
        with path.open(newline="") as f:
            for lineno, raw in enumerate(f, start=1):
                where = f"{path} line {lineno}"
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                row = [c.strip() for c in next(csv.reader([raw]))]
                if not header_seen:
                    if row != ROSTER_COLUMNS:
                        raise ValueError(
                            f"{where}: bad header {','.join(row)!r}; the first "
                            f"non-comment line must be exactly "
                            f"'{','.join(ROSTER_COLUMNS)}'"
                        )
                    header_seen = True
                    continue
                if len(row) != len(ROSTER_COLUMNS):
                    raise ValueError(
                        f"{where}: {len(row)} columns, expected "
                        f"{len(ROSTER_COLUMNS)} ({','.join(ROSTER_COLUMNS)}); "
                        f"add the missing commas -- empty fields are fine"
                    )
                fid_raw, mac_raw, class_raw, x, y, z, _notes = row

                mac = normalize_mac(mac_raw, where)
                if mac in seen_mac:
                    raise ValueError(
                        f"{where}: duplicate mac {mac} (first used on line "
                        f"{seen_mac[mac]}); delete one of the two rows"
                    )
                seen_mac[mac] = lineno

                fixture_id = fid_raw or None
                if fixture_id is not None:
                    if fixture_id in seen_id:
                        raise ValueError(
                            f"{where}: duplicate fixture_id {fixture_id!r} "
                            f"(first used on line {seen_id[fixture_id]}); "
                            f"rename one of the two rows"
                        )
                    seen_id[fixture_id] = lineno

                fclass = _CLASS_NAMES.get(class_raw.lower())
                if fclass is None:
                    raise ValueError(
                        f"{where}: unknown class {class_raw!r}; {_CLASS_FIX}"
                    )

                if x == "" and y == "" and z == "":
                    xyz = None
                elif x == "" or y == "" or z == "":
                    raise ValueError(
                        f"{where}: partial position x={x!r} y={y!r} z={z!r}; "
                        f"fill in all three of x,y,z or leave all three empty"
                    )
                else:
                    try:
                        xyz = (float(x), float(y), float(z))
                    except ValueError:
                        raise ValueError(
                            f"{where}: x,y,z must be numbers, got "
                            f"x={x!r} y={y!r} z={z!r}; write plain decimals "
                            f"like 1.5 (meters)"
                        ) from None

                fixtures.append(Fixture(mac, fixture_id, fclass, xyz))
        if not header_seen:
            raise ValueError(
                f"{path}: no header row found; the first non-comment line must "
                f"be '{','.join(ROSTER_COLUMNS)}'"
            )
        return cls(fixtures)

    @classmethod
    def from_registry(
        cls,
        registry_csv_path: str | Path,
        classes: dict[str, str] | None = None,
    ) -> "Roster":
        """Build a roster from Ben's ops/fleet/registry.csv.

        Keeps commissioned boards only, and excludes the serial_bridge board:
        it is our downlink radio, not a lightable fixture. Class defaults to
        DOWNLIGHT; `classes` maps mac (full or short) -> class name to override.
        """
        path = Path(registry_csv_path)
        overrides: dict[str, FixtureClass] = {}
        for k, v in (classes or {}).items():
            mac = normalize_mac(k, f"classes[{k!r}]")
            fclass = _CLASS_NAMES.get(v.strip().lower())
            if fclass is None:
                raise ValueError(f"classes[{k!r}]: unknown class {v!r}; {_CLASS_FIX}")
            overrides[mac] = fclass

        fixtures: list[Fixture] = []
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status", "").strip() != "commissioned":
                    continue
                if row.get("role", "").strip() == "serial_bridge":
                    continue
                mac = normalize_mac(row["mac"], f"{path} mac {row['mac']!r}")
                fclass = overrides.get(mac, FixtureClass.DOWNLIGHT)
                # Registry's fixture_id column is just the short mac again;
                # human labels (B###/F###) come from roster CSVs, not here.
                fixtures.append(Fixture(mac, None, fclass, None))
        return cls(fixtures)

    def tx_partition(self) -> list[list[Fixture]]:
        """Chunk the roster into downlink packets of <= TX_CHUNK_SIZE fixtures.

        Fixtures are sorted by mac before chunking, so for a given roster the
        partition is STABLE: the same fixture always lands in the same chunk.
        Why: each chunk becomes one radio packet, and ESP-NOW multicast drops
        whole packets -- with a stable partition a lost packet always affects
        the same, predictable subset of fixtures instead of a shifting random
        set, which makes drops diagnosable on site.
        """
        ordered = sorted(self.fixtures, key=lambda f: f.mac)
        return [
            ordered[i : i + TX_CHUNK_SIZE]
            for i in range(0, len(ordered), TX_CHUNK_SIZE)
        ]
