"""Exercise the worker entry point without importing Qlib or connecting to services."""

import ast
import asyncio
import io
import json
import logging
import re
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.shared.utc_datetime import to_utc_iso, utc_now


class _RetryRequested(Exception):
    pass


class _Redis:
    def __init__(self):
        self.logs = {}

    def rpush(self, key, value):
        self.logs.setdefault(key, []).append(value)

    def expire(self, key, seconds):
        pass


@pytest.fixture
def worker(monkeypatch):
    """Run the actual task/logging code with only infrastructure replaced.

    Loading these definitions avoids worker bootstrap side effects and lets this
    regression run on development machines without Qlib/Celery installed.
    """
    task_path = (
        Path(__file__).resolve().parents[1]
        / "services/engine/qlib_app/tasks.py"
    )
    names = {
        "TaskRedisLogHandler",
        "TaskStreamAdapter",
        "TaskLogCapture",
        "TaskStructuredLogger",
        "_to_jsonable",
        "_run_async",
        "run_backtest_async",
    }
    tree = ast.parse(task_path.read_text(encoding="utf-8"))
    definitions = [node for node in tree.body if getattr(node, "name", None) in names]
    redis = _Redis()
    logger = logging.getLogger("test.backtest_worker")

    def task(**options):
        def decorate(function):
            function.task_options = options
            return function

        return decorate

    service = types.SimpleNamespace(
        initialize=Mock(),
        run_backtest=AsyncMock(
            return_value=QlibBacktestResult(backtest_id="backtest-1")
        ),
    )
    guard = types.SimpleNamespace(__exit__=Mock())
    budget = types.ModuleType("test_budget")
    budget.begin_execution = Mock(return_value=guard)
    persistence = types.SimpleNamespace(mark_failed=AsyncMock())
    persistence_module = types.ModuleType("test_persistence")
    persistence_module.BacktestPersistence = Mock(return_value=persistence)
    cache = types.SimpleNamespace(invalidate_user_history=Mock())
    cache_module = types.ModuleType("test_cache")
    cache_module.get_cache_manager = lambda: cache
    for suffix, module in (
        ("resource_budget", budget),
        ("services.backtest_persistence", persistence_module),
        ("cache_manager", cache_module),
    ):
        monkeypatch.setitem(
            sys.modules, f"backend.services.engine.qlib_app.{suffix}", module
        )

    namespace = {
        "Any": Any,
        "asyncio": asyncio,
        "io": io,
        "json": json,
        "logging": logging,
        "sys": sys,
        "datetime": datetime,
        "logger": logger,
        "redis": types.SimpleNamespace(Redis=lambda **kwargs: redis),
        "REDIS_HOST": "unused",
        "REDIS_PORT": 6379,
        "REDIS_PASSWORD": None,
        "REDIS_DB": 0,
        "QlibBacktestResult": QlibBacktestResult,
        "utc_now": utc_now,
        "celery_app": types.SimpleNamespace(task=task),
        "CallbackTask": object,
        "_async_loop": None,
        "_get_qlib_service_instance": Mock(return_value=service),
        "_send_progress_update": Mock(),
    }
    exec(
        compile(ast.Module(body=definitions, type_ignores=[]), str(task_path), "exec"),
        namespace,
    )
    task_self = types.SimpleNamespace(
        request=types.SimpleNamespace(id="backtest-1"),
        update_state=Mock(),
        retry=Mock(side_effect=_RetryRequested),
    )
    payload = {
        "backtest_id": "backtest-1",
        "strategy_type": "standard_topk",
        "start_date": "2026-01-01",
        "end_date": "2026-11-20",
        "user_id": "user-1",
        "tenant_id": "tenant-1",
    }
    yield types.SimpleNamespace(
        run=namespace["run_backtest_async"],
        task=task_self,
        payload=payload,
        service=service,
        persistence=persistence,
        budget=budget,
        guard=guard,
        cache=cache,
        progress=namespace["_send_progress_update"],
        redis=redis,
        logger=logger,
    )
    loop = namespace["_async_loop"]
    if loop is not None:
        loop.close()
        asyncio.set_event_loop(None)


def test_initialization_failure_is_logged_and_persisted_as_terminal(worker, caplog):
    def fail_initialize():
        worker.logger.error("Qlib day-frequency data is not ready")
        raise RuntimeError("Qlib day-frequency data is not ready")

    worker.service.initialize.side_effect = fail_initialize
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="day-frequency data is not ready"):
            worker.run(worker.task, worker.payload)

    worker.service.run_backtest.assert_not_called()
    worker.persistence.mark_failed.assert_awaited_once()
    saved = worker.persistence.mark_failed.await_args.kwargs
    assert saved["backtest_id"] == saved["task_id"] == "backtest-1"
    assert (saved["tenant_id"], saved["user_id"]) == ("tenant-1", "user-1")
    assert saved["completed_at"].utcoffset().total_seconds() == 0
    assert "created_at" not in saved and "config" not in saved
    assert "day-frequency data is not ready" in saved["error_message"]
    assert "RuntimeError" in saved["full_error"]
    logs = worker.redis.logs["qlib:logs:tenant-1:backtest-1"]
    assert any("day-frequency data is not ready" in line for line in logs)
    assert worker.progress.call_args.args[0]["status"] == "failed"
    worker.cache.invalidate_user_history.assert_called_once_with("tenant-1:user-1")
    worker.task.retry.assert_not_called()
    assert not worker.run.task_options.get("autoretry_for")
    assert worker.guard.__exit__.call_args.args[0] is RuntimeError


