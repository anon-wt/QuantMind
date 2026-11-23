"""A 股初始化 API：配置、权限、任务分发及失败/取消语义。"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.services.engine.data_platform import modelscope_dataset_sync
from backend.shared.data_source_config import save_sources


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    monkeypatch.setenv("QM_DATA_SOURCE_CONFIG_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setenv("QM_DATAYES_DATA_DIR", str(tmp_path / "datayes"))
    monkeypatch.setenv("DATAYES_TOKEN", "test-datayes-only")
    monkeypatch.setattr(
        modelscope_dataset_sync, "resolve_data_root", lambda: tmp_path / "quantdb"
    )
    save_sources("A", {"quantdb": True, "datayes": True})
    auth = types.ModuleType("backend.services.api.user_app.middleware.auth")

    async def require_admin():
        raise HTTPException(401, "Authentication required")

    auth.require_admin = require_admin
    monkeypatch.setitem(sys.modules, auth.__name__, auth)
    name = "backend.services.api.routers.admin._a_share_initialization_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backend/services/api/routers/admin/a_share_initialization.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    threads = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            threads.append(self.kwargs)

    monkeypatch.setattr(module, "threading", types.SimpleNamespace(Thread=FakeThread))
    console = types.ModuleType("backend.services.api.routers.admin.quantdb_console")
    console._reset_quantdb_runtime_caches = MagicMock()
    console._publish_baseline_after_init = MagicMock()
    monkeypatch.setitem(sys.modules, console.__name__, console)
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[require_admin] = lambda: {"user_id": "test-admin"}
    with TestClient(app) as client:
        yield client, module, threads


def request(sources=None, datasets=None):
    return {
        "sources": sources or ["datayes"],
        "datasets": datasets or ["daily_forward"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-04",
    }


def test_catalog_exposes_two_initialization_sources_and_explicit_coverage(api):
    client, _, _ = api
    response = client.get("/initialization")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [p["source"] for p in data["providers"]] == ["quantdb", "datayes"]
    assert next(d for d in data["datasets"] if d["dataset"] == "l1_factors")[
        "sources"
    ] == ["quantdb"]
    assert (
        "datayes"
        in next(d for d in data["datasets"] if d["dataset"] == "balance")["sources"]
    )


def test_datayes_only_never_starts_quantdb_initialization(api, monkeypatch):
    client, module, threads = api
    datayes = MagicMock(
        return_value={
            "datasets": [
                {"dataset": "daily_forward", "status": "completed", "rows": 2}
            ],
            "errors": 0,
        }
    )
    quantdb = MagicMock()
    monkeypatch.setattr(module.datayes_sync, "sync_datayes", datayes)
    monkeypatch.setattr(modelscope_dataset_sync, "init_from_modelscope", quantdb)
    response = client.post("/initialization/jobs", json=request())
    assert response.status_code == 200
    assert len(threads) == 1
    threads[0]["target"](*threads[0]["args"])
    job = client.get("/initialization/jobs").json()["data"]["jobs"][0]
    assert job["status"] == "completed"
    assert set(datayes.call_args.args[0]) == {
        "instrument_detail",
        "trading_calendar",
        "daily_forward",
    }
    quantdb.assert_not_called()


def test_quantdb_only_never_calls_datayes(api, monkeypatch):
    client, module, threads = api
    quantdb = MagicMock(
        return_value={"datasets": {"daily_forward": {"downloaded": 1}}, "errors": 0}
    )
    datayes = MagicMock()
    monkeypatch.setattr(modelscope_dataset_sync, "init_from_modelscope", quantdb)
    monkeypatch.setattr(module.datayes_sync, "sync_datayes", datayes)
    client.post("/initialization/jobs", json=request(["quantdb"]))
    threads[0]["target"](*threads[0]["args"])
    assert quantdb.call_args.args[0] == ["daily_forward"]
    datayes.assert_not_called()


def test_missing_token_starts_neither_source(api, monkeypatch):
    client, _, threads = api
    monkeypatch.setenv("DATAYES_TOKEN", "")
    response = client.post("/initialization/jobs", json=request(["quantdb", "datayes"]))
    assert response.status_code == 400
    assert threads == []


@pytest.mark.parametrize(
    "change",
    [
        {"sources": ["unknown"]},
        {"sources": ["datayes", "datayes"]},
        {"datasets": ["l1_factors"]},
        {"datasets": ["not-a-dataset"]},
        {"datasets": []},
        {"start_date": "2024-02-01", "end_date": "2024-01-01"},
    ],
)
def test_invalid_initialization_is_rejected_before_dispatch(api, change):
    client, _, threads = api
    response = client.post("/initialization/jobs", json={**request(), **change})
    assert response.status_code in (400, 422)
    assert not threads


def test_disabled_source_and_concurrent_job_are_rejected(api):
    client, _, threads = api
    save_sources("A", {"datayes": False})
    assert client.post("/initialization/jobs", json=request()).status_code == 409
    save_sources("A", {"datayes": True})
    assert client.post("/initialization/jobs", json=request()).status_code == 200
    assert client.post("/initialization/jobs", json=request()).status_code == 409
    assert len(threads) == 1


def test_dataset_failures_are_reported_as_partial(api, monkeypatch):
    client, module, threads = api
    monkeypatch.setattr(
        module.datayes_sync,
        "sync_datayes",
        lambda *a, **k: {
            "errors": 1,
            "datasets": [
                {
                    "dataset": "daily_forward",
                    "status": "failed",
                    "rows": 0,
                    "error": "permission denied",
                }
            ],
        },
    )
    client.post("/initialization/jobs", json=request())
    threads[0]["target"](*threads[0]["args"])
    job = client.get("/initialization/jobs").json()["data"]["jobs"][0]
    assert job["status"] == "partial"
    assert job["results"][0]["datasets"][0]["error"] == "permission denied"


def test_cancel_prevents_fetch_and_preserves_terminal_state(api, monkeypatch):
    client, module, threads = api
    fetch = MagicMock()
    monkeypatch.setattr(module.datayes_sync, "sync_datayes", fetch)
    job = client.post("/initialization/jobs", json=request()).json()["data"]["job"]
    assert (
        client.post(f"/initialization/jobs/{job['job_id']}/cancel").json()["data"][
            "job"
        ]["status"]
        == "cancelling"
    )
    threads[0]["target"](*threads[0]["args"])
    assert (
        client.get(f"/initialization/jobs/{job['job_id']}").json()["data"]["job"][
            "status"
        ]
        == "cancelled"
    )
    fetch.assert_not_called()


def test_admin_dependency_protects_configuration_and_start(api):
    client, _, threads = api
    client.app.dependency_overrides.clear()
    assert client.get("/initialization").status_code == 401
    assert client.post("/initialization/jobs", json=request()).status_code == 401
    assert not threads
