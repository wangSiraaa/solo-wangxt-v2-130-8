"""Read-only precheck for a proposed datum.

Before a surveyor confirms a new datum, the project manager needs to know:

1. which connected component of the *current draft* the candidate point lands in;
2. which known datums already constrain that component;
3. whether the proposed elevation is obviously inconsistent with those datums.

Everything here is read-only: it evaluates the draft topology in memory and
returns a verdict. It never creates a datum, never writes a snapshot and never
produces a publishable "solution". Confirmation still goes through the normal
optimistic-lock revision API, so a draft changed by someone else after the
precheck is rejected with 409.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from app.services import network

# Verdict vocabulary exposed to the UI.
VERDICT_DATUMLESS = "datumless_can_add"
VERDICT_COMPATIBLE = "compatible"
VERDICT_CONFLICT = "conflict"
VERDICT_EXISTING_INCONSISTENCY = "existing_inconsistency"
VERDICT_INDETERMINATE = "indeterminate"

RISK_INFO = "info"
RISK_WARNING = "warning"
RISK_DANGER = "danger"


def _point_code(points: list[dict[str, Any]], point_id: int) -> str | None:
    for point in points:
        if int(point["id"]) == point_id:
            return str(point.get("code"))
    return None


def locate_component(
    points: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    target_point_id: int,
) -> tuple[int, list[int], list[tuple[int, dict[str, Any]]]]:
    """Return ``(component_index, local_indices, component_observations)``.

    Isolated points (no incident observation) still form a singleton component.
    """
    components = network.build_components(points, observations)
    point_to_component: dict[int, int] = {}
    for index, local_indices in enumerate(components):
        for local_index in local_indices:
            point_to_component[int(points[local_index]["id"])] = index

    component_index = point_to_component.get(int(target_point_id))
    if component_index is None:
        raise KeyError(target_point_id)
    local_indices = components[component_index]
    component_point_ids = {int(points[i]["id"]) for i in local_indices}
    component_observations = [
        (source_index, observation)
        for source_index, observation in enumerate(observations)
        if int(observation["from_point_id"]) in component_point_ids
        and int(observation["to_point_id"]) in component_point_ids
    ]
    return component_index, local_indices, component_observations


def _component_weights(
    component_observations: list[tuple[int, dict[str, Any]]],
    observations: list[dict[str, Any]],
    weight_rule: dict[str, Any] | None,
) -> np.ndarray | None:
    if weight_rule is None:
        return None
    source_indices = [source_index for source_index, _obs in component_observations]
    return network.compute_weights([observations[i] for i in source_indices], weight_rule)


def precheck_candidate_datum(
    *,
    points: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    datums: list[dict[str, Any]],
    weight_rule: dict[str, Any] | None,
    candidate_point_id: int,
    elevation_m: float,
    sigma_m: float,
    rank_tol: float = 1e-9,
) -> dict[str, Any]:
    """Evaluate a proposed datum against the live draft without persisting it."""
    component_index, local_indices, component_observations = locate_component(
        points, observations, candidate_point_id
    )
    component_point_ids = {int(points[i]["id"]) for i in local_indices}
    component_datums = [
        datum
        for datum in datums
        if int(datum["point_id"]) in component_point_ids and datum.get("active", True)
    ]
    existing_payload = [
        {
            "id": datum.get("id"),
            "point_id": int(datum["point_id"]),
            "point_code": _point_code(points, int(datum["point_id"])),
            "elevation_m": float(datum["elevation_m"]),
            "sigma_m": float(datum.get("sigma_m", 0.001)),
        }
        for datum in component_datums
    ]
    component_summary = {
        "index": component_index,
        "point_count": len(component_point_ids),
        "observation_count": len(component_observations),
        "datum_count": len(component_datums),
        "sample_points": [
            _point_code(points, int(points[i]["id"])) or str(points[i]["id"]) for i in local_indices[:20]
        ],
    }

    candidate = {
        "point_id": int(candidate_point_id),
        "point_code": _point_code(points, int(candidate_point_id)),
        "elevation_m": float(elevation_m),
        "sigma_m": float(sigma_m),
    }
    conflicts: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] = {
        "evaluation": "read_only_diagnostic",
        "formal_solve": False,
        "persisted": False,
    }

    # A component with no known datum is vertically rank deficient today; adding
    # the candidate datum is exactly how it gets its vertical reference.
    if not component_datums:
        diagnostic["reason"] = "component_has_no_datum"
        diagnostic["formal_status_if_solved_now"] = "blocked_rank_deficient"
        return {
            "candidate": candidate,
            "component": component_summary,
            "existing_datums": [],
            "verdict": VERDICT_DATUMLESS,
            "risk_level": RISK_INFO,
            "conflicts": [],
            "message": "所属连通分量当前没有基准，求解会因垂直基准缺失而阻塞；该候选点可补基准。",
            "diagnostic": diagnostic,
        }

    component_weights = _component_weights(component_observations, observations, weight_rule)
    if component_weights is None:
        diagnostic["reason"] = "no_active_weight_rule"
        return {
            "candidate": candidate,
            "component": component_summary,
            "existing_datums": existing_payload,
            "verdict": VERDICT_INDETERMINATE,
            "risk_level": RISK_WARNING,
            "conflicts": [],
            "message": "缺少活动权重规则，无法评估候选基准与既有基准是否矛盾；仍可先做同点比对。",
            "diagnostic": diagnostic,
        }

    # Same-point check: a datum already exists at the candidate point.
    same_point = [d for d in component_datums if int(d["point_id"]) == int(candidate_point_id)]
    for existing in same_point:
        existing_elevation = float(existing["elevation_m"])
        existing_sigma = float(existing.get("sigma_m", 0.001))
        residual = float(elevation_m) - existing_elevation
        threshold = 3.0 * float(np.hypot(float(sigma_m), existing_sigma))
        if abs(residual) > threshold:
            conflicts.append(
                {
                    "kind": "same_point_datum",
                    "point_id": int(candidate_point_id),
                    "point_code": candidate["point_code"],
                    "existing_datum_id": existing.get("id"),
                    "declared_m": float(elevation_m),
                    "existing_m": existing_elevation,
                    "residual_m": residual,
                    "threshold_m": threshold,
                    "sigma_m": float(sigma_m),
                }
            )

    # First check the component is already internally consistent without the
    # candidate. The formal solve runs the same math; if it blocks today the
    # manager must resolve existing contradictions rather than add more datums.
    baseline = network.solve_component(
        local_indices,
        points,
        component_observations,
        component_datums,
        component_weights,
        ill_threshold=1e12,
        rank_tol=rank_tol,
        dense_qr_max_rows=20_000,
        estimate_condition=False,
    )
    diagnostic["baseline_status"] = baseline["status"]
    diagnostic["baseline_method"] = baseline["method"]
    if baseline["status"] != "ok" or baseline.get("x") is None:
        diagnostics = baseline.get("diagnostics", {})
        existing_contradictions = diagnostics.get("datum_contradictions", [])
        diagnostic["existing_datum_contradictions"] = existing_contradictions
        message = "所属分量的既有基准已经互相矛盾，正式求解当前会被阻塞；请先处理现有冲突，而不是叠加新基准。"
        if "rank" in baseline["status"]:
            message = "所属分量基准配置秩亏，无法评估候选基准；请先修正既有基准。"
        elif baseline["status"] == "blocked_illconditioned":
            message = "所属分量病态，候选基准评估不可靠；请先检查观测与基准配置。"
        return {
            "candidate": candidate,
            "component": component_summary,
            "existing_datums": existing_payload,
            "verdict": VERDICT_EXISTING_INCONSISTENCY,
            "risk_level": RISK_DANGER,
            "conflicts": conflicts
            or [
                {
                    "kind": "existing_inconsistency",
                    "status": baseline["status"],
                    "contradictions": existing_contradictions,
                }
            ],
            "message": message,
            "diagnostic": diagnostic,
        }

    # Fix every *other* datum (excluding a datum already sitting on the candidate
    # point) and propagate elevations through the leveling network. The candidate
    # is checked against what the rest of the network independently implies —
    # identical leave-one-datum-out logic to the formal datum-contradiction guard.
    anchor_datums = [
        datum for datum in component_datums if int(datum["point_id"]) != int(candidate_point_id)
    ]
    if anchor_datums:
        implied = network.implied_elevations(
            local_indices, points, component_observations, anchor_datums, component_weights
        )
    else:
        # Only datum in the component is the one already on this point; nothing
        # independent can imply an elevation, so only the direct comparison above
        # is meaningful.
        implied = None
    if implied is not None and int(candidate_point_id) in implied:
        implied_m = float(implied[int(candidate_point_id)])
        residual = float(elevation_m) - implied_m
        threshold = 3.0 * max(float(sigma_m), rank_tol)
        diagnostic["implied_elevation_m"] = implied_m
        diagnostic["residual_m"] = residual
        diagnostic["threshold_m"] = threshold
        diagnostic["anchor_datum_count"] = len(anchor_datums)
        if abs(residual) > threshold:
            conflicts.append(
                {
                    "kind": "network_implied_elevation",
                    "point_id": int(candidate_point_id),
                    "point_code": candidate["point_code"],
                    "declared_m": float(elevation_m),
                    "implied_m": implied_m,
                    "residual_m": residual,
                    "threshold_m": threshold,
                    "sigma_m": float(sigma_m),
                    "anchor_datums": [
                        {
                            "point_id": int(d["point_id"]),
                            "point_code": _point_code(points, int(d["point_id"])),
                            "elevation_m": float(d["elevation_m"]),
                        }
                        for d in anchor_datums
                    ],
                }
            )

    if conflicts:
        return {
            "candidate": candidate,
            "component": component_summary,
            "existing_datums": existing_payload,
            "verdict": VERDICT_CONFLICT,
            "risk_level": RISK_DANGER,
            "conflicts": conflicts,
            "message": "候选基准与所属分量的既有基准/观测网明显矛盾（超过 3σ），正式求解将触发 blocked_datum_contradiction；预检未保存任何数据。",
            "diagnostic": diagnostic,
        }

    return {
        "candidate": candidate,
        "component": component_summary,
        "existing_datums": existing_payload,
        "verdict": VERDICT_COMPATIBLE,
        "risk_level": RISK_INFO,
        "conflicts": [],
        "message": "候选基准与所属分量既有约束相容，可提交确认；确认仍按乐观锁修订，预检结果不作为正式求解。",
        "diagnostic": diagnostic,
    }
