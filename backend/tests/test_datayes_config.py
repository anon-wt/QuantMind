"""Datayes 配置：权限门、密钥持久化/脱敏，以及存量数据源选择兼容。"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.shared import data_source_config, runtime_secrets


@pytest.fixture
def config_client(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    monkeypatch.setenv("QM_DATA_SOURCE_CONFIG_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setenv("DATAYES_TOKEN", "")
    monkeypatch.setattr(runtime_secrets, "_injected_keys", set())

    # 只加载被测路由，隔离管理台其他模块的数据库及认证服务依赖。
    auth = types.ModuleType("backend.services.api.user_app.middleware.auth")

    async def require_admin():
        raise HTTPException(status_code=401, detail="Authentication required")

    auth.require_admin = require_admin
    monkeypatch.setitem(sys.modules, auth.__name__, auth)
    spec = importlib.util.spec_from_file_location(
        "_datayes_config_test",
        ROOT / "backend/services/api/routers/admin/datayes_config.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.state.datayes_config = module
    app.include_router(module.router, prefix="/datayes")
    app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
    with TestClient(app) as client:
        yield client


def test_unconfigured_token(config_client):
    response = config_client.get("/datayes/config")
    assert response.status_code == 200
    assert response.json()["data"]["token_configured"] is False
    assert response.json()["data"]["token_masked"] == ""


def test_save_persists_masks_and_keeps_source_disabled(
    config_client, tmp_path, monkeypatch
):
    token = "test-datayes-token-1234567890"
    sources = data_source_config.list_sources("A")
    assert next(s for s in sources if s["source"] == "datayes")["enabled"] is False
    response = config_client.post("/datayes/config", json={"token": f"  {token}  "})
    assert response.status_code == 200
    assert response.json()["data"]["token_configured"] is True
    assert response.json()["data"]["token_masked"] == runtime_secrets.mask_secret(token)
    assert token not in response.text
    assert (tmp_path / "runtime.env").read_text() == f"DATAYES_TOKEN={token}\n"
    assert not data_source_config.is_source_enabled("A", "datayes")

    # 模拟新进程仅凭落盘文件读取配置，返回值依旧不暴露明文。
    monkeypatch.delenv("DATAYES_TOKEN")
    reloaded = config_client.get("/datayes/config")
    assert reloaded.json()["data"]["token_configured"] is True
    assert token not in reloaded.text

    replacement = "replacement-datayes-token-0987654321"
    response = config_client.post("/datayes/config", json={"token": replacement})
    assert response.status_code == 200
    assert response.json()["data"]["token_masked"] == runtime_secrets.mask_secret(
        replacement
    )
    assert runtime_secrets.get_secret("DATAYES_TOKEN") == replacement
    assert token not in (tmp_path / "runtime.env").read_text()


@pytest.mark.parametrize(
    "token",
    ["", "   ", "abc\nOTHER_KEY=bad", "\nabc", "abc\r", "a b", "abc\0", "x" * 4097],
)
def test_invalid_token_is_not_written(config_client, tmp_path, token):
    response = config_client.post("/datayes/config", json={"token": token})
    assert response.status_code == 400
    assert not (tmp_path / "runtime.env").exists()


@pytest.mark.parametrize("method", ["get", "post"])
def test_config_routes_require_admin_dependency(config_client, method):
    config_client.app.dependency_overrides.clear()
    response = config_client.request(
        method, "/datayes/config", json={"token": "test-token"}
    )
    assert response.status_code == 401


def test_storage_error_does_not_expose_token(config_client, monkeypatch):
    token = "sensitive-test-datayes-token"

    def fail_write(*args):
        raise OSError(f"could not write {token}")

    monkeypatch.setattr(
        config_client.app.state.datayes_config, "set_secret", fail_write
    )
    response = config_client.post("/datayes/config", json={"token": token})
    assert response.status_code == 500
    assert token not in response.text


def test_datayes_selection_preserves_existing_sources(tmp_path, monkeypatch):
    path = tmp_path / "sources.json"
    monkeypatch.setenv("QM_DATA_SOURCE_CONFIG_FILE", str(path))
    original = {"A": {"quantdb": True, "yahoo": False}, "HK": {"ccass": True}}
    path.write_text(json.dumps(original), encoding="utf-8")
    datayes = next(
        s for s in data_source_config.list_sources("A") if s["source"] == "datayes"
    )
    assert datayes["enabled"] is False

    data_source_config.save_sources("A", {"datayes": True})
    assert data_source_config.is_source_enabled("A", "datayes")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["A"] == {**original["A"], "datayes": True}
    assert saved["HK"] == original["HK"]
    data_source_config.save_sources("A", {"datayes": False})
    assert not data_source_config.is_source_enabled("A", "datayes")


def test_a_share_source_api_accepts_datayes(config_client, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_quantdb_console_test",
        ROOT / "backend/services/api/routers/admin/quantdb_console.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    config_client.app.include_router(module.router, prefix="/quantdb")
    # 构建 schema 也须能解析源配置请求模型。
    assert "/quantdb/data-sources" in config_client.app.openapi()["paths"]
    response = config_client.get("/quantdb/data-sources")
    assert response.status_code == 200
    sources = response.json()["data"]["sources"]
    assert next(s for s in sources if s["source"] == "datayes")["enabled"] is False
    response = config_client.post(
        "/quantdb/data-sources", json={"sources": {"datayes": True}}
    )
    assert response.status_code == 200
    assert response.json()["data"]["sources"]["datayes"] is True
    assert data_source_config.is_source_enabled("A", "quantdb") is True
