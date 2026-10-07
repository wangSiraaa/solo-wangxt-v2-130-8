from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    BulkImportIn,
    DatumCreateIn,
    DatumPrecheckIn,
    OptimisticDatumPatch,
    OptimisticObservationPatch,
    OptimisticRulePatch,
    ProjectIn,
    PublishIn,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.models.schema import (
    Datum,
    Job,
    JobStage,
    Observation,
    Point,
    Project,
    Publication,
    Snapshot,
    WeightRule,
    ObservationResult,
    ComponentResult,
)
from app.services import datum_precheck
from app.services.snapshots import (
    StaleDraftError,
    apply_optimistic_update,
    bump_project_draft,
    create_immutable_snapshot,
    ensure_single_generation,
)
from app.workers.tasks import build_pipeline

router = APIRouter(prefix="/api")


def _get(db: Session, model, entity_id: int):
    obj = db.get(model, entity_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {entity_id} not found")
    return obj


@router.post("/projects", status_code=201)
def create_project(payload: ProjectIn, db: Session = Depends(get_db)):
    project = Project(code=payload.code, name=payload.name)
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"project code {payload.code} already exists") from None
    db.refresh(project)
    return {"id": project.id, "code": project.code, "lock_version": project.lock_version}


@router.post("/projects/{project_id}/import", status_code=201)
def bulk_import(project_id: int, payload: BulkImportIn, db: Session = Depends(get_db)):
    project = _get(db, Project, project_id)
    existing = {p.code: p for p in db.scalars(select(Point).where(Point.project_id == project_id)).all()}
    created_points = 0
    for point in payload.points:
        if point.code not in existing:
            instance = Point(project_id=project_id, code=point.code, name=point.name)
            db.add(instance)
            created_points += 1
    db.flush()
    points = {p.code: p for p in db.scalars(select(Point).where(Point.project_id == project_id)).all()}
    from_codes = {obs.from_code for obs in payload.observations}
    to_codes = {obs.to_code for obs in payload.observations}
    # Parenthesize the union: set difference binds tighter than union, so the
    # unparenthesized form would parse as from_codes | (to_codes - known_codes).
    known_codes = set(points)
    missing = sorted((from_codes | to_codes) - known_codes)
    if missing:
        raise HTTPException(400, f"unknown point codes in observations: {missing[:20]}")
    observations = []
    for obs in payload.observations:
        observations.append(
            Observation(
                project_id=project_id,
                line_code=obs.line_code,
                from_point_id=points[obs.from_code].id,
                to_point_id=points[obs.to_code].id,
                observed_delta_m=obs.observed_delta_m,
                distance_m=obs.distance_m,
                direction=obs.direction,
                pair_group=obs.pair_group,
                weight_override=obs.weight_override,
            )
        )
    db.add_all(observations)
    new_version = bump_project_draft(
        db,
        project,
        action="bulk_import",
        entity_type="project",
        detail={"points_created": created_points, "observations_created": len(observations)},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "one or more line codes already exist in this project") from None
    return {
        "points_created": created_points,
        "observations_created": len(observations),
        "lock_version": new_version,
    }


def _draft_state(db: Session, project_id: int) -> tuple[Project, list, list, list, list | None]:
    project = _get(db, Project, project_id)
    points = db.scalars(select(Point).where(Point.project_id == project_id).order_by(Point.id)).all()
    observations = db.scalars(
        select(Observation)
        .where(Observation.project_id == project_id, Observation.active.is_(True))
        .order_by(Observation.id)
    ).all()
    datums = db.scalars(
        select(Datum).where(Datum.project_id == project_id, Datum.active.is_(True)).order_by(Datum.id)
    ).all()
    rule = db.scalar(
        select(WeightRule)
        .where(WeightRule.project_id == project_id, WeightRule.active.is_(True))
        .order_by(WeightRule.id)
    )
    return project, points, observations, datums, rule


def _draft_precheck_payload(db: Session, project_id: int, point_code: str, elevation_m: float, sigma_m: float):
    project, point_rows, observation_rows, datum_rows, rule_row = _draft_state(db, project_id)
    point = next((p for p in point_rows if p.code == point_code), None)
    if point is None:
        raise HTTPException(404, f"point {point_code} not found")
    settings = get_settings()
    result = datum_precheck.precheck_candidate_datum(
        points=[{"id": p.id, "code": p.code} for p in point_rows],
        observations=[
            {
                "id": o.id,
                "from_point_id": o.from_point_id,
                "to_point_id": o.to_point_id,
                "observed_delta_m": float(o.observed_delta_m),
                "distance_m": float(o.distance_m),
                "weight_override": None if o.weight_override is None else float(o.weight_override),
            }
            for o in observation_rows
        ],
        datums=[
            {"id": d.id, "point_id": d.point_id, "elevation_m": float(d.elevation_m), "sigma_m": float(d.sigma_m)}
            for d in datum_rows
        ],
        weight_rule=None if rule_row is None else rule_row.rule,
        candidate_point_id=point.id,
        elevation_m=elevation_m,
        sigma_m=sigma_m,
        rank_tol=settings.qr_rank_tol,
    )
    # The version pins the verdict to a specific draft; confirmation must present it.
    result["project_id"] = project_id
    result["draft_lock_version"] = project.lock_version
    return result


