"""Exercise the actual evolve handler: JSON algorithm selection precedes LLM gates."""

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.testclient import TestClient

from backend.services.engine.alpha_agent.api_models import MiningSubmitRequest
from backend.services.engine.alpha_agent import statistical_runtime as runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def endpoint(monkeypatch):
    # Import only the actual handler so unrelated router imports cannot connect
    # to databases or initialize engines in this contract test.
    source = ROOT / "backend/services/engine/routers/alpha_agent.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    handler = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_evolution"
    )
    handler.decorator_list = []
    llm = AsyncMock(return_value=(None, "none"))
    namespace = {
        "Request": Request,
        "Query": Query,
        "Body": Body,
        "MiningSubmitRequest": MiningSubmitRequest,
        "HTTPException": HTTPException,
        "get_authenticated_identity": lambda request: ("owner", "tenant"),
        "assert_identity_not_spoofed": MagicMock(),
        "_resolve_effective_llm_config": llm,
        "_universe_is_valid": lambda universe: True,
        "logger": logging.getLogger(__name__),
    }
    exec(
        compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    submitted = AsyncMock(
        return_value={
            "code": 200,
            "data": {
                "task_id": "stat-task",
                "status": "queued",
                "algorithm": "statistical",
            },
        }
    )
    monkeypatch.setattr(runtime, "submit_statistical", submitted)
    app = FastAPI()
    app.post("/evolve")(namespace["start_evolution"])
    with TestClient(app) as client:
        yield client, submitted, llm


def request_body():
    return {
        "algorithm": "statistical",
        "universe": "all_a",
        "market": "a_share",
        "data_source": "qlib_bin",
        "freq": "day",
        "auto_materialize": False,
        "mining_start": "2020-01-01",
        "mining_end": "2021-12-31",
        "valid_start": "2022-01-01",
        "valid_end": "2022-12-31",
    }


def test_statistical_json_dispatch_does_not_request_llm(endpoint):
    client, submitted, llm = endpoint
    response = client.post("/evolve?universe=csi300", json=request_body())
    assert response.status_code == 200
    assert response.json()["data"]["algorithm"] == "statistical"
    values = submitted.call_args.args[1]
    assert values["universe"] == "all_a"
    assert values["mining_start"] == "2020-01-01"
    assert values["mining_end"] == "2021-12-31"
    assert submitted.call_args.args[3] == {"tenant_id": "tenant", "user_id": "owner"}
    llm.assert_not_called()


def test_gp_dispatch_preserves_seed_and_does_not_request_llm(endpoint):
    client, submitted, llm = endpoint
    response = client.post(
        "/evolve",
        json={
            **request_body(),
            "algorithm": "gp",
            "gp_config": {"seed": 17, "population_size": 8, "generations": 2},
        },
    )
    assert response.status_code == 200
    values = submitted.call_args.args[1]
    assert values["gp_config"]["seed"] == 17
    assert values["algorithm"] == "gp"
    assert submitted.call_args.args[2].key == "gp"
    llm.assert_not_called()


@pytest.mark.parametrize("algorithm", ["statistical", "gp"])
def test_forecast_period_reaches_numeric_runner(endpoint, algorithm):
    client, submitted, _ = endpoint
    response = client.post(
        "/evolve",
        json={**request_body(), "algorithm": algorithm, "prediction_horizon": 5},
    )
    assert response.status_code == 200
    values = submitted.call_args.args[1]
    assert values["prediction_horizon"] == 5
    if algorithm == "statistical":
        assert values["horizons"] == [5]
    else:
        assert values["gp_config"]["horizon"] == 5


@pytest.mark.parametrize(
    "change",
    [
        {"gp_config": {"population_size": 1000}},
        {"gp_config": {"seed": -1}},
        {"valid_start": "2021-01-01", "valid_end": "2023-01-01"},
    ],
)
def test_invalid_gp_is_rejected_before_dispatch(endpoint, change):
    client, submitted, llm = endpoint
    response = client.post(
        "/evolve", json={**request_body(), "algorithm": "gp", **change}
    )
    assert response.status_code == 422
    submitted.assert_not_called()
    llm.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"algorithm": "unknown"},
        {"tenant_id": "another-owner"},
        {"mining_start": "2022-01-01"},
        {"mining_end": None},
    ],
)
def test_invalid_statistical_submission_is_not_dispatched(endpoint, change):
    client, submitted, llm = endpoint
    response = client.post("/evolve", json={**request_body(), **change})
    assert response.status_code == 422
    submitted.assert_not_called()
    llm.assert_not_called()


