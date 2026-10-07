"""HTTP-level tests for the datum precheck + optimistic confirmation flow.

The production stack is PostgreSQL/PostGIS; these tests substitute SQLite and
replace the Geometry column type with a plain text column so the optimistic-lock
semantics (read-only precheck, 409 on stale confirmation) can be exercised
without a database server.
"""
from __future__ import annotations

from decimal import Decimal
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import JSON, Text, TypeDecorator

from app.api.routes import router
from app.core.db import Base
from app.core.db import get_db
from app.models import schema  # noqa: F401  (register models on Base)


# Production uses PostgreSQL's JSONB, which accepts Decimal; SQLite's generic
# JSON uses json.dumps, so emulate JSONB with a Decimal-aware decorator.
class SQLiteJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        def default(node):
            if isinstance(node, Decimal):
                return float(node)
            raise TypeError(f"not JSON serializable: {type(node).__name__}")

        return json.loads(json.dumps(value, default=default)) if value is not None else None


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@pytest.fixture()
def client():
    # Swap the PostGIS geometry column for plain text and every JSONB column
    # for a Decimal-coercing JSON before create_all so the schema materializes
    # without geo DDL events against SQLite.
    schema.Point.__table__.c.geom.type = Text()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = SQLiteJSON()

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client, TestingSession


def _seed(db_factory):
    db = db_factory()
    project = schema.Project(code="P1", name="precheck project")
    db.add(project)
    db.flush()
    db.add_all(
        [
            schema.Point(id=10, project_id=project.id, code="A"),
            schema.Point(id=11, project_id=project.id, code="B"),
            schema.Point(id=12, project_id=project.id, code="C"),
            schema.Point(id=13, project_id=project.id, code="D"),
        ]
    )
    db.flush()
    db.add_all(
        [
            schema.Observation(
                project_id=project.id, line_code="L1", from_point_id=10, to_point_id=11,
                observed_delta_m=1.0, distance_m=1000,
            ),
            schema.Observation(
                project_id=project.id, line_code="L2", from_point_id=11, to_point_id=12,
                observed_delta_m=1.0, distance_m=1000,
            ),
        ]
    )
    db.add(schema.Datum(project_id=project.id, point_id=10, elevation_m=100.0, sigma_m=0.001))
    db.add(
        schema.WeightRule(
            project_id=project.id, name="r",
            rule={"method": "distance_inverse_km", "c_km": 1.0, "base_sigma_m": 0.001},
        )
    )
    db.commit()
    pid = project.id
    db.close()
    return pid


def test_precheck_datumless_component_says_can_add_and_persists_nothing(client):
    test_client, db_factory = client
    project_id = _seed(db_factory)

    response = test_client.post(
        f"/api/projects/{project_id}/datums/precheck",
        json={"point_code": "D", "elevation_m": 50.0, "sigma_m": 0.001},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "datumless_can_add"
    assert body["existing_datums"] == []
    assert body["component"]["point_count"] == 1
    assert body["draft_lock_version"] == 1

    # The precheck must not write anything: D has no datum row.
    db = db_factory()
    assert db.query(schema.Datum).join(schema.Point).filter(schema.Point.code == "D").count() == 0
    assert db.query(schema.AuditEvent).count() == 0
    db.close()


def test_conflicting_precheck_reports_risk_without_saving(client):
    test_client, db_factory = client
    project_id = _seed(db_factory)

    # A=100 with 1 m steps implies C=102; proposing C=110 is a hard contradiction.
    response = test_client.post(
        f"/api/projects/{project_id}/datums/precheck",
        json={"point_code": "C", "elevation_m": 110.0, "sigma_m": 0.001},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "conflict"
    assert body["risk_level"] == "danger"
    assert any(c["kind"] == "network_implied_elevation" for c in body["conflicts"])

    db = db_factory()
    assert db.query(schema.Datum).count() == 1
    assert db.query(schema.AuditEvent).count() == 0
    db.close()


def test_confirm_with_current_version_then_stale_version_is_rejected_409(client):
    test_client, db_factory = client
    project_id = _seed(db_factory)

    precheck = test_client.post(
        f"/api/projects/{project_id}/datums/precheck",
        json={"point_code": "B", "elevation_m": 101.0, "sigma_m": 0.001},
    ).json()
    assert precheck["verdict"] == "compatible"
    version = precheck["draft_lock_version"]

    # Someone else revises the draft after the precheck.
    db = db_factory()
    project = db.get(schema.Project, project_id)
    project.lock_version += 1
    db.commit()
    db.close()

    # The old version is rejected and nothing is saved.
    stale = test_client.post(
        f"/api/projects/{project_id}/datums",
        json={"point_code": "B", "elevation_m": 101.0, "sigma_m": 0.001, "lock_version": version},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["error"] == "optimistic_lock_conflict"
    assert detail["expected_lock_version"] == version
    assert detail["actual_lock_version"] == version + 1

    db = db_factory()
    assert db.query(schema.Datum).count() == 1
    db.close()

    # Confirming against the current version succeeds through the same API.
    ok = test_client.post(
        f"/api/projects/{project_id}/datums",
        json={"point_code": "B", "elevation_m": 101.0, "sigma_m": 0.001, "lock_version": version + 1},
    )
    assert ok.status_code == 201
    payload = ok.json()
    assert payload["draft_lock_version"] == version + 2
    db = db_factory()
    assert db.query(schema.Datum).count() == 2
    db.close()


def test_observation_patch_invalidates_precheck_version(client):
    """The 'someone else changed the draft' edit may be an observation revision."""
    test_client, db_factory = client
    project_id = _seed(db_factory)

    precheck = test_client.post(
        f"/api/projects/{project_id}/datums/precheck",
        json={"point_code": "B", "elevation_m": 101.0, "sigma_m": 0.001},
    ).json()
    version = precheck["draft_lock_version"]

    db = db_factory()
    observation = db.query(schema.Observation).filter_by(line_code="L1").one()
    obs_id, obs_version = observation.id, observation.lock_version
    db.close()

    patched = test_client.patch(
        f"/api/observations/{obs_id}",
        json={"lock_version": obs_version, "observed_delta_m": 1.005},
    )
    assert patched.status_code == 200
    assert patched.json()["draft_lock_version"] == version + 1

    stale = test_client.post(
        f"/api/projects/{project_id}/datums",
        json={"point_code": "B", "elevation_m": 101.0, "sigma_m": 0.001, "lock_version": version},
    )
    assert stale.status_code == 409


def test_precheck_unknown_point_is_404(client):
    test_client, db_factory = client
    project_id = _seed(db_factory)
    response = test_client.post(
        f"/api/projects/{project_id}/datums/precheck",
        json={"point_code": "ZZZ", "elevation_m": 0.0},
    )
    assert response.status_code == 404


def test_import_with_observations_resolves_all_codes(client):
    """Regression: set difference binds tighter than set union.

    ``from_codes | to_codes - known`` parsed as ``from_codes | (to_codes -
    known)`` falsely reported every from-code as unknown.
    """
    test_client, db_factory = client
    project_id = test_client.post("/api/projects", json={"code": "IMP", "name": "import"}).json()["id"]
    response = test_client.post(
        f"/api/projects/{project_id}/import",
        json={
            "points": [{"code": "A"}, {"code": "B"}],
            "observations": [
                {
                    "line_code": "L1",
                    "from_code": "A",
                    "to_code": "B",
                    "observed_delta_m": 1.0,
                    "distance_m": 1000,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["observations_created"] == 1