def test_failed_runtime_result_is_not_announced_as_completed(worker):
    worker.service.run_backtest.return_value = QlibBacktestResult(
        backtest_id="backtest-1",
        status="failed",
        error_message="Prediction signal is missing",
        full_error="Original Qlib failure traceback",
    )
    with pytest.raises(RuntimeError, match="Prediction signal is missing"):
        worker.run(worker.task, worker.payload)

    saved = worker.persistence.mark_failed.await_args.kwargs
    assert saved["full_error"] == "Original Qlib failure traceback"
    assert all(
        call.kwargs["state"] != "SUCCESS"
        for call in worker.task.update_state.call_args_list
    )
    assert all(
        call.args[0]["status"] != "completed"
        for call in worker.progress.call_args_list
    )


def test_failure_persistence_outage_keeps_original_error_and_notifies(worker):
    worker.service.initialize.side_effect = RuntimeError("Qlib cache is missing")
    worker.persistence.mark_failed.side_effect = OSError("Database unavailable")

    with pytest.raises(RuntimeError, match="Qlib cache is missing"):
        worker.run(worker.task, worker.payload)

    assert worker.progress.call_args.args[0]["error_message"] == "Qlib cache is missing"
    assert worker.guard.__exit__.call_args.args[0] is RuntimeError


def test_capacity_wait_keeps_explicit_retry_before_execution(worker):
    worker.budget.begin_execution.return_value = None
    with pytest.raises(_RetryRequested):
        worker.run(worker.task, worker.payload)

    worker.task.retry.assert_called_once_with(countdown=30, max_retries=None)
    worker.service.initialize.assert_not_called()
    worker.persistence.mark_failed.assert_not_called()
    worker.guard.__exit__.assert_not_called()


def test_successful_backtest_still_completes_and_releases_lease(worker):
    result = worker.run(worker.task, worker.payload)

    assert result["status"] == "completed"
    assert worker.progress.call_args.args[0]["status"] == "completed"
    worker.task.update_state.assert_any_call(state="SUCCESS", meta={"result": result})
    worker.persistence.mark_failed.assert_not_called()
    worker.guard.__exit__.assert_called_once_with(None, None, None)


def test_failure_update_preserves_submission_and_status_exposes_errors(worker):
    persistence_path = (
        Path(__file__).resolve().parents[1]
        / "services/engine/qlib_app/services/backtest_persistence.py"
    )
    tree = ast.parse(persistence_path.read_text(encoding="utf-8"))
    persistence_class = next(
        node
        for node in tree.body
        if getattr(node, "name", None) == "BacktestPersistence"
    )
    persistence_class.body = [
        node
        for node in persistence_class.body
        if getattr(node, "name", None) in {"mark_failed", "get_status"}
    ]
    session = types.SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def get_session(**kwargs):
        yield session

    namespace = {
        "Any": Any,
        "datetime": datetime,
        "json": json,
        "text": text,
        "get_session": get_session,
        "normalize_user_id": str,
        "to_utc_iso": to_utc_iso,
    }
    exec(
        compile(
            ast.Module(body=[persistence_class], type_ignores=[]),
            str(persistence_path),
            "exec",
        ),
        namespace,
    )
    persistence = namespace["BacktestPersistence"]()
    worker.persistence.mark_failed = persistence.mark_failed
    worker.service.initialize.side_effect = RuntimeError("Qlib cache is missing")
    with pytest.raises(RuntimeError, match="Qlib cache is missing"):
        worker.run(worker.task, worker.payload)

    query, params = session.execute.await_args.args
    sql = str(query)
    assignments = sql.split(" SET", 1)[1].split("WHERE", 1)[0]
    assert set(re.findall(r"^\s*(\w+)\s*=", assignments, re.MULTILINE)) == {
        "status", "completed_at", "task_id", "result_json"
    }
    assert "user_id = :user_id AND tenant_id = :tenant_id" in sql
    assert "status IN ('pending', 'queued', 'running', 'failed')" in sql
    assert "COALESCE(task_id, :task_id)" in sql
    assert "COALESCE(result_json, '{}'::jsonb)" in sql
    assert params["completed_at"].utcoffset().total_seconds() == 0
    failure = json.loads(params["failure_json"])
    assert failure["completed_at"].endswith("Z")
    assert "created_at" not in failure and "config" not in failure

    row = {
        "backtest_id": "backtest-1",
        "status": failure["status"],
        "created_at": utc_now(),
        "completed_at": params["completed_at"],
        "result_json": failure,
    }
    session.execute.return_value = types.SimpleNamespace(
        mappings=lambda: types.SimpleNamespace(first=lambda: row)
    )
    status = asyncio.run(
        persistence.get_status("backtest-1", tenant_id="tenant-1", user_id="user-1")
    )
    assert status["status"] == "failed"
    assert status["error_message"] == "Qlib cache is missing"
    assert "RuntimeError" in status["full_error"]
