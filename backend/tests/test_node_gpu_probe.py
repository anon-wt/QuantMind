"""CPU 部署关闭容器 GPU 探针，保留默认 GPU 探测与缓存。"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.services.engine.training.node_manager import NodeStatus


def test_disabled_probe_does_not_create_container_or_return_cached_gpu(monkeypatch):
    monkeypatch.setenv("TRAINING_DOCKER_GPU_PROBE", "false")
    monkeypatch.setattr(NodeStatus, "_gpu_probe_cache", (float("inf"), [{"name": "GPU"}]))
    client = MagicMock()

    assert asyncio.run(NodeStatus._probe_gpu_via_docker(client, "trainer")) == []
    client.containers.run.assert_not_called()


def test_default_probe_detects_gpu_and_reuses_cache(monkeypatch):
    monkeypatch.delenv("TRAINING_DOCKER_GPU_PROBE", raising=False)
    monkeypatch.setattr(NodeStatus, "_gpu_probe_cache", None)
    monkeypatch.setitem(
        sys.modules, "docker", SimpleNamespace(types=SimpleNamespace(DeviceRequest=dict))
    )
    client = MagicMock()
    client.containers.run.return_value = b"12, 1024, 24576, 45, NVIDIA RTX 4090\n"

    async def run():
        first = await NodeStatus._probe_gpu_via_docker(client, "trainer")
        second = await NodeStatus._probe_gpu_via_docker(client, "trainer")
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert first[0]["name"] == "NVIDIA RTX 4090"
    assert first[0]["mem_total_mb"] == 24576
    client.containers.run.assert_called_once()
    assert client.containers.run.call_args.kwargs["entrypoint"] == ["nvidia-smi"]
