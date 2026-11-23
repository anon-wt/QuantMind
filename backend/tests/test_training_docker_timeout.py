"""训练容器准备使用独立的 Docker 等待上限，兼容慢速 vfs 镜像复制。"""

from unittest.mock import MagicMock

import pytest

from backend.services.engine.training import local_docker_orchestrator as module


@pytest.mark.parametrize("configured, expected", [(None, 600), ("1200", 1200)])
def test_training_client_uses_provisioning_timeout(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("TRAINING_DOCKER_API_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("TRAINING_DOCKER_API_TIMEOUT_SECONDS", configured)
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "test-internal-only")
    factory = MagicMock()
    monkeypatch.setattr(module, "DockerClient", factory)
    monkeypatch.setattr(module, "TrainingRunLogStream", MagicMock())

    orchestrator = module.LocalDockerOrchestrator()

    factory.from_env.assert_called_once_with(timeout=expected)
    assert orchestrator.docker_api_timeout == expected
    assert orchestrator.docker is factory.from_env.return_value


@pytest.mark.parametrize("configured", ["0", "-1", "invalid", ""])
def test_invalid_timeout_keeps_finite_default(monkeypatch, configured):
    monkeypatch.setenv("TRAINING_DOCKER_API_TIMEOUT_SECONDS", configured)
    assert module._docker_api_timeout_seconds() == 600
