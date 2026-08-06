from pathlib import Path

import pytest

from cambium.model import FixtureClass
from cambium.roster import TX_CHUNK_SIZE, Roster, normalize_mac

BENCH10 = Path(__file__).parent.parent / "config" / "roster-bench10.csv"
BENCH3 = Path(__file__).parent.parent / "config" / "roster-bench3-perimeter.csv"

HEADER = "fixture_id,mac,class,x,y,z,notes\n"


def write_roster(tmp_path, body, name="roster.csv"):
    p = tmp_path / name
    p.write_text("# a comment line\n" + HEADER + body)
    return p


def write_registry(tmp_path):
    """Controlled registry sample; the real sibling registry changes at the bench."""
    p = tmp_path / "registry.csv"
    p.write_text(
        "mac,status,role\n"
        "F2BED4,commissioned,serial_bridge\n"
        "9E5AE8,enumerated,perimeter_demo\n"
        "9F2694,commissioned,\n"
        "F2BDB4,commissioned,\n"
        "F2BDC0,commissioned,\n"
    )
    return p


# ---- normalize_mac ---------------------------------------------------------

def test_normalize_mac_forms():
    assert normalize_mac("68:EE:8F:F2:BD:B4", "x") == "F2BDB4"
    assert normalize_mac("f2bdb4", "x") == "F2BDB4"
    assert normalize_mac(" F2BDB4 ", "x") == "F2BDB4"


def test_normalize_mac_bad():
    with pytest.raises(ValueError) as e:
        normalize_mac("F2:BD:B4", "file.csv line 3")
    msg = str(e.value)
    assert "file.csv line 3" in msg
    assert "68:EE:8F:F2:BD:B4" in msg and "6 hex digits" in msg  # the fix


# ---- Roster.load happy path ------------------------------------------------

def test_load_happy(tmp_path):
    p = write_roster(tmp_path, (
        "B000,68:EE:8F:F2:BD:B4,downlight,1.0,2.0,3.0,front left\n"
        "# mid-file comment\n"
        ",9F2694,PERIMETER,,,,\n"
        "B002,f2bdc0,Uplight,-1.5,0,2.25,\n"
    ))
    r = Roster.load(p)
    assert [f.mac for f in r.fixtures] == ["F2BDB4", "9F2694", "F2BDC0"]
    assert r.by_mac["F2BDB4"].fixture_id == "B000"
    assert r.by_mac["F2BDB4"].xyz == (1.0, 2.0, 3.0)
    assert r.by_mac["9F2694"].fixture_id is None
    assert r.by_mac["9F2694"].xyz is None
    assert r.by_mac["9F2694"].cls is FixtureClass.PERIMETER
    assert r.by_mac["F2BDC0"].cls is FixtureClass.UPLIGHT
    assert r.by_id["B002"].mac == "F2BDC0"
    assert "9F2694" not in {k for k in r.by_id}


def test_load_bench10_ships_valid():
    r = Roster.load(BENCH10)
    assert len(r.fixtures) == 10
    assert [f.fixture_id for f in r.fixtures] == [f"B00{i}" for i in range(10)]
    assert all(f.cls is FixtureClass.DOWNLIGHT for f in r.fixtures)
    assert "F2BED4" not in r.by_mac  # serial_bridge must never be lightable


def test_load_bench3_perimeter_ships_valid():
    r = Roster.load(BENCH3)
    assert [f.mac for f in r.fixtures] == ["F3FD88", "F2BE80", "F2BFEC"]
    assert all(f.cls is FixtureClass.PERIMETER for f in r.fixtures)


def test_trunk_is_alias_for_wire_stable_uplight_class(tmp_path):
    p = write_roster(tmp_path, "T000,F2BDB4,trunk,,,,\n")
    (fixture,) = Roster.load(p).fixtures
    assert fixture.cls is FixtureClass.UPLIGHT


# ---- Roster.load errors: file, line, fix -----------------------------------

def assert_names_file_line_fix(excinfo, path, lineno, fix_fragment):
    msg = str(excinfo.value)
    assert str(path) in msg
    assert f"line {lineno}" in msg
    assert fix_fragment in msg