def test_factor_results_keep_owner_task_evidence_and_fixed_expression():
    from backend.services.engine.alpha_agent.statistical.candidates import CANDIDATES

    candidate = CANDIDATES[0]
    report = {
        "key": candidate.key,
        "name": candidate.name,
        "expression": candidate.expression,
        "selected": True,
        "rank_ic_mean": 0.03,
        "icir": 0.4,
    }
    params = {
        "universe": "csi300",
        "mining_start": "2020-01-01",
        "mining_end": "2021-12-31",
        "valid_start": "2022-01-01",
        "valid_end": "2022-12-31",
    }
    rows = runtime.result_factors("task-a", "a" * 64, params, {"candidates": [report]})
    assert len(rows) == 1
    assert rows[0]["rank_ic"] == 0.03
    assert rows[0]["metadata"]["task_id"] == "task-a"
    assert rows[0]["metadata"]["candidate_key"] == candidate.key
    assert rows[0]["factor_code"] == candidate.expression
    different = runtime.result_factors(
        "task-b", "a" * 64, params, {"candidates": [report]}
    )
    assert rows[0]["factor_id"] != different[0]["factor_id"]
    with pytest.raises(ValueError, match="differs"):
        runtime.result_factors(
            "task-a",
            "a" * 64,
            params,
            {"candidates": [{**report, "expression": "arbitrary()"}]},
        )


@pytest.mark.parametrize("algorithm", ["statistical", "gp"])
def test_launcher_dispatches_statistical_before_rd_agent(monkeypatch, algorithm):
    import asyncio
    from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher

    run = AsyncMock()
    monkeypatch.setattr(runtime, "run_statistical", run)
    launcher = AlphaAgentLauncher.__new__(AlphaAgentLauncher)
    row = {
        "algorithm": algorithm,
        "market": "a_share",
        "tenant_id": "tenant",
        "user_id": "owner",
    }
    asyncio.run(launcher.run_queued(row))
    run.assert_awaited_once_with(launcher, row)


