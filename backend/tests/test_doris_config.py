"""Doris 连接配置接口权限、密码脱敏和映射校验。"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_DORIS_CONFIG_FILE", str(tmp_path / "doris.json"))
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    for name in (
        "DORIS_PASSWORD",
        "DORIS_FE_HOST",
        "DORIS_FE_QUERY_PORT",
        "DORIS_USER",
        "DORIS_DATABASE",
    ):
        monkeypatch.delenv(name, raising=False)
    auth = types.ModuleType("backend.services.api.user_app.middleware.auth")

    async def require_admin():
        raise HTTPException(401, "Authentication required")

    auth.require_admin = require_admin
    monkeypatch.setitem(sys.modules, auth.__name__, auth)
    path = (
        Path(__file__).resolve().parents[1]
        / "services/api/routers/admin/doris_config.py"
    )
    name = "_test_doris_config_router"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), app, require_admin


def payload():
    return {
        "host": "doris.test",
        "port": 9030,
        "database": "market",
        "user": "reader",
        "password": "test-only-password",
        "dataset_mappings": {
            "daily_forward": {
                "table": "bars",
                "columns": {"symbol": "code", "close": "px"},
            },
        },
    }


def test_config_requires_existing_admin_session(api):
    client, _, _ = api
    assert client.get("/config").status_code == 401
    assert client.post("/config", json=payload()).status_code == 401


def test_save_roundtrip_never_echoes_password_and_retains_it_when_omitted(api):
    client, app, require_admin = api
    app.dependency_overrides[require_admin] = lambda: {"user_id": "admin"}
    first = client.post("/config", json=payload())
    assert first.status_code == 200
    assert "test-only-password" not in first.text
    assert first.json()["data"]["password_configured"]
    changed = payload()
    changed.pop("password")
    changed["dataset_mappings"]["daily_forward"]["columns"]["close"] = "close_price"
    second = client.post("/config", json=changed)
    assert second.status_code == 200
    data = client.get("/config").json()["data"]
    assert data["password_configured"]
    assert (
        data["connection"]["dataset_mappings"]["daily_forward"]["columns"]["close"]
        == "close_price"
    )
    assert "password" not in data["connection"]


def test_mapping_cannot_inject_sql_or_a_local_file_path(api):
    client, app, require_admin = api
    app.dependency_overrides[require_admin] = lambda: {"user_id": "admin"}
    request = payload()
    request["dataset_mappings"]["daily_forward"]["table"] = "bars; DROP TABLE bars"
    assert client.post("/config", json=request).status_code == 422
    request = payload()
    request["dataset_mappings"]["../../outside"] = {"table": "bars"}
    assert client.post("/config", json=request).status_code == 422
