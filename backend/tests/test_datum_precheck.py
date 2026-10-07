import pytest

from app.services.datum_precheck import (
    VERDICT_COMPATIBLE,
    VERDICT_CONFLICT,
    VERDICT_DATUMLESS,
    VERDICT_EXISTING_INCONSISTENCY,
    precheck_candidate_datum,
)

POINTS = [
    {"id": 1, "code": "A"},
    {"id": 2, "code": "B"},
    {"id": 3, "code": "C"},
    {"id": 4, "code": "D"},
    {"id": 5, "code": "E"},
]
# Component {A,B,C} chain 1-2-3; component {D,E} edge 4-5.
OBSERVATIONS = [
    {"id": 10, "from_point_id": 1, "to_point_id": 2, "observed_delta_m": 1.0, "distance_m": 1000},
    {"id": 11, "from_point_id": 2, "to_point_id": 3, "observed_delta_m": 1.0, "distance_m": 1000},
    {"id": 12, "from_point_id": 4, "to_point_id": 5, "observed_delta_m": 2.0, "distance_m": 1000},
]
RULE = {"method": "distance_inverse_km", "c_km": 1.0, "base_sigma_m": 0.001}
SIGMA = 0.001


def _run(*, candidate_id, elevation, sigma=SIGMA, datums=None, rule=RULE):
    return precheck_candidate_datum(
        points=POINTS,
        observations=OBSERVATIONS,
        datums=datums or [],
        weight_rule=rule,
        candidate_point_id=candidate_id,
        elevation_m=elevation,
        sigma_m=sigma,
    )


def test_datumless_component_can_be_filled():
    # D,E component has no datum; a candidate there fills the vertical reference.
    result = _run(candidate_id=4, elevation=50.0, datums=[{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": SIGMA}])
    assert result["verdict"] == VERDICT_DATUMLESS
    assert result["component"]["datum_count"] == 0
    assert result["component"]["point_count"] == 2
    assert result["component"]["observation_count"] == 1
    assert result["existing_datums"] == []
    assert result["conflicts"] == []
    assert result["diagnostic"]["formal_status_if_solved_now"] == "blocked_rank_deficient"
    # Read-only diagnostic, never a formal solve.
    assert result["diagnostic"]["formal_solve"] is False
    assert result["diagnostic"]["persisted"] is False


def test_compatible_datum_in_datumed_component():
    # A fixed at 100 with 1 m steps implies C = 102.
    datums = [{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": SIGMA}]
    result = _run(candidate_id=3, elevation=102.0, datums=datums)
    assert result["verdict"] == VERDICT_COMPATIBLE
    assert len(result["existing_datums"]) == 1
    assert result["existing_datums"][0]["point_code"] == "A"
    assert abs(result["diagnostic"]["residual_m"]) < 3 * SIGMA


def test_network_implied_conflict_is_flagged_not_saved():
    datums = [{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": SIGMA}]
    result = _run(candidate_id=3, elevation=110.0, datums=datums)
    assert result["verdict"] == VERDICT_CONFLICT
    assert result["risk_level"] == "danger"
    conflict = next(c for c in result["conflicts"] if c["kind"] == "network_implied_elevation")
    assert conflict["implied_m"] == pytest.approx(102.0, abs=1e-9)
    assert conflict["residual_m"] == pytest.approx(8.0, abs=1e-9)
    assert result["diagnostic"]["persisted"] is False


def test_same_point_duplicate_datum_conflict():
    datums = [{"id": 1, "point_id": 2, "elevation_m": 101.0, "sigma_m": SIGMA}]
    result = _run(candidate_id=2, elevation=101.5, datums=datums)
    assert result["verdict"] == VERDICT_CONFLICT
    assert any(c["kind"] == "same_point_datum" for c in result["conflicts"])


def test_existing_datum_contradiction_blocks_new_proposal():
    # A=100 and C=110 already contradict each other through the 1-2-3 chain.
    datums = [
        {"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": SIGMA},
        {"id": 2, "point_id": 3, "elevation_m": 110.0, "sigma_m": SIGMA},
    ]
    result = _run(candidate_id=2, elevation=101.0, datums=datums)
    assert result["verdict"] == VERDICT_EXISTING_INCONSISTENCY
    assert result["risk_level"] == "danger"
    assert result["diagnostic"]["baseline_status"] != "ok"


def test_isolated_point_with_no_datum_is_datumless():
    points = POINTS + [{"id": 6, "code": "F"}]
    result = precheck_candidate_datum(
        points=points,
        observations=OBSERVATIONS,
        datums=[{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": SIGMA}],
        weight_rule=RULE,
        candidate_point_id=6,
        elevation_m=7.0,
        sigma_m=SIGMA,
    )
    assert result["verdict"] == VERDICT_DATUMLESS
    assert result["component"]["point_count"] == 1
    assert result["component"]["observation_count"] == 0


def test_missing_weight_rule_is_indeterminate_for_datumed_component():
    datums = [{"id": 1, "point_id": 1, "elevation_m": 100.0, "sigma_m": SIGMA}]
    result = _run(candidate_id=3, elevation=102.0, datums=datums, rule=None)
    assert result["verdict"] == "indeterminate"
    assert result["risk_level"] == "warning"
