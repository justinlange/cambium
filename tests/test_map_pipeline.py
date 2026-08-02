"""The whole mapping pipeline against make_sweep's known ground truth:
ingest -> align (anchors + Umeyama) -> assign -> export, asserting recovery."""

import json
import math

import pytest

from cambium.fakefleet.make_sweep import make_sweep
from cambium.fakefleet.runner import synthetic_fixtures
from cambium.mapping.align import apply_transform, fit_anchors, sanity_report, write_transform
from cambium.mapping.assign import assign, load_calibration, to_calibration
from cambium.mapping.export import (
    build_fixtures_map,
    load_authored,
    to_elliot_doc,
    validate_elliot_doc,
    write_json,
)
from cambium.mapping.ingest import ingest
from cambium.mapping.sweep import constellate_command, freeze_roster, load_frozen

CREATED = "2026-08-02T12:00:00Z"


def pipeline(tmp_path, *, dropout=None, noise=0.005):
    """make_sweep -> ingest -> align; returns (measured_doc, tf, truth, world)."""
    fixtures = synthetic_fixtures(10)
    sweep_dir = tmp_path / "sweep"
    make_sweep(fixtures, sweep_dir, created=CREATED, seed=3, noise_m=noise,
               dropout=dropout or [])
    truth = json.loads((sweep_dir / "truth.json").read_text())

    measured_doc = ingest(
        sweep_dir / "session", sweep_dir, tmp_path / "map/measured-points.json",
        created=CREATED,
    )
    measured = {
        e["sweep_index"]: e["xyz_camera"]
        for e in measured_doc["entries"]
        if e["xyz_camera"] is not None
    }
    anchor_ids = [i for i in (0, 4, 9) if i in measured]
    anchors = [
        {"index": i, "world": truth["world_points"][str(i)]} for i in anchor_ids
    ]
    fit = fit_anchors(anchors, measured, with_scale=False)
    write_transform(
        fit, tmp_path / "map/transform.json", created=CREATED,
        session_ref=str(sweep_dir / "session"), scaled_input=True,
    )
    world = {
        e["mac"]: apply_transform(fit, e["xyz_camera"])
        for e in measured_doc["entries"]
        if e["xyz_camera"] is not None
    }
    return measured_doc, fit, truth, world


def test_align_recovers_world_positions(tmp_path):
    measured_doc, fit, truth, world = pipeline(tmp_path)
    assert not sanity_report(fit, scaled_input=True)
    for e in measured_doc["entries"]:
        if e["xyz_camera"] is None:
            continue
        got = world[e["mac"]]
        want = truth["world_points"][str(e["sweep_index"])]
        assert math.dist(got, want) < 0.05  # 5 cm at 5 mm noise


def test_transform_versioning_appends(tmp_path):
    _, fit, _, _ = pipeline(tmp_path)
    # A refit does not destroy the previous transform.
    write_transform(
        fit, tmp_path / "map/transform.json", created=CREATED,
        session_ref="again", scaled_input=True,
    )
    assert (tmp_path / "map/transform.1.json").exists()
    assert (tmp_path / "map/transform.json").exists()


def test_bad_anchor_is_named_by_leave_one_out(tmp_path):
    measured_doc, _, truth, _ = pipeline(tmp_path)
    measured = {
        e["sweep_index"]: e["xyz_camera"]
        for e in measured_doc["entries"] if e["xyz_camera"] is not None
    }
    anchors = [
        {"index": i, "world": truth["world_points"][str(i)]} for i in (0, 3, 6, 9)
    ]
    anchors[2]["world"] = [50.0, -20.0, 3.0]  # the fat-fingered tape entry
    fit = fit_anchors(anchors, measured, with_scale=False)
    warnings = sanity_report(fit, scaled_input=True)
    assert warnings and "index 6" in " ".join(w for w in warnings)


def test_dropout_flows_through_as_unmapped_and_fallback(tmp_path):
    measured_doc, fit, truth, world = pipeline(tmp_path, dropout=[7])
    unmapped = [e for e in measured_doc["entries"] if e["status"] == "unmapped"]
    assert [e["sweep_index"] for e in unmapped] == [7]

    # Authored layout = the truth positions, so assignment is trivial.
    authored_fixtures = {
        f"B{i:03d}": {
            "fixture_id": f"B{i:03d}", "role": "downlight", "zone": "low",
            "position": truth["world_points"][str(i)], "beam_deg": 120,
        }
        for i in range(10)
    }
    authored_pts = {k: v["position"] for k, v in authored_fixtures.items()}
    assignments = assign(world, authored_pts)
    fmap = build_fixtures_map(
        measured_doc, world, assignments, authored_fixtures,
        created=CREATED, sources={},
    )
    by_mac = {f["mac"]: f for f in fmap["fixtures"]}
    dropped = next(f for f in fmap["fixtures"] if f["sweep_index"] == 7)
    assert dropped["status"] == "unmapped"

    doc = to_elliot_doc(fmap, authored_fixtures, created=CREATED)
    validate_elliot_doc(doc)
    statuses = {f["mac"]: f["cambium_status"] for f in doc["fixtures"]}
    assert list(statuses.values()).count("authored-fallback") == 1
    # strict mode omits the hole instead
    strict = to_elliot_doc(fmap, authored_fixtures, created=CREATED, strict=True)
    assert strict["meta"]["count"] == 9


