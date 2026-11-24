"""Exercise actual internal HTTP fencing and fixed statistical factor admission."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.engine.alpha_agent import factor_job_internal as internal
from backend.services.engine.alpha_agent.statistical.candidates import CANDIDATE_BY_KEY


@pytest.fixture
def runner(monkeypatch):
    row = {
        "job_id": "job-a",
        "kind": "backtest",
        "status": "running",
        "execution_generation": 1,
        "request_snapshot": {"request": {"factor_kind": "qlib_expression"}},
    }

    async def get(*a, **kw):
        assert kw["tenant_id"] == "tenant-a"
        assert kw["user_id"] == "user-a"
        return dict(row)

    async def finish(*a, **kw):
        row["status"] = "completed" if "result" in kw else "failed"
        return True

    async def cancel(*a, **kw):
        row["status"] = "cancelled"
        return True

    jobs = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        finish=AsyncMock(side_effect=finish),
        confirm_cancelled=AsyncMock(side_effect=cancel),
        update_progress=AsyncMock(return_value=True),
    )
    budget = SimpleNamespace(
        store=SimpleNamespace(
            get_lease=MagicMock(return_value="lease"),
            claim_engine_execution=MagicMock(return_value="claimed"),
            transition=MagicMock(return_value=True),
            mark_engine_request_finished=MagicMock(return_value=True),
            task_snapshot=MagicMock(return_value={"stop_requested": False}),
        ),
        projection=SimpleNamespace(heartbeat=MagicMock(return_value=True)),
    )

    def execute(*a, **kw):
        kw["progress"]({"stage": "factor_eval", "progress_pct": 45})
        return {"metrics": {"ic_mean": 0.03}}

    executor = MagicMock(side_effect=execute)
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.alpha_agent.factor_jobs",
        SimpleNamespace(AlphaFactorJobPersistence=lambda: jobs),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.alpha_agent.factor_job_budget",
        SimpleNamespace(POOL="alpha_factor_jobs", get_budget=lambda: budget),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.alpha_agent.factor_job_executor",
        SimpleNamespace(
            execute_factor_backtest=executor, execute_factor_batch=executor
        ),
    )
    monkeypatch.setattr(internal, "_internal_secret", lambda: "test-internal-secret")
    monkeypatch.setattr(
        internal, "get_authenticated_identity", lambda r: ("user-a", "tenant-a")
    )
    monkeypatch.setattr(internal, "capture_process", lambda: {"pid": 123})
    app = FastAPI()
    app.include_router(internal.router)
    with TestClient(app) as client:
        yield client, jobs, budget, executor, row


BODY = {"job_id": "job-a", "execution_generation": 1, "dispatch_token": "delivery-a"}
HEADERS = {"X-Internal-Call": "test-internal-secret"}
URL = "/api/v1/internal/alpha-factor-jobs/run"


def test_internal_execution_requires_dispatcher_secret(runner):
    client, jobs, _, executor, _ = runner
    assert client.post(URL, json=BODY).status_code == 403
    jobs.get.assert_not_awaited()
    executor.assert_not_called()


def test_internal_execution_fences_delivery_and_finishes_result(runner):
    client, jobs, budget, executor, _ = runner
    response = client.post(URL, json=BODY, headers=HEADERS)
    assert response.json()["status"] == "completed"
    assert (
        budget.store.get_lease.call_args.kwargs["expected_dispatch_token"]
        == "delivery-a"
    )
    assert jobs.finish.call_args.kwargs["result"]["metrics"]["ic_mean"] == 0.03
    assert jobs.update_progress.await_count == 1
    executor.assert_called_once()


def test_duplicate_delivery_does_not_execute_again(runner):
    client, jobs, budget, executor, _ = runner
    budget.store.claim_engine_execution.return_value = "existing_same"
    assert client.post(URL, json=BODY, headers=HEADERS).json()["status"] == "running"
    jobs.finish.assert_not_awaited()
    executor.assert_not_called()


def test_stale_generation_is_rejected_before_execution(runner):
    client, _, budget, executor, row = runner
    row["execution_generation"] = 2
    assert client.post(URL, json=BODY, headers=HEADERS).status_code == 409
    budget.store.claim_engine_execution.assert_not_called()
    executor.assert_not_called()


def test_failed_local_computation_has_terminal_error(runner):
    client, jobs, _, executor, _ = runner
    executor.side_effect = ValueError("Insufficient price observations")
    assert client.post(URL, json=BODY, headers=HEADERS).json()["status"] == "failed"
    assert jobs.finish.call_args.kwargs["error_code"] == "ValueError"


def test_unconfirmed_sandbox_retains_occupancy(runner):
    client, jobs, budget, executor, row = runner
    row["request_snapshot"]["request"]["factor_kind"] = "factor_class"

    def uncertain(*a, **kw):
        kw["lifecycle"]["create_started"] = True
        raise RuntimeError("Sandbox stop could not be confirmed")

    executor.side_effect = uncertain
    assert client.post(URL, json=BODY, headers=HEADERS).json()["status"] == "running"
    jobs.finish.assert_not_awaited()
    budget.store.mark_engine_request_finished.assert_called_once()


def test_stop_during_computation_cancels_without_publishing(runner):
    client, jobs, _, executor, row = runner

    def stop(*a, **kw):
        row["status"] = "stopping"
        return {"metrics": {}}

    executor.side_effect = stop
    assert client.post(URL, json=BODY, headers=HEADERS).json()["status"] == "cancelled"
    jobs.finish.assert_not_awaited()
    jobs.confirm_cancelled.assert_awaited_once()


def test_statistical_admission_requires_exact_fixed_expression():
    path = (
        Path(__file__).resolve().parents[1]
        / "services/engine/alpha_agent/factor_job_api.py"
    )
    function = next(
        n
        for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "_factor_job_kind"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    candidate = CANDIDATE_BY_KEY["reversal_5d"]
    metadata = {"algorithm": "statistical", "candidate_key": candidate.key}
    router = SimpleNamespace(_detect_factor_kind=MagicMock(return_value="factor_class"))
    assert (
        namespace["_factor_job_kind"](candidate.expression, router, metadata)
        == "qlib_expression"
    )
    router._detect_factor_kind.assert_not_called()
    with pytest.raises(ValueError, match="differs"):
        namespace["_factor_job_kind"](candidate.expression + "+1", router, metadata)


def test_fixed_statistical_backtest_computes_real_metrics_without_user_code(
    monkeypatch,
):
    import hashlib
    import numpy as np
    import pandas as pd
    from backend.services.engine.alpha_agent import factor_job_executor as executor

    dates = pd.bdate_range("2025-11-01", "2026-04-30")
    instruments = [f"sh{600000 + i}" for i in range(20)]
    index = pd.MultiIndex.from_product(
        [instruments, dates], names=["instrument", "datetime"]
    )
    rng = np.random.default_rng(12)
    close = (
        np.exp(np.cumsum(rng.normal(0, 0.02, (20, len(dates))), axis=1)).ravel() * 20
    )
    frame = pd.DataFrame(
        {
            "$open": close,
            "$high": close * 1.01,
            "$low": close * 0.99,
            "$close": close,
            "$volume": rng.uniform(1000, 5000, len(index)),
            "$amount": rng.uniform(10000, 50000, len(index)),
            "$factor": 1.0,
        },
        index=index,
    )

    def features(symbols, fields, *, start_time, end_time, freq):
        observed = frame.index.get_level_values(1)
        return frame.loc[(observed >= start_time) & (observed <= end_time), fields]

    provider = SimpleNamespace(calendar=lambda **kw: dates, features=features)
    init = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "qlib",
        SimpleNamespace(init=init, data=SimpleNamespace(D=provider)),
    )
    monkeypatch.setitem(sys.modules, "qlib.data", SimpleNamespace(D=provider))
    path = (
        Path(__file__).resolve().parents[1] / "services/engine/routers/alpha_agent.py"
    )
    function = next(
        n
        for n in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "_vectorized_daily_spearman_ic"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.routers.alpha_agent",
        SimpleNamespace(
            _vectorized_daily_spearman_ic=namespace["_vectorized_daily_spearman_ic"],
        ),
    )
    sandbox = MagicMock(
        side_effect=AssertionError("Fixed catalogue must not execute user code")
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.strategy_lab.runner.container_runner",
        SimpleNamespace(run_factor_series=sandbox),
    )
    monkeypatch.setattr(executor, "_cost_rate", lambda r: (0.0035, {}))
    candidate = CANDIDATE_BY_KEY["reversal_5d"]
    result = executor.execute_factor_backtest(
        {
            "factor_id": "factor-a",
            "factor_code": candidate.expression,
            "factor_code_sha256": hashlib.sha256(
                candidate.expression.encode()
            ).hexdigest(),
            "factor_revision_no": 1,
            "factor_kind": "qlib_expression",
            "statistical_candidate_key": candidate.key,
            "universe": "csi300",
            "universe_symbols": instruments,
            "start_date": "2026-01-01",
            "end_date": "2026-04-30",
        },
        tenant_id="tenant-a",
        user_id="user-a",
        lease=None,
        budget=None,
        lifecycle={},
        progress=lambda r: None,
    )
    assert result["metrics"]["observations"] > 100
    assert np.isfinite(result["metrics"]["ic_mean"])
    assert np.isfinite(result["metrics"]["rank_icir"])
    assert result["series"]
    assert init.call_args.kwargs["kernels"] == 1
    sandbox.assert_not_called()