@router.post("/projects/{project_id}/datums/precheck")
def precheck_datum(project_id: int, payload: DatumPrecheckIn, db: Session = Depends(get_db)):
    """Read-only: locate the candidate's component and evaluate datum constraints.

    Never writes a datum, a snapshot or an audit revision. The returned
    ``draft_lock_version`` must be echoed on confirmation; if the draft changed
    in between, confirmation is rejected with 409.
    """
    return _draft_precheck_payload(db, project_id, payload.point_code, payload.elevation_m, payload.sigma_m)


@router.post("/projects/{project_id}/datums", status_code=201)
def add_datum(project_id: int, payload: DatumCreateIn, db: Session = Depends(get_db)):
    project = _get(db, Project, project_id)
    point = db.scalar(select(Point).where(Point.project_id == project_id, Point.code == payload.point_code))
    if point is None:
        raise HTTPException(404, f"point {payload.point_code} not found")
    # Confirmation still goes through the optimistic revision path. A precheck
    # against an older draft (or a concurrent edit) is rejected before anything
    # is written, never silently saved.
    if payload.lock_version is not None and int(project.lock_version) != payload.lock_version:
        raise StaleDraftError("project", payload.lock_version, int(project.lock_version))
    datum = Datum(
        project_id=project_id,
        point_id=point.id,
        elevation_m=payload.elevation_m,
        sigma_m=payload.sigma_m,
    )
    db.add(datum)
    db.flush()
    new_version = bump_project_draft(
        db,
        project,
        expected_version=payload.lock_version,
        action="create",
        entity_type="datum",
        entity_id=datum.id,
        detail={"point_code": payload.point_code, "elevation_m": payload.elevation_m, "sigma_m": payload.sigma_m},
    )
    db.commit()
    db.refresh(datum)
    return {"id": datum.id, "lock_version": datum.lock_version, "draft_lock_version": new_version}


@router.post("/projects/{project_id}/weight-rules", status_code=201)
def add_weight_rule(project_id: int, name: str, rule: dict, db: Session = Depends(get_db)):
    project = _get(db, Project, project_id)
    model = WeightRule(project_id=project_id, name=name, rule=rule)
    db.add(model)
    db.flush()
    new_version = bump_project_draft(
        db,
        project,
        action="create",
        entity_type="weightrule",
        entity_id=model.id,
        detail={"name": name},
    )
    db.commit()
    return {"id": model.id, "lock_version": model.lock_version, "draft_lock_version": new_version}


@router.patch("/observations/{observation_id}")
def patch_observation(observation_id: int, payload: OptimisticObservationPatch, db: Session = Depends(get_db)):
    obs = _get(db, Observation, observation_id)
    changes = payload.model_dump(exclude={"lock_version"}, exclude_none=True)
    apply_optimistic_update(db, obs, changes, expected_version=payload.lock_version)
    project = db.get(Project, obs.project_id)
    draft_version = bump_project_draft(
        db, project, action="update", entity_type="observation", entity_id=obs.id, detail=changes
    )
    db.commit()
    return {"id": obs.id, "lock_version": obs.lock_version, "draft_lock_version": draft_version}


@router.patch("/datums/{datum_id}")
def patch_datum(datum_id: int, payload: OptimisticDatumPatch, db: Session = Depends(get_db)):
    datum = _get(db, Datum, datum_id)
    changes = payload.model_dump(exclude={"lock_version"}, exclude_none=True)
    apply_optimistic_update(db, datum, changes, expected_version=payload.lock_version)
    project = db.get(Project, datum.project_id)
    draft_version = bump_project_draft(
        db, project, action="update", entity_type="datum", entity_id=datum.id, detail=changes
    )
    db.commit()
    return {"id": datum.id, "lock_version": datum.lock_version, "draft_lock_version": draft_version}


@router.patch("/weight-rules/{rule_id}")
def patch_weight_rule(rule_id: int, payload: OptimisticRulePatch, db: Session = Depends(get_db)):
    rule = _get(db, WeightRule, rule_id)
    changes = payload.model_dump(exclude={"lock_version"}, exclude_none=True)
    apply_optimistic_update(db, rule, changes, expected_version=payload.lock_version)
    project = db.get(Project, rule.project_id)
    draft_version = bump_project_draft(
        db, project, action="update", entity_type="weightrule", entity_id=rule.id, detail={"name": rule.name}
    )
    db.commit()
    return {"id": rule.id, "lock_version": rule.lock_version, "draft_lock_version": draft_version}


