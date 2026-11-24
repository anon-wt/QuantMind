"""Validation-history HTTP contract against the real factor reader and SQL."""

import ast
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from backend.shared.owner_scope import OwnerScope
from backend.shared.secret_redaction import redact_data
from backend.shared.utc_datetime import to_utc_iso

ROOT = Path(__file__).resolve().parents[2]


def load_function(relative_path, name, namespace, *, class_name=None):
    """Use actual production bodies without unrelated router/engine startup."""
    path = ROOT / relative_path
    nodes = ast.parse(path.read_text(encoding="utf-8")).body
    if class_name:
        nodes = next(
            n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name
        ).body
    function = next(
        n for n in nodes if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )
    function.decorator_list = []
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace[name]


@pytest.fixture
def validation_api():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    columns = (
        "factor_id factor_name factor_code status ic_value sharpe_ratio "
        "annual_return max_drawdown rank_ic tenant_id user_id market universe "
        "factor_formulation data_source date_range metadata_json created_at updated_at archived_at"
    ).split()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE rd_agent_factor_revisions (factor_id TEXT, tenant_id TEXT, "
                "user_id TEXT, revision_no INTEGER, factor_code TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE rd_agent_factors ("
                + ", ".join(f"{column} TEXT" for column in columns)
                + ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO rd_agent_factors "
                "(factor_id,tenant_id,user_id,factor_name,metadata_json,factor_code) "
                "VALUES (:factor_id,:tenant_id,:user_id,:factor_name,:metadata,'original')"
            ),
            {
                "factor_id": "factor-a",
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "factor_name": "Example",
                "metadata": json.dumps({"algorithm": "statistical"}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO rd_agent_factor_revisions VALUES "
                "('factor-a','tenant-a','user-a',2,'original'),"
                "('factor-a','tenant-a','user-a',3,'changed'),"
                "('factor-a','other-tenant','user-a',99,'original')"
            )
        )

    @asynccontextmanager
    async def session_factory(*, read_only):
        assert read_only
        with engine.connect() as conn:

            async def execute(statement, params):
                return conn.execute(statement, params)

            yield SimpleNamespace(execute=execute)

    reader = load_function(
        "backend/services/engine/qlib_app/services/rd_agent_persistence.py",
        "get_factor",
        {
            "Any": Any,
            "OwnerScope": OwnerScope,
            "text": text,
            "get_session": session_factory,
            "json": json,
        },
        class_name="RDAgentFactorPersistence",
    )
    batch_reader = load_function(
        "backend/services/engine/qlib_app/services/rd_agent_persistence.py",
        "get_factors_by_ids",
        {"Any": Any, "OwnerScope": OwnerScope},
        class_name="RDAgentFactorPersistence",
    )
    store = type(
        "FactorStore", (), {"get_factor": reader, "get_factors_by_ids": batch_reader}
    )()
    jobs = SimpleNamespace(
        latest_validation_id=AsyncMock(return_value=None),
        validation_history=AsyncMock(return_value=[]),
    )
    handler = load_function(
        "backend/services/engine/alpha_agent/factor_job_api.py",
        "factor_validation_history",
        {
            "Request": Request,
            "Query": Query,
            "HTTPException": HTTPException,
            "_owner": lambda r: (r.headers["x-test-tenant"], r.headers["x-test-user"]),
            "_factors": store,
            "_jobs": jobs,
            "datetime": datetime,
            "timezone": timezone,
            "to_utc_iso": to_utc_iso,
            "redact_data": redact_data,
        },
    )
    app = FastAPI()
    app.get("/factors/{factor_id}/validation-history")(handler)
    with TestClient(app) as client:
        yield client, store, jobs
    engine.dispose()


OWNER_HEADERS = {"x-test-tenant": "tenant-a", "x-test-user": "user-a"}


def test_owned_factor_without_validations_is_empty_success(validation_api):
    client, _, jobs = validation_api
    response = client.get("/factors/factor-a/validation-history", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert response.json()["data"] == {"latest_validation_id": None, "attempts": []}
    jobs.validation_history.assert_awaited_once_with(
        "factor-a", tenant_id="tenant-a", user_id="user-a", limit=20
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"x-test-tenant": "other-tenant", "x-test-user": "user-a"},
        {"x-test-tenant": "tenant-a", "x-test-user": "other-user"},
    ],
)
def test_other_owner_cannot_read_factor_validation_history(validation_api, headers):
    client, _, jobs = validation_api
    response = client.get("/factors/factor-a/validation-history", headers=headers)
    assert response.status_code == 404
    jobs.latest_validation_id.assert_not_awaited()
    jobs.validation_history.assert_not_awaited()


def test_completed_validation_preserves_metrics_and_utc_timestamp(validation_api):
    client, _, jobs = validation_api
    jobs.latest_validation_id.return_value = "validation-1"
    jobs.validation_history.return_value = [
        {
            "validation_id": "validation-1",
            "factor_id": "factor-a",
            "status": "completed",
            "validation_context": {"non_overlapping": True},
            "result_snapshot": {"metrics": {"ic_mean": 0.04}},
            "created_at": datetime(2026, 11, 24, tzinfo=timezone.utc),
            "job_id": "private-internal-job",
        }
    ]
    response = client.get("/factors/factor-a/validation-history", headers=OWNER_HEADERS)
    assert response.status_code == 200
    attempt = response.json()["data"]["attempts"][0]
    assert attempt["is_latest"] is True
    assert attempt["result_snapshot"]["metrics"]["ic_mean"] == 0.04
    assert attempt["created_at"].endswith("Z")
    assert "job_id" not in attempt


def test_legacy_factor_reader_remains_compatible(validation_api):
    _, store, _ = validation_api
    row = asyncio.run(store.get_factor("factor-a"))
    assert row["factor_name"] == "Example"
    assert row["metadata"]["algorithm"] == "statistical"
    assert row["factor_revision_no"] == 2


def test_batch_reader_keeps_scope_and_source_revisions(validation_api):
    _, store, _ = validation_api
    rows = asyncio.run(
        store.get_factors_by_ids(
            ["factor-a"], tenant_id="tenant-a", user_id="user-a", include_archived=True
        )
    )
    assert rows[0]["factor_revision_no"] == 2
    assert (
        asyncio.run(
            store.get_factors_by_ids(
                ["factor-a"], tenant_id="other-tenant", user_id="user-a"
            )
        )
        == []
    )


@pytest.mark.parametrize("scope", [{"tenant_id": "tenant-a"}, {"user_id": "user-a"}])
def test_partial_owner_scope_is_rejected(validation_api, scope):
    _, store, _ = validation_api
    with pytest.raises(ValueError, match="Both owner dimensions"):
        asyncio.run(store.get_factor("factor-a", **scope))
