"""make_sweep: deterministic synthetic Constellate exports."""

import json
import math

import pytest

from cambium.fakefleet.make_sweep import make_sweep
from cambium.fakefleet.runner import synthetic_fixtures

CREATED = "2026-08-02T12:00:00Z"


def run(tmp_path, name="a", **kw):
    out = tmp_path / name
    paths = make_sweep(synthetic_fixtures(10), out, created=CREATED, **kw)
    return out, paths


def test_deterministic_bytes_for_same_seed(tmp_path):
    a, _ = run(tmp_path, "a", seed=7)
    b, _ = run(tmp_path, "b", seed=7)
    for rel in ("roster.json", "session/session.json", "session/export/points.json"):
        assert (a / rel).read_bytes() == (b / rel).read_bytes()


def test_different_seed_moves_points(tmp_path):
    a, _ = run(tmp_path, "a", seed=1)
    b, _ = run(tmp_path, "b", seed=2)
    pa = json.loads((a / "session/export/points.json").read_text())
    pb = json.loads((b / "session/export/points.json").read_text())
    assert pa != pb


def test_dropout_omits_indices(tmp_path):
    out, _ = run(tmp_path, dropout=[3, 7])
    points = json.loads((out / "session/export/points.json").read_text())
    assert "3" not in points and "7" not in points and "0" in points
    truth = json.loads((out / "truth.json").read_text())
    assert truth["dropout"] == [3, 7]
    # absent-not-wrong: the roster still lists every fixture
    roster = json.loads((out / "roster.json").read_text())
    assert len(roster["entries"]) == 10


def test_roster_index_is_mac_ascending(tmp_path):
    out, _ = run(tmp_path)
    roster = json.loads((out / "roster.json").read_text())
    macs = [e["mac"] for e in roster["entries"]]
    assert macs == sorted(macs)
    assert [e["index"] for e in roster["entries"]] == list(range(10))


def test_camera_frame_is_y_down(tmp_path):
    # OpenCV convention: +Y down. World up (+Z) must map to negative camera
    # Y -- pinned in the recorded transform, not just eyeballed from points.
    out, _ = run(tmp_path)
    truth = json.loads((out / "truth.json").read_text())
    assert truth["camera_from_world"]["R"][1][2] < -0.9


def test_scale_measure_matches_geometry(tmp_path):
    out, _ = run(tmp_path, noise_m=0.0)
    session = json.loads((out / "session/session.json").read_text())
    points = json.loads((out / "session/export/points.json").read_text())
    sm = session["scale_measure"]
    assert sm["kind"] == "led_pair" and (sm["a"], sm["b"]) == (0, 1)
    d = math.dist(points["0"], points["1"])
    # similarity preserves distances (scale 1.0, zero noise here)
    assert d == pytest.approx(sm["meters"], abs=1e-4)


def test_needs_two_placed_fixtures(tmp_path):
    with pytest.raises(ValueError, match="xyz"):
        make_sweep(synthetic_fixtures(1), tmp_path / "x", created=CREATED)