@router.post("/projects/{project_id}/jobs", status_code=202)
def submit_job(project_id: int, db: Session = Depends(get_db)):
    _get(db, Project, project_id)
    snapshot = create_immutable_snapshot(db, project_id)
    job, created = ensure_single_generation(db, project_id, snapshot.id)
    db.commit()
    if created:
        build_pipeline(job.id).apply_async()
    return {
        "job_id": job.id,
        "snapshot_id": snapshot.id,
        "snapshot_version": snapshot.version,
        "generation_key": job.generation_key,
        "deduplicated": not created,
    }


@router.post("/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, Job, job_id)
    stages = {s.name: s for s in job.stages}
    # Confirmed stages are skipped. Failed stages are retried; /resume also resets
    # RUNNING markers left by a killed worker.
    pipeline = build_pipeline(job.id)
    pipeline.apply_async()
    return {"job_id": job.id, "current_stage": job.current_stage, "confirmed": [n for n, s in stages.items() if s.status == "confirmed"]}


@router.get("/projects/{project_id}/topology")
def topology(project_id: int, db: Session = Depends(get_db)):
    points = db.scalars(select(Point).where(Point.project_id == project_id).order_by(Point.id)).all()
    obs = db.scalars(select(Observation).where(Observation.project_id == project_id).order_by(Observation.id)).all()
    point_index = {p.id: p.code for p in points}
    return {
        "nodes": [{"data": {"id": p.code, "label": p.code, "point_id": p.id}} for p in points],
        "edges": [
            {
                "data": {
                    "id": o.line_code,
                    "source": point_index[o.from_point_id],
                    "target": point_index[o.to_point_id],
                    "label": f"{float(o.observed_delta_m):.3f}",
                }
            }
            for o in obs
        ],
    }


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, Job, job_id)
    return {
        "id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "generation_key": job.generation_key,
        "snapshot_version": job.snapshot.version,
        "input_summary": job.snapshot.input_summary,
        "algorithm": job.snapshot.algorithm,
        "diagnostics": job.diagnostics,
        "stages": [
            {"name": s.name, "status": s.status, "attempt": s.attempt, "detail": s.detail}
            for s in sorted(db.scalars(select(JobStage).where(JobStage.job_id == job_id)).all(), key=lambda s: s.id)
        ],
    }


@router.get("/jobs/{job_id}/residuals")
def residuals(job_id: int, limit: int = 200, db: Session = Depends(get_db)):
    _get(db, Job, job_id)
    results = db.scalars(select(ObservationResult).where(ObservationResult.job_id == job_id).limit(limit)).all()
    return [
        {
            "line_code": r.line_code,
            "observed_delta_m": float(r.observed_delta_m),
            "adjusted_delta_m": None if r.adjusted_delta_m is None else float(r.adjusted_delta_m),
            "correction_m": None if r.correction_m is None else float(r.correction_m),
            "residual": None if r.residual_v is None else float(r.residual_v),
        }
        for r in results
    ]


@router.post("/jobs/{job_id}/publish", status_code=201)
def publish(job_id: int, payload: PublishIn, db: Session = Depends(get_db)):
    job = _get(db, Job, job_id)
    if not payload.confirm:
        raise HTTPException(400, "publication requires confirm=true")
    if str(job.status) != "completed":
        raise HTTPException(409, f"job is not publishable: {job.status}")
    stage_checks = {s.name: s.detail for s in db.scalars(select(JobStage).where(JobStage.job_id == job_id)).all()}
    if stage_checks.get("publish_checks", {}).get("regularization", {}).get("value") != "none":
        raise HTTPException(409, "regularization audit failed")

    current_snapshot_id = db.scalar(
        select(func.max(Snapshot.id)).where(Snapshot.project_id == job.project_id)
    )
    if current_snapshot_id != job.snapshot_id:
        # Old task may finish as audit history, but cannot overwrite a newer surveyor draft.
        raise HTTPException(409, "stale generation: rerun against current draft before publishing")

    elevations: dict[str, float] = {}
    for component in db.scalars(select(ComponentResult).where(ComponentResult.job_id == job_id)).all():
        if component.status != "ok":
            raise HTTPException(409, f"component {component.component_index} is {component.status}")
        elevations.update(component.elevations)
    # Component elevations key by point id; expose stable codes.
    points = {p.id: p.code for p in db.scalars(select(Point).where(Point.project_id == job.project_id)).all()}
    elevations = {points.get(int(pid), str(pid)): value for pid, value in elevations.items()}

    version = (db.scalar(select(func.max(Publication.version)).where(Publication.project_id == job.project_id)) or 0) + 1
    publication = Publication(
        project_id=job.project_id,
        job_id=job.id,
        snapshot_id=job.snapshot_id,
        version=version,
        checks=stage_checks,
        input_summary=job.snapshot.input_summary,
        algorithm=job.snapshot.algorithm,
        elevations=elevations,
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return {"publication_id": publication.id, "version": publication.version, "elevation_count": len(elevations)}
