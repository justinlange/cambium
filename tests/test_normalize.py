from pathlib import Path

import pytest

from cambium.model import Fixture, FixtureClass, RGBW
from cambium.normalize.color import clamp01, quantize8, white_extract
from cambium.normalize.frames import sim_to_canonical
from cambium.roster import Roster

BENCH10 = Path(__file__).parent.parent / "config" / "roster-bench10.csv"


# ---- clamp01 / quantize8 ---------------------------------------------------

def test_clamp01():
    assert clamp01(-0.5) == 0.0
    assert clamp01(0.0) == 0.0
    assert clamp01(0.25) == 0.25
    assert clamp01(1.0) == 1.0
    assert clamp01(1.7) == 1.0  # sim linear floats may exceed 1.0


def test_quantize8():
    assert quantize8(0.0) == 0
    assert quantize8(1.0) == 255
    assert quantize8(2.5) == 255  # clamp before quantize
    assert quantize8(-1.0) == 0
    assert quantize8(0.2) == 51


# ---- white_extract ---------------------------------------------------------

def test_subtract_rgbw_class():
    # w = min(r,g,b), subtracted from each channel -- hue-preserving.
    assert white_extract(200, 150, 100, FixtureClass.DOWNLIGHT, "subtract") == RGBW(
        100, 50, 0, 100
    )


def test_subtract_gray_goes_all_white():
    assert white_extract(80, 80, 80, FixtureClass.UPLIGHT, "subtract") == RGBW(
        0, 0, 0, 80
    )


def test_subtract_perimeter_no_white_hardware():
    # PERIMETER is GRB-only: w stays 0, rgb untouched, regardless of policy.
    assert white_extract(200, 150, 100, FixtureClass.PERIMETER, "subtract") == RGBW(
        200, 150, 100, 0
    )


def test_policy_none_never_extracts():
    assert white_extract(200, 150, 100, FixtureClass.DOWNLIGHT, "none") == RGBW(
        200, 150, 100, 0
    )
    assert white_extract(80, 80, 80, FixtureClass.PERIMETER, "none") == RGBW(
        80, 80, 80, 0
    )


def test_unknown_policy_names_the_fix():
    with pytest.raises(ValueError) as e:
        white_extract(1, 2, 3, FixtureClass.DOWNLIGHT, "subtractt")
    msg = str(e.value)
    assert "subtractt" in msg
    assert "white_extract" in msg and "subtract" in msg and "none" in msg


# ---- sim_to_canonical ------------------------------------------------------

@pytest.fixture
def bench10():
    return Roster.load(BENCH10)


def test_sim_to_canonical_resolves_and_extracts(bench10):
    frame, unknown = sim_to_canonical(
        [{"id": "B003", "rgb": [1.0, 1.0, 0.5]}], bench10, "subtract", seq=42
    )
    assert unknown == []
    assert frame.seq == 42  # seq is the caller's, passed through untouched
    # B003 -> F2BE38; (255, 255, 128) -> w=128 extracted
    assert frame.colors == {"F2BE38": RGBW(127, 127, 0, 128)}


def test_sim_to_canonical_clamps_wild_floats(bench10):
    frame, unknown = sim_to_canonical(
        [{"id": "B000", "rgb": [3.0, -0.5, 0.0]}], bench10, "none", seq=1
    )
    assert unknown == []
    assert frame.colors == {"9F2694": RGBW(255, 0, 0, 0)}


def test_sim_to_canonical_accepts_raw_mac_as_id(bench10):
    # Forgiving input: during mapping, fixtures have macs before labels.
    frame, unknown = sim_to_canonical(
        [
            {"id": "F2BDB4", "rgb": [0.0, 0.0, 0.0]},
            {"id": "f40174", "rgb": [0.0, 0.0, 0.0]},  # any case
        ],
        bench10,
        "subtract",
        seq=1,
    )
    assert unknown == []
    assert set(frame.colors) == {"F2BDB4", "F40174"}


def test_sim_to_canonical_collects_unknown_ids_never_raises(bench10):
    frame, unknown = sim_to_canonical(
        [
            {"id": "Z999", "rgb": [1.0, 0.0, 0.0]},    # no such label
            {"id": "ABCDEF", "rgb": [1.0, 0.0, 0.0]},  # hex, but not in roster
            {"id": "B001", "rgb": [1.0, 0.0, 0.0]},    # known -- still lands
        ],
        bench10,
        "subtract",
        seq=1,
    )
    assert unknown == ["Z999", "ABCDEF"]
    assert set(frame.colors) == {"F2BDB4"}


def test_sim_to_canonical_perimeter_class_gets_no_white():
    roster = Roster([Fixture("AA0001", "P000", FixtureClass.PERIMETER, None)])
    frame, unknown = sim_to_canonical(
        [{"id": "P000", "rgb": [0.5, 0.5, 0.5]}], roster, "subtract", seq=1
    )
    assert unknown == []
    c = frame.colors["AA0001"]
    assert c.w == 0 and c.r == c.g == c.b == quantize8(0.5)
