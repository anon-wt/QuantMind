"""A 股引导初始化：按已保存的数据源配置分发 QuantDB / Datayes / Doris 任务。"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.data_platform import datayes_sync, doris_source
from backend.shared.data_source_config import list_sources
from backend.shared.quantdb_datasets import DATASETS
from backend.shared.runtime_secrets import get_secret

router = APIRouter(dependencies=[Depends(require_admin)])
log = logging.getLogger(__name__)
_jobs: dict[str, dict] = {}
_lock = threading.RLock()
_all = {spec.dataset for spec in DATASETS}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload(data):
    return {"success": True, "data": data}


class InitializationRequest(BaseModel):
    sources: list[Literal["quantdb", "datayes", "doris"]] = Field(
        ..., min_length=1, max_length=3
    )
    datasets: list[str] | None = None
    start_date: date = date(2016, 1, 1)
    end_date: date = Field(default_factory=date.today)
    incremental: bool = True


def _catalog():
    from backend.services.engine.data_platform.modelscope_dataset_sync import (
        resolve_data_root,
    )

    sources = {row["source"]: row for row in list_sources("A")}
    providers = []
    doris_supported = doris_source.supported_datasets()
    for name, label, root, supported in (
        ("quantdb", "QuantDB", resolve_data_root(), _all),
        (
            "datayes",
            "Datayes 通联数据",
            datayes_sync.data_root(),
            datayes_sync.SUPPORTED,
        ),
        ("doris", "Doris 数据库", doris_source.data_root(), doris_supported),
    ):
        local = 0
        for spec in DATASETS:
            if spec.dataset in supported:
                directory = root / spec.rel_dir
                if (
                    directory.is_dir()
                    and next(directory.glob("**/*.parquet"), None) is not None
                ):
                    local += 1
        providers.append(
            {
                "source": name,
                "label": label,
                "root": str(root),
                "enabled": sources[name]["enabled"],
                "configured": name == "quantdb" or (
                    bool(doris_supported) if name == "doris" else bool(get_secret("DATAYES_TOKEN"))
                ),
                "dataset_count": len(supported),
                "local_dataset_count": local,
            }
        )
    return {
        "providers": providers,
        "datasets": [
            {
                "dataset": spec.dataset,
                "name": spec.name,
                "group": spec.group,
                "sources": ["quantdb"]
                + (["datayes"] if spec.dataset in datayes_sync.SUPPORTED else [])
                + (["doris"] if spec.dataset in doris_supported else []),
                "doris_note": "按已保存的 Doris 表映射同步；因子字段保留原始定义"
                if spec.dataset in doris_supported else "请先发现并保存对应表映射",
                "datayes_note": "由行情与估值派生（不含 QuantDB 专有因子）"
                if spec.dataset in datayes_sync.DERIVED
                else (
                    "数据字段与历史范围以通联接口及订阅权限为准"
                    if spec.dataset in datayes_sync.SUPPORTED
                    else datayes_sync.UNSUPPORTED_REASON
                ),
            }
            for spec in DATASETS
        ],
        "timestamp": _now(),
    }


@router.get("/initialization")
async def get_initialization():
    return _payload(await asyncio.to_thread(_catalog))


def _update(job_id, **fields):
    with _lock:
        _jobs[job_id].update(fields)


def _plan(source, requested):
    selected = (
        requested if requested is not None else [spec.dataset for spec in DATASETS]
    )
    supported = (
        _all if source == "quantdb" else
        doris_source.supported_datasets() if source == "doris" else datayes_sync.SUPPORTED
    )
    selected = list(dict.fromkeys(name for name in selected if name in supported))
    if source == "doris" and selected:
        return doris_source.plan_datasets(selected)
    return datayes_sync.plan_datasets(selected) if source == "datayes" and selected else selected


def _run(job_id: str, payload: InitializationRequest):
    def cancelled():
        with _lock:
            return _jobs[job_id].get("cancel_requested", False)

    results = []
    try:
        for source in payload.sources:
            if cancelled():
                break
            selected = _plan(source, payload.datasets)
            if not selected:
                continue
            _update(job_id, source=source, current=f"{source} 初始化中")

            def progress(event, _source=source, **details):
                with _lock:
                    job = _jobs[job_id]
                    job["stage"] = event
                    job["detail"] = details
                    job["current"] = (
                        details.get("message") or details.get("dataset") or _source
                    )
                    if event == "dataset_done":
                        job["done"] += 1

            try:
                if source == "quantdb":
                    from backend.services.engine.data_platform.modelscope_dataset_sync import (
                        init_from_modelscope,
                    )

                    summary = init_from_modelscope(
                        selected, progress_cb=progress, should_cancel=cancelled
                    )
                    if not summary.get("cancelled"):
                        missing = set(selected) - set(summary.get("datasets", {}))
                        for name in missing:
                            summary.setdefault("datasets", {})[name] = {"errors": 1}
                        summary["errors"] = summary.get("errors", 0) + len(missing)
                elif source == "doris":
                    summary = doris_source.sync_doris(
                        selected, payload.start_date, payload.end_date,
                        incremental=payload.incremental, progress=progress, should_cancel=cancelled,
                    )
                else:
                    summary = datayes_sync.sync_datayes(
                        selected,
                        payload.start_date,
                        payload.end_date,
                        progress=progress,
                        should_cancel=cancelled,
                    )
                results.append({"source": source, **summary})
            except Exception:
                log.exception("A 股 %s 初始化失败", source)
                # 上游异常/请求对象可能含访问凭据，接口只回传稳定的错误文案。
                results.append(
                    {
                        "source": source,
                        "errors": 1,
                        "error": f"{source} 初始化失败，请检查数据权限、服务器网络及后台日志",
                    }
                )
            _update(job_id, results=results)
        status = (
            "cancelled"
            if cancelled() or any(r.get("cancelled") for r in results)
            else (
                "failed"
                if results and all(r.get("error") for r in results)
                else "partial"
                if any(r.get("errors") for r in results)
                else "completed"
            )
        )
        from .quantdb_console import (
            _reset_quantdb_runtime_caches,
            _publish_baseline_after_init,
        )

        _reset_quantdb_runtime_caches()
        _update(job_id, status=status, finished_at=_now(), current=None)
        if status == "completed":
            _publish_baseline_after_init(_jobs, job_id, "a-share-init")
    except Exception:
        log.exception("A 股初始化任务失败")
        _update(
            job_id,
            status="failed",
            error="初始化任务失败，请检查后台日志",
            finished_at=_now(),
        )


@router.post("/initialization/jobs")
async def start_initialization(
    payload: InitializationRequest, current_user: dict = Depends(require_admin)
):
    if len(set(payload.sources)) != len(payload.sources):
        raise HTTPException(400, "数据源不可重复")
    if (
        payload.start_date > payload.end_date
        or payload.start_date < date(1990, 1, 1)
        or payload.end_date > date.today()
    ):
        raise HTTPException(400, "请选择 1990 年至今天之间的有效日期范围")
    enabled = {row["source"] for row in list_sources("A") if row["enabled"]}
    if set(payload.sources) - enabled:
        raise HTTPException(409, "数据源选择已变化，请刷新后重新选择")
    if "datayes" in payload.sources and not get_secret("DATAYES_TOKEN"):
        raise HTTPException(400, "请先到个人中心 → 数据平台配置 Datayes Token")
    if "doris" in payload.sources and not doris_source.supported_datasets():
        raise HTTPException(400, "请先到个人中心 → 数据平台配置 Doris 并保存表映射")
    if payload.datasets is not None:
        if not payload.datasets or set(payload.datasets) - _all:
            raise HTTPException(400, "请选择有效的数据集")
        if (
            payload.sources == ["datayes"]
            and set(payload.datasets) - datayes_sync.SUPPORTED
        ):
            raise HTTPException(400, "所选数据集包含 Datayes 暂不支持的专有数据")
        supported = set().union(*(
            _all if source == "quantdb" else doris_source.supported_datasets()
            if source == "doris" else datayes_sync.SUPPORTED
            for source in payload.sources
        ))
        if set(payload.datasets) - supported:
            raise HTTPException(400, "所选数据集缺少可用数据源或 Doris 表映射")
    with _lock:
        if any(job["status"] in ("running", "cancelling") for job in _jobs.values()):
            raise HTTPException(409, "已有 A 股初始化任务正在执行")
        job_id = uuid4().hex
        job = {
            "job_id": job_id,
            "sources": payload.sources,
            "status": "running",
            "done": 0,
            "total": sum(
                len(_plan(source, payload.datasets)) for source in payload.sources
            ),
            "stage": "starting",
            "current": "正在启动",
            "results": [],
            "started_at": _now(),
            "started_by": str(current_user.get("user_id", "admin")),
            "cancel_requested": False,
            "incremental": payload.incremental,
        }
        _jobs[job_id] = job
        for old in list(_jobs)[:-20]:
            _jobs.pop(old, None)
        result = copy.deepcopy(job)
    threading.Thread(target=_run, args=(job_id, payload), daemon=True).start()
    return _payload({"job": result})


@router.get("/initialization/jobs")
async def list_initialization_jobs():
    with _lock:
        return _payload({"jobs": copy.deepcopy(list(reversed(_jobs.values())))})


@router.get("/initialization/jobs/{job_id}")
async def get_initialization_job(job_id: str):
    with _lock:
        if job_id not in _jobs:
            raise HTTPException(404, "初始化任务不存在或服务已重启")
        return _payload({"job": copy.deepcopy(_jobs[job_id])})


@router.post("/initialization/jobs/{job_id}/cancel")
async def cancel_initialization_job(job_id: str):
    with _lock:
        if job_id not in _jobs:
            raise HTTPException(404, "初始化任务不存在")
        job = _jobs[job_id]
        if job["status"] in ("running", "cancelling"):
            job.update(status="cancelling", cancel_requested=True)
        return _payload({"job": copy.deepcopy(job)})