def test_assignment_positions_match_authored_slots(tmp_path):
    measured_doc, fit, truth, world = pipeline(tmp_path)
    authored_pts = {
        f"B{i:03d}": truth["world_points"][str(i)] for i in range(10)
    }
    assignments = assign(world, authored_pts)
    # Every mac lands on the slot whose truth position it was generated from:
    # synthetic macs FA0000.. are index order, which is also mac order.
    for i, e in enumerate(measured_doc["entries"]):
        assert assignments[e["mac"]]["fixture_id"] == f"B{i:03d}"


def test_assign_ambiguity_and_overrides():
    measured = {"AAAAAA": [0.0, 0.0, 0.0], "BBBBBB": [5.0, 0.0, 0.0]}
    authored = {"F1": [0.05, 0.0, 0.0], "F2": [0.06, 0.0, 0.0], "F3": [5.0, 0.0, 0.0]}
    a = assign(measured, authored)
    # AAAAAA sits between two coincident-ish slots -> ambiguous, unassigned
    assert a["AAAAAA"]["fixture_id"] is None
    assert set(a["AAAAAA"]["ambiguous_with"]) == {"F1", "F2"}
    assert a["BBBBBB"]["fixture_id"] == "F3"
    # An override resolves it and always wins
    a2 = assign(measured, authored, overrides=[{"mac": "AAAAAA", "fixture_id": "F2"}])
    assert a2["AAAAAA"] == {
        "fixture_id": "F2", "method": "manual", "confidence": 1.0,
        "distance_m": None, "ambiguous_with": None,
    }


def test_calibration_roundtrip(tmp_path):
    assignments = {
        "AAAAAA": {"fixture_id": "B001", "method": "nn", "confidence": 0.9,
                   "distance_m": 0.1, "ambiguous_with": None},
        "BBBBBB": {"fixture_id": "B002", "method": "manual", "confidence": 1.0,
                   "distance_m": None, "ambiguous_with": None},
    }
    cal = to_calibration(assignments, at=CREATED)
    assert cal["version"] == 2
    stages = {e["mac"]: e["stage"] for e in cal["entries"]}
    assert stages == {"AAAAAA": "hypothesis", "BBBBBB": "confirmed"}

    path = tmp_path / "cal.json"
    write_json(cal, path)
    confirmed = load_calibration(path)
    assert confirmed == {"BBBBBB": "B002"}  # hypotheses are NOT truth yet


def test_ingest_scale_gate(tmp_path):
    fixtures = synthetic_fixtures(4)
    sweep_dir = tmp_path / "s"
    make_sweep(fixtures, sweep_dir, created=CREATED)
    session = sweep_dir / "session"
    # Strip the tape measurement -> refusal names the fix
    sj = session / "session.json"
    doc = json.loads(sj.read_text())
    del doc["scale_measure"]
    sj.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="ARBITRARY UNITS"):
        ingest(session, sweep_dir, tmp_path / "m.json", created=CREATED)
    # --allow-unscaled proceeds, recorded as unscaled
    out = ingest(session, sweep_dir, tmp_path / "m.json", created=CREATED,
                 allow_unscaled=True)
    assert out["provenance"]["scaled"] is False


def test_ingest_bare_points_and_wrong_sweep(tmp_path):
    fixtures = synthetic_fixtures(4)
    sweep_dir = tmp_path / "s"
    make_sweep(fixtures, sweep_dir, created=CREATED)
    points = sweep_dir / "session/export/points.json"
    with pytest.raises(ValueError, match="assert-scaled"):
        ingest(points, sweep_dir, tmp_path / "m.json", created=CREATED)
    ingest(points, sweep_dir, tmp_path / "m.json", created=CREATED,
           assert_scaled=True)
    # An export with indexes beyond the roster = wrong sweep pairing
    points.write_text(json.dumps({"99": [0, 0, 0]}))
    with pytest.raises(ValueError, match="DIFFERENT sweep"):
        ingest(points, sweep_dir, tmp_path / "m2.json", created=CREATED,
               assert_scaled=True)


def test_freeze_roster_immutable_and_command(tmp_path):
    from cambium.roster import Roster

    roster = Roster(synthetic_fixtures(5))
    freeze_roster(roster, tmp_path / "sw", created=CREATED, roster_path=None)
    with pytest.raises(FileExistsError, match="immutable"):
        freeze_roster(roster, tmp_path / "sw", created=CREATED)
    doc = load_frozen(tmp_path / "sw")
    assert [e["mac"] for e in doc["entries"]] == sorted(
        e["mac"] for e in doc["entries"]
    )
    cmd = constellate_command(tmp_path / "sw")
    assert "--leds 5" in cmd and "constellate/light?led={led}" in cmd
