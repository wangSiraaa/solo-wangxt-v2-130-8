"""Datum placement precheck: component location, contradiction screening, and
the draft-version guard that keeps a stale precheck from being confirmed."""
import pytest

from app.models.schema import Project
from app.services.datum_precheck import precheck_datum_candidate
from app.services.snapshots import StaleDraftError, bump_project_draft, ensure_draft_version

RULE = {"method": "millimeter_sqrt_km", "c_km": 1.0, "base_sigma_m": 0.001}


def _draft():
    # Component 0: BM-A -- P1 -- P2 (datum on BM-A). Component 1: P3 -- P4 (no datum).
    points = [
        {"id": 1, "code": "BM-A"},
        {"id": 2, "code": "P1"},
        {"id": 3, "code": "P2"},
        {"id": 4, "code": "P3"},
        {"id": 5, "code": "P4"},
    ]
    observations = [
        {"id": 1, "from_point_id": 1, "to_point_id": 2, "observed_delta_m": 1.001, "distance_m": 1000, "weight_override": None},
        {"id": 2, "from_point_id": 2, "to_point_id": 3, "observed_delta_m": 1.000, "distance_m": 1000, "weight_override": None},
        {"id": 3, "from_point_id": 4, "to_point_id": 5, "observed_delta_m": 0.5, "distance_m": 1000, "weight_override": None},
    ]
    datums = [{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": 0.0001}]
    return points, observations, datums


def test_candidate_in_datumless_component_fills_datum_gap():
    points, observations, datums = _draft()
    report = precheck_datum_candidate(
        points=points, observations=observations, datums=datums,
        candidate_point_id=4, elevation_m=50.0, sigma_m=0.001, rule=RULE,
    )
    assert report["assessment"] == "fills_datum_gap"
    assert report["component"]["datum_count"] == 0
    assert report["component"]["point_count"] == 2
    assert report["existing_datums"] == []
    assert report["conflicts"] == []


def test_consistent_candidate_is_reported_against_existing_datums():
    points, observations, datums = _draft()
    report = precheck_datum_candidate(
        points=points, observations=observations, datums=datums,
        candidate_point_id=3, elevation_m=102.001, sigma_m=0.001, rule=RULE,
    )
    assert report["assessment"] == "consistent"
    assert report["component"]["index"] == 0
    assert report["component"]["datum_count"] == 1
    assert len(report["existing_datums"]) == 1
    check = report["existing_datums"][0]
    assert check["implied_candidate_elevation_m"] == pytest.approx(102.001, abs=1e-9)
    assert check["within_tolerance"] is True
    assert report["conflicts"] == []


def test_contradictory_candidate_is_flagged_as_risk_without_saving():
    points, observations, datums = _draft()
    # BM-A=100.0 implies ~101.001 at P1; declaring 105.0 contradicts by metres.
    report = precheck_datum_candidate(
        points=points, observations=observations, datums=datums,
        candidate_point_id=2, elevation_m=105.0, sigma_m=0.001, rule=RULE,
    )
    assert report["assessment"] == "contradiction_risk"
    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["datum_id"] == 1
    assert conflict["discrepancy_m"] == pytest.approx(105.0 - 101.001, abs=1e-9)
    assert conflict["within_tolerance"] is False
    # The precheck is a pure read-only screening: the draft it was given is
    # untouched and no datum was added.
    assert datums == [{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": 0.0001}]


def test_precheck_is_deterministic_for_same_draft():
    points, observations, datums = _draft()
    first = precheck_datum_candidate(
        points=points, observations=observations, datums=datums,
        candidate_point_id=3, elevation_m=102.0, sigma_m=0.001, rule=RULE,
    )
    second = precheck_datum_candidate(
        points=points, observations=observations, datums=datums,
        candidate_point_id=3, elevation_m=102.0, sigma_m=0.001, rule=RULE,
    )
    assert first == second


def test_isolated_point_component_reported_as_isolated():
    points, observations, datums = _draft()
    points.append({"id": 6, "code": "ISO"})
    report = precheck_datum_candidate(
        points=points, observations=observations, datums=datums,
        candidate_point_id=6, elevation_m=10.0, sigma_m=0.001, rule=RULE,
    )
    assert report["assessment"] == "fills_datum_gap"
    assert report["component"]["isolated"] is True
    assert report["component"]["observation_count"] == 0


def test_ensure_draft_version_rejects_stale_confirm_with_409():
    project = Project(code="P", name="p", lock_version=3)
    ensure_draft_version(project, 3)  # current version passes
    with pytest.raises(StaleDraftError) as excinfo:
        ensure_draft_version(project, 2)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"] == "optimistic_lock_conflict"
    assert excinfo.value.detail["expected_lock_version"] == 2
    assert excinfo.value.detail["actual_lock_version"] == 3


def test_bump_project_draft_increments_and_audits():
    added = []

    class FakeSession:
        def add(self, obj):
            added.append(obj)

    project = Project(code="P", name="p", lock_version=3)
    bump_project_draft(FakeSession(), project, reason="update_observation:7")
    assert project.lock_version == 4
    assert len(added) == 1
    event = added[0]
    assert event.entity_type == "project"
    assert event.action == "draft_revision"
    assert event.lock_version_in == 3
    assert event.lock_version_out == 4


def test_stale_precheck_confirm_flow_is_refused():
    """Acceptance: precheck at draft v3, another revision moves the draft to
    v4, and confirming with the precheck's version is a 409 conflict."""
    added = []

    class FakeSession:
        def add(self, obj):
            added.append(obj)

    project = Project(code="P", name="p", lock_version=3)
    precheck_version = int(project.lock_version)
    # Someone else revises the draft after the precheck was issued.
    bump_project_draft(FakeSession(), project, reason="update_observation:9")
    with pytest.raises(StaleDraftError) as excinfo:
        ensure_draft_version(project, precheck_version)
    assert excinfo.value.status_code == 409