@pytest.fixture
def controller(monkeypatch, tmp_path):
    import contextlib
    import hashlib
    import sys
    from backend.services.engine.alpha_agent import resource_budget
    from backend.shared import process_witness

    row = {
        "task_id": "stat-run",
        "tenant_id": "tenant",
        "user_id": "owner",
        "algorithm": "statistical",
        "market": "a_share",
        "universe": "csi300",
        "params_snapshot": {
            "market": "a_share",
            "universe": "csi300",
            "mining_start": "2020-01-01",
            "mining_end": "2021-12-31",
        "valid_start": "2022-01-01",
        "valid_end": "2022-12-31",
        },
        "data_snapshot": {"status": "preparing", "qlib_provider_uri": "/public/qlib"},
    }
    store = SimpleNamespace(
        update=AsyncMock(return_value=True),
        get=AsyncMock(return_value={"status": "running"}),
        update_progress=AsyncMock(return_value=True),
        set_process_group=AsyncMock(return_value=True),
        seal_data_snapshot=AsyncMock(return_value="b" * 64),
        get_for_dispatch=AsyncMock(
            return_value={
                "execution_fingerprint": "b" * 64,
                "data_snapshot": {
                    "status": "frozen",
                    "panel_sha256": "p" * 64,
                    "metadata_sha256": "m" * 64,
                },
            }
        ),
        complete_with_result=AsyncMock(return_value=True),
        confirm_cancelled=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(runtime, "_task_store", lambda: store)
    monkeypatch.setitem(
        sys.modules,
        "backend.services.engine.alpha_agent.queue",
        SimpleNamespace(capture_controller=lambda: {"controller": "test"}),
    )
    monkeypatch.setattr(process_witness, "capture_process", lambda pid: {"pid": pid})

    @contextlib.contextmanager
    def lease(*args, **kwargs):
        yield

    monkeypatch.setattr(resource_budget, "begin_execution", lease)
    monkeypatch.setattr(resource_budget, "record_process_group", lambda *a, **k: True)
    process = SimpleNamespace(
        pid=12345, returncode=None, wait=MagicMock(return_value=0)
    )
    calls = []

    def poll():
        if calls:
            process.returncode = 0
            return 0
        calls.append(True)
        return None

    process.poll = poll
    captured = {}

    def spawn(args, **kwargs):
        captured.update(kwargs)
        (tmp_path / "statistical_panel.parquet").write_bytes(b"prices")
        (tmp_path / "statistical_metadata.parquet").write_bytes(b"metadata")
        runtime._atomic_json(
            tmp_path / "statistical_prepared.json",
            {
                "panel_sha256": hashlib.sha256(b"prices").hexdigest(),
                "metadata_sha256": hashlib.sha256(b"metadata").hexdigest(),
                "symbols_sha256": "c" * 64,
            },
        )
        runtime._atomic_json(
            tmp_path / "statistical_progress.json",
            {
                "stage": "evaluate",
                "detail": "截面检验",
                "progress_pct": 35,
            },
        )
        runtime._atomic_json(
            tmp_path / "statistical_result.json",
            {
                "candidates": [],
                "selected": [],
            },
        )
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    launcher = SimpleNamespace(
        _tasks={},
        task_dir=lambda *a, **k: tmp_path,
        _persist_task=MagicMock(),
        _terminate_process_group=AsyncMock(return_value=True),
    )
    return launcher, row, store, process, captured, tmp_path


def test_controller_seals_snapshot_before_atomic_completion(monkeypatch, controller):
    import asyncio

    launcher, row, store, process, captured, work = controller
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-statistical-worker")
    asyncio.run(runtime.run_statistical(launcher, row))

    store.seal_data_snapshot.assert_awaited_once()
    assert (work / "statistical_sealed.json").is_file()
    completed = store.complete_with_result.call_args.kwargs
    assert completed["execution_fingerprint"] == "b" * 64
    assert completed["result"]["total_factors"] == 0
    assert completed["tenant_id"] == "tenant"
    assert completed["user_id"] == "owner"
    assert launcher._tasks["stat-run"].status.value == "completed"
    assert "OPENAI_API_KEY" not in captured["env"]
    launcher._terminate_process_group.assert_awaited_once_with(process.pid)
    process.wait.assert_called_once()


def test_controller_cancellation_stops_child_without_publishing_results(controller):
    import asyncio

    launcher, row, store, process, _, work = controller
    store.get.return_value = {"status": "stopping"}
    asyncio.run(runtime.run_statistical(launcher, row))

    store.complete_with_result.assert_not_called()
    store.seal_data_snapshot.assert_not_called()
    assert not (work / "statistical_sealed.json").exists()
    store.confirm_cancelled.assert_awaited_once_with(
        "stat-run", tenant_id="tenant", user_id="owner"
    )
    assert launcher._tasks["stat-run"].status.value == "cancelled"
    launcher._terminate_process_group.assert_awaited_once_with(process.pid)


def test_queued_status_uses_owned_database_row_before_worker_attaches(monkeypatch):
    import asyncio

    row = {
        "task_id": "waiting",
        "tenant_id": "tenant",
        "user_id": "owner",
        "algorithm": "statistical",
        "status": "queued",
        "executor_json": {"private": "must-not-be-public"},
        "params_snapshot": {"mining_start": "2020-01-01"},
    }
    store = SimpleNamespace(get_for_owner_with_spec=AsyncMock(return_value=row))
    monkeypatch.setattr(runtime, "_task_store", lambda: store)
    result = asyncio.run(
        runtime.owned_statistical_task("waiting", tenant_id="tenant", user_id="owner")
    )
    assert result["status"] == "queued"
    assert result["mining_start"] == "2020-01-01"
    assert "executor_json" not in result
    store.get_for_owner_with_spec.assert_awaited_once_with(
        "waiting", tenant_id="tenant", user_id="owner", consistent=True
    )


def test_controller_refuses_changed_snapshot_artifacts(monkeypatch, controller):
    import asyncio

    launcher, row, store, process, _, work = controller
    monkeypatch.setattr(runtime, "digest_file", lambda *a: "0" * 64)
    asyncio.run(runtime.run_statistical(launcher, row))

    store.complete_with_result.assert_not_called()
    store.seal_data_snapshot.assert_not_called()
    assert not (work / "statistical_sealed.json").exists()
    assert launcher._tasks["stat-run"].status.value == "failed"
    assert "校验失败" in launcher._tasks["stat-run"].error_message
    launcher._terminate_process_group.assert_awaited_once_with(process.pid)


def test_qlib_loading_respects_statistical_worker_capacity(monkeypatch):
    import sys
    from backend.services.engine.alpha_agent.statistical import panel

    init = MagicMock()
    data = SimpleNamespace(
        D=SimpleNamespace(
            features=MagicMock(return_value="panel"),
            calendar=MagicMock(return_value=["2020-01-02"]),
            instruments=MagicMock(return_value={"market": "all"}),
            list_instruments=MagicMock(return_value=["sh600000"]),
        )
    )
    monkeypatch.setitem(sys.modules, "qlib", SimpleNamespace(init=init, data=data))
    monkeypatch.setitem(sys.modules, "qlib.data", data)
    monkeypatch.setitem(sys.modules, "qlib.constant", SimpleNamespace(REG_CN="cn"))
    panel.qlib_calendar("2020-01-01", "2020-12-31", qlib_dir="/public")
    assert panel.qlib_instruments("all", qlib_dir="/public") == ["sh600000"]
    assert (
        panel._qlib_features(
            ["sh600000"], ["$close"], "2020-01-01", "2020-12-31", "/public"
        )
        == "panel"
    )
    assert init.call_count == 3
    assert all(call.kwargs["kernels"] == 1 for call in init.call_args_list)


def test_missing_listing_metadata_has_actionable_error_instead_of_empty_success():
    import pandas as pd
    from backend.services.engine.alpha_agent.statistical.worker import (
        _require_listing_metadata,
    )

    for metadata in (pd.DataFrame(), pd.DataFrame({"listing_date": [pd.NaT]})):
        with pytest.raises(ValueError, match="初始化股票基础资料"):
            _require_listing_metadata(metadata, 120)
        _require_listing_metadata(metadata, 0)
    _require_listing_metadata(
        pd.DataFrame({"listing_date": [pd.Timestamp("2020-01-01")]}), 120
    )
