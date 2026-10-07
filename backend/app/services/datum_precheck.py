"""Read-only datum placement precheck against the current draft topology.

Given a candidate (point, elevation, sigma), the precheck locates the connected
component the point belongs to and screens the candidate against the component's
known datums: each datum implies an elevation at the candidate point by
propagating observed height differences along a deterministic spanning tree,
and a discrepancy beyond 3 sigma is flagged as a contradiction risk.

This is a screening diagnostic only. It never writes to the database, never
produces adjusted elevations, and never replaces the solve stage's weighted
least-squares datum-contradiction analysis. Confirming a datum still goes
through the optimistic-lock revision API.
"""
from __future__ import annotations

import math
from typing import Any

from app.services.network import WeightParams, build_components, observation_sigma

# Same 3-sigma rule the solve stage applies to weighted datum residuals.
CONTRADICTION_SIGMA = 3.0

ASSESSMENT_FILLS_DATUM_GAP = "fills_datum_gap"
ASSESSMENT_CONSISTENT = "consistent"
ASSESSMENT_CONTRADICTION_RISK = "contradiction_risk"


def _weight_params(rule: dict[str, Any] | None) -> WeightParams:
    rule = rule or {}
    return WeightParams(
        method=str(rule.get("method", "millimeter_sqrt_km")),
        c_km=float(rule.get("c_km", 1.0)),
        base_sigma_m=float(rule.get("base_sigma_m", 0.001)),
    )


def precheck_datum_candidate(
    *,
    points: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    datums: list[dict[str, Any]],
    candidate_point_id: int,
    elevation_m: float,
    sigma_m: float,
    rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Locate the candidate's component and screen it against known datums.

    ``points``/``observations``/``datums`` are the serialized *current draft*
    (active rows only), identical in shape to the snapshot payload. The result
    is deterministic for a given draft and carries no adjusted elevations.
    """
    params = _weight_params(rule)
    candidate_point_id = int(candidate_point_id)
    point_index = {int(p["id"]): i for i, p in enumerate(points)}
    if candidate_point_id not in point_index:
        raise KeyError(f"point {candidate_point_id} not in draft")

    components = build_components(points, observations)
    target_local = point_index[candidate_point_id]
    component_index = next(i for i, comp in enumerate(components) if target_local in comp)
    component = components[component_index]
    component_point_ids = {int(points[i]["id"]) for i in component}
    component_observations = [
        obs
        for obs in observations
        if int(obs["from_point_id"]) in component_point_ids and int(obs["to_point_id"]) in component_point_ids
    ]
    component_datums = [d for d in datums if int(d["point_id"]) in component_point_ids]

    # Deterministic spanning tree rooted at the candidate point: observations
    # are visited in id order, so the implied elevations are reproducible for
    # the same draft. potential[p] = h[p] - h[candidate]; path_variance[p]
    # accumulates observation variance along the tree path from the candidate.
    adjacency: dict[int, list[tuple[int, float, float]]] = {}
    for obs in sorted(component_observations, key=lambda o: int(o["id"])):
        a, b = int(obs["from_point_id"]), int(obs["to_point_id"])
        sigma = observation_sigma(float(obs["distance_m"]), obs.get("weight_override"), params)
        delta = float(obs["observed_delta_m"])
        adjacency.setdefault(a, []).append((b, delta, sigma))
        adjacency.setdefault(b, []).append((a, -delta, sigma))

    potential: dict[int, float] = {candidate_point_id: 0.0}
    path_variance: dict[int, float] = {candidate_point_id: 0.0}
    stack = [candidate_point_id]
    while stack:
        u = stack.pop()
        for v, signed_delta, obs_sigma in adjacency.get(u, []):
            if v in potential:
                continue
            potential[v] = potential[u] + signed_delta
            path_variance[v] = path_variance[u] + obs_sigma * obs_sigma
            stack.append(v)

    candidate_sigma = max(float(sigma_m), 1e-9)
    datum_checks: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for datum in sorted(component_datums, key=lambda d: int(d["id"])):
        datum_point = int(datum["point_id"])
        # h[candidate] implied by this datum = H_datum - (h[datum] - h[candidate]).
        implied = float(datum["elevation_m"]) - potential[datum_point]
        variance = (
            max(float(datum.get("sigma_m", 0.001)), 1e-9) ** 2
            + candidate_sigma**2
            + path_variance[datum_point]
        )
        tolerance = CONTRADICTION_SIGMA * math.sqrt(variance)
        discrepancy = float(elevation_m) - implied
        check = {
            "datum_id": int(datum["id"]),
            "point_id": datum_point,
            "declared_elevation_m": float(datum["elevation_m"]),
            "sigma_m": float(datum.get("sigma_m", 0.001)),
            "implied_candidate_elevation_m": implied,
            "discrepancy_m": discrepancy,
            "tolerance_m": tolerance,
            "within_tolerance": abs(discrepancy) <= tolerance,
        }
        datum_checks.append(check)
        if not check["within_tolerance"]:
            conflicts.append(check)

    isolated = not component_observations
    if conflicts:
        assessment = ASSESSMENT_CONTRADICTION_RISK
    elif component_datums:
        assessment = ASSESSMENT_CONSISTENT
    else:
        # A connected component without any datum has a vertical datum defect;
        # this candidate would be its first datum.
        assessment = ASSESSMENT_FILLS_DATUM_GAP

    return {
        "assessment": assessment,
        "component": {
            "index": component_index,
            "point_count": len(component),
            "observation_count": len(component_observations),
            "datum_count": len(component_datums),
            "isolated": isolated,
        },
        "existing_datums": datum_checks,
        "conflicts": conflicts,
        "datum_already_on_point": any(int(d["point_id"]) == candidate_point_id for d in component_datums),
    }