def test_load_bad_header(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("# c\nmac,class\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 2, HEADER.strip())


def test_load_missing_header(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("# only comments\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert str(p) in str(e.value)
    assert HEADER.strip() in str(e.value)


def test_load_wrong_column_count(tmp_path):
    p = write_roster(tmp_path, "B000,F2BDB4,downlight\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 3, "add the missing commas")


def test_load_bad_mac(tmp_path):
    p = write_roster(tmp_path, "B000,NOTHEX,downlight,,,,\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 3, "6 hex digits")


def test_load_duplicate_mac(tmp_path):
    p = write_roster(tmp_path, (
        "B000,F2BDB4,downlight,,,,\n"
        "B001,68:EE:8F:F2:BD:B4,downlight,,,,\n"
    ))
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 4, "delete one of the two rows")
    assert "line 3" in str(e.value)  # points at the first occurrence too


def test_load_duplicate_fixture_id(tmp_path):
    p = write_roster(tmp_path, (
        "B000,F2BDB4,downlight,,,,\n"
        "B000,F2BDC0,downlight,,,,\n"
    ))
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 4, "rename one of the two rows")


def test_load_unknown_class(tmp_path):
    p = write_roster(tmp_path, "B000,F2BDB4,floodlight,,,,\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(
        e, p, 3,
        "use one of: downlight, perimeter, uplight, chandelier, trunk"
    )


def test_load_partial_xyz(tmp_path):
    p = write_roster(tmp_path, "B000,F2BDB4,downlight,1.0,,3.0,\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 3, "all three of x,y,z or leave all three empty")


def test_load_non_numeric_xyz(tmp_path):
    p = write_roster(tmp_path, "B000,F2BDB4,downlight,1.0,two,3.0,\n")
    with pytest.raises(ValueError) as e:
        Roster.load(p)
    assert_names_file_line_fix(e, p, 3, "plain decimals")


# ---- tx_partition ----------------------------------------------------------

def big_roster_csv(tmp_path, n=40):
    rows = "".join(f"L{i:03d},{0xA00000 + i:06X},downlight,,,,\n" for i in range(n))
    return write_roster(tmp_path, rows)


def test_tx_partition_sorted_and_chunked(tmp_path):
    r = Roster.load(big_roster_csv(tmp_path, 40))
    chunks = r.tx_partition()
    assert [len(c) for c in chunks] == [18, 18, 4]
    flat = [f.mac for c in chunks for f in c]
    assert flat == sorted(flat)


def test_tx_partition_stable_across_reload(tmp_path):
    p = big_roster_csv(tmp_path, 40)

    def chunk_of(roster):
        return {
            f.mac: i for i, chunk in enumerate(roster.tx_partition()) for f in chunk
        }

    first = chunk_of(Roster.load(p))
    again = chunk_of(Roster.load(p))
    assert first == again  # same fixture -> same chunk for a given roster


def test_tx_partition_ignores_csv_row_order(tmp_path):
    # Reordering the rows on disk must not move fixtures between chunks.
    rows = [f"L{i:03d},{0xA00000 + i:06X},downlight,,,,\n" for i in range(40)]
    p1 = write_roster(tmp_path, "".join(rows), name="a.csv")
    p2 = write_roster(tmp_path, "".join(reversed(rows)), name="b.csv")
    part1 = [[f.mac for f in c] for c in Roster.load(p1).tx_partition()]
    part2 = [[f.mac for f in c] for c in Roster.load(p2).tx_partition()]
    assert part1 == part2


def test_tx_partition_empty():
    assert Roster([]).tx_partition() == []


# ---- from_registry ---------------------------------------------------------

def test_from_registry_excludes_bridge_and_uncommissioned(tmp_path):
    r = Roster.from_registry(write_registry(tmp_path))
    assert "F2BED4" not in r.by_mac  # role=serial_bridge
    assert "9E5AE8" not in r.by_mac  # status=enumerated, not commissioned
    assert [f.mac for f in r.fixtures] == ["9F2694", "F2BDB4", "F2BDC0"]
    assert all(f.cls is FixtureClass.DOWNLIGHT for f in r.fixtures)
    assert all(f.fixture_id is None for f in r.fixtures)


def test_from_registry_class_overrides(tmp_path):
    r = Roster.from_registry(
        write_registry(tmp_path),
        classes={"68:EE:8F:F2:BD:B4": "perimeter", "f2bdc0": "Uplight"},
    )
    assert r.by_mac["F2BDB4"].cls is FixtureClass.PERIMETER
    assert r.by_mac["F2BDC0"].cls is FixtureClass.UPLIGHT
    assert r.by_mac["9F2694"].cls is FixtureClass.DOWNLIGHT


def test_from_registry_bad_class_override(tmp_path):
    with pytest.raises(ValueError) as e:
        Roster.from_registry(
            write_registry(tmp_path), classes={"F2BDB4": "spotlight"}
        )
    msg = str(e.value)
    assert "spotlight" in msg
    assert "use one of: downlight, perimeter, uplight, chandelier" in msg


def test_from_registry_accepts_trunk_alias(tmp_path):
    r = Roster.from_registry(
        write_registry(tmp_path), classes={"F2BDB4": "trunk"}
    )
    assert r.by_mac["F2BDB4"].cls is FixtureClass.UPLIGHT


def test_playa_template_loads_empty():
    playa = Path(__file__).parent.parent / "config" / "roster-playa.csv"
    r = Roster.load(playa)
    assert r.fixtures == []


def test_chunk_size_is_18():
    assert TX_CHUNK_SIZE == 18
