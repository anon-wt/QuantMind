"""Local admission and execution of the fixed statistical mining worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, Request

from backend.services.engine.alpha_agent.statistical.configuration import (
    STATISTICAL_PARAMETER_KEYS,
    statistical_config_payload,
)
from backend.services.engine.alpha_agent.statistical.worker import (
    _atomic_json,
    digest_file,
)
from backend.services.engine.alpha_agent.task_spec import (
    IdempotencyConflict,
    MiningTaskSpec,
)


def _task_store():
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        AlphaMiningTaskPersistence,
    )

    return AlphaMiningTaskPersistence()


async def submit_statistical(request: Request, values: dict, algorithm, owner: dict):
    from backend.services.engine.alpha_agent.launcher import get_launcher
    from backend.services.engine.alpha_agent.public_task import public_task
    from backend.services.engine.alpha_agent.resource_budget import submit_budget
    from backend.shared.qlib_paths import (
        is_qlib_provider_ready,
        resolve_qlib_provider_uri,
    )
    from backend.shared.resource_lease import LeaseUnavailable
    from backend.shared.stock_pool.resolver import ResolveContext, resolver
    from backend.shared.stock_utils import StockCodeUtil
    from backend.shared.storage_quota import StorageQuotaExceeded, check_user_quota

    if values.get("data_source") not in (None, "", "qlib_bin"):
        raise HTTPException(400, "统计挖掘仅支持公共 Qlib 日线数据")
    if values.get("auto_materialize"):
        raise HTTPException(400, "统计挖掘完成后请显式提交因子数据生成任务")
    key = request.headers.get("Idempotency-Key", "").strip() or None
    if key is not None and (len(key) > 128 or not key.isascii()):
        raise HTTPException(400, "Idempotency-Key 不合法")
    try:
        await asyncio.to_thread(
            check_user_quota,
            get_launcher()._log_dir,
            owner["tenant_id"],
            owner["user_id"],
            quota_gb=float(os.getenv("FACTOR_QUOTA_GB", "30")),
        )
    except StorageQuotaExceeded as exc:
        raise HTTPException(507, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "暂时无法核验存储配额，请稍后重试") from exc
    params = {
        "algorithm": "statistical",
        "market": "a_share",
        "universe": values.get("universe", "all_a"),
        "data_source": "qlib_bin",
        "loop_n": 1,
        "direction": "",
        "auto_materialize": False,
        **{
            k: values[k]
            for k in ("mining_start", "mining_end", "valid_start", "valid_end")
            if values.get(k) is not None
        },
    }
    try:
        options = await asyncio.to_thread(
            statistical_config_payload,
            {k: values[k] for k in STATISTICAL_PARAMETER_KEYS if k in values},
        )
        pool_ref = params["universe"]
        if not pool_ref.startswith(("pool:", "pool_id:", "list:", "file:")):
            pool_ref = f"pool:{pool_ref}"
        pool = await resolver.resolve(pool_ref, ResolveContext(**owner), strict=True)
        symbols = (
            None
            if pool.unfiltered
            else [StockCodeUtil.to_qlib(symbol) for symbol in pool.symbols]
        )
        provider = resolve_qlib_provider_uri("CN")
        if not is_qlib_provider_ready(provider):
            raise ValueError("本地 Qlib 数据尚未就绪，请先同步数据并构建 Qlib 缓存")
        if symbols is None:
            from backend.services.engine.alpha_agent.statistical.panel import (
                qlib_instruments,
            )

            # The UI's all_a pool corresponds to Qlib's all instrument file.
            # Freeze its membership as well as explicit/custom pools.
            symbols = await asyncio.to_thread(
                qlib_instruments, "all", qlib_dir=provider
            )
        if not symbols or len(symbols) > 20000:
            raise ValueError("所选股票池为空或超过统计挖掘容量")
        spec = MiningTaskSpec.build(
            algorithm="statistical",
            algorithm_version=algorithm.version,
            request=values,
            params={**params, **options},
            data_snapshot={
                "status": "preparing",
                "qlib_provider_uri": provider,
                "symbols": symbols,
                "pool_checksum": pool.checksum,
            },
            request_key=key,
        )
        task_id = uuid.uuid4().hex[:16]
        await asyncio.to_thread(submit_budget, task_id, **owner)
        store = _task_store()
        task_id = await store.create(
            task_id,
            **owner,
            specification=spec,
            market="a_share",
            universe=params["universe"],
            loop_n=1,
            direction="",
            data_source="qlib_bin",
            status="queued",
            phase="queued",
            progress="等待统计挖掘执行",
            executor_json="{}",
        )
        row = await store.get(task_id, **owner, consistent=True)
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LeaseUnavailable as exc:
        raise HTTPException(503, "统计挖掘队列暂不可用，请稍后确认原提交") from exc
    if row is None:
        raise HTTPException(503, "任务已提交，状态暂无法核验，请确认原提交")
    return {"code": 200, "data": {**public_task(row), "algorithm": "statistical"}}


def _read_json(path: Path, limit=4 * 1024 * 1024) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("统计挖掘产物缺失或超过容量")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("统计挖掘产物格式错误")
    return result


def result_factors(task_id: str, fingerprint: str, params: dict, result: dict):
    from backend.services.engine.alpha_agent.statistical.materialization import (
        trusted_statistical_candidate,
    )

    reports = [r for r in result.get("candidates", []) if r.get("selected")]
    composite = result.get("composite")
    if isinstance(composite, dict) and composite.get("eligible"):
        reports.append(composite)
    rows = []
    for report in reports:
        key = report["key"]
        expression = report["expression"]
        metadata = {
            "algorithm": "statistical",
            "task_id": task_id,
            "execution_fingerprint": fingerprint,
            "candidate_key": key,
            "evidence": report,
            "rank_icir": report.get("icir"),
        }
        trusted_statistical_candidate(metadata, expression)
        identity = hashlib.sha256(f"{task_id}:{key}".encode()).hexdigest()[:32]
        rows.append(
            {
                "factor_id": f"stat_{identity}",
                "factor_name": report["name"],
                "factor_code": expression,
                "factor_formulation": expression,
                "rank_ic": report.get("rank_ic_mean"),
                "market": "a_share",
                "universe": params["universe"],
                "data_source": "qlib_bin",
                "date_range": f"{params['mining_start']}~{params['mining_end']}",
                "metadata": metadata,
            }
        )
    return rows


async def run_statistical(launcher, record: dict):
    from backend.services.engine.alpha_agent.launcher import EvolutionTask, TaskStatus
    from backend.services.engine.alpha_agent.queue import capture_controller
    from backend.services.engine.alpha_agent.resource_budget import (
        ExecutionCapacityFull,
        begin_execution,
        record_process_group,
    )
    from backend.shared.process_witness import capture_process

    owner = {k: record[k] for k in ("tenant_id", "user_id")}
    task_id = record["task_id"]
    params = record["params_snapshot"]
    store = _task_store()
    task = EvolutionTask(
        task_id=task_id,
        **owner,
        algorithm="statistical",
        market="a_share",
        universe=record["universe"],
        data_source="qlib_bin",
        loop_n=1,
        **{
            k: params.get(k)
            for k in ("mining_start", "mining_end", "valid_start", "valid_end")
        },
    )
    launcher._tasks[task_id] = task
    witness = await asyncio.to_thread(capture_controller)
    try:
        with begin_execution(
            task_id, **owner, executor_kind="engine-local", witness=witness
        ):
            if not await store.update(
                task_id,
                **owner,
                expected_status=("pending", "queued"),
                status="running",
                phase="data",
                progress="准备统计挖掘数据",
            ):
                return
            task.status = TaskStatus.RUNNING
            work = launcher.task_dir(task_id, **owner)
            work.mkdir(parents=True, exist_ok=True)
            request_path, result_path, progress_path = (
                work / name
                for name in (
                    "statistical_request.json",
                    "statistical_result.json",
                    "statistical_progress.json",
                )
            )
            _atomic_json(
                request_path,
                {
                    "algorithm": "statistical",
                    "params": params,
                    "data_snapshot": record["data_snapshot"],
                    "panel_path": str(work / "statistical_panel.parquet"),
                    "metadata_path": str(work / "statistical_metadata.parquet"),
                },
            )
            process = None
            try:
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k
                    in {
                        "PATH",
                        "HOME",
                        "LANG",
                        "LC_ALL",
                        "TMPDIR",
                        "LD_LIBRARY_PATH",
                        "QM_QUANTDB_DATA_DIR",
                        "QM_DATAYES_DATA_DIR",
                        "QM_DATA_SOURCE_CONFIG_FILE",
                        "QM_STAT_WORKER_MEMORY_BYTES",
                        "QM_STAT_WORKER_CPU_SECONDS",
                    }
                }
                env.update(
                    PYTHONPATH=str(Path(__file__).resolve().parents[4]),
                    PYTHONUNBUFFERED="1",
                    OMP_NUM_THREADS="1",
                    OPENBLAS_NUM_THREADS="1",
                    MKL_NUM_THREADS="1",
                )
                with (work / "subprocess_stdout.log").open("w") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "backend.services.engine.alpha_agent.statistical.worker",
                            str(request_path),
                            str(result_path),
                            str(progress_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=env,
                        start_new_session=True,
                    )
                    task.process = process
                    leader = capture_process(process.pid)
                    if not await asyncio.to_thread(
                        record_process_group,
                        task_id,
                        **owner,
                        pgid=process.pid,
                        leader=leader,
                    ):
                        raise RuntimeError("统计挖掘执行占用已失效")
                    if not await store.set_process_group(
                        task_id, **owner, pgid=process.pid, leader=leader
                    ):
                        raise RuntimeError("统计挖掘任务状态已变更")
                    deadline = time.monotonic() + 4 * 3600
                    sealed = False
                    last_progress = None
                    while process.poll() is None:
                        row = await store.get(task_id, **owner, consistent=True)
                        if (
                            task._cancel_requested
                            or not row
                            or row["status"] == "stopping"
                        ):
                            task._cancel_requested = True
                            break
                        if time.monotonic() > deadline:
                            raise TimeoutError("统计挖掘超过 4 小时执行上限")
                        prepared = work / "statistical_prepared.json"
                        if not sealed and prepared.is_file():
                            snapshot = {
                                **record["data_snapshot"],
                                **_read_json(prepared),
                                "status": "frozen",
                            }
                            for name, size, key in (
                                (
                                    "statistical_panel.parquet",
                                    2 * 1024**3,
                                    "panel_sha256",
                                ),
                                (
                                    "statistical_metadata.parquet",
                                    64 * 1024**2,
                                    "metadata_sha256",
                                ),
                            ):
                                if digest_file(work / name, size) != snapshot[key]:
                                    raise ValueError("冻结数据校验失败")
                            await store.seal_data_snapshot(
                                task_id, **owner, data_snapshot=snapshot
                            )
                            frozen = await store.get_for_dispatch(task_id)
                            _atomic_json(
                                work / "statistical_sealed.json",
                                frozen["data_snapshot"],
                            )
                            sealed = True
                        if progress_path.is_file():
                            progress = _read_json(progress_path, 64 * 1024)
                            if progress != last_progress:
                                task.phase = str(progress["stage"])
                                task.progress = str(progress["detail"])
                                task.progress_pct = max(
                                    task.progress_pct,
                                    min(99, int(progress.get("progress_pct", 5))),
                                )
                                await store.update_progress(
                                    task_id,
                                    **owner,
                                    snapshot={
                                        "phase": task.phase,
                                        "progress": task.progress,
                                        "progress_pct": task.progress_pct,
                                    },
                                )
                                launcher._persist_task(task)
                                last_progress = progress
                        await asyncio.sleep(1)
                    if task._cancel_requested:
                        return
                    if process.returncode != 0:
                        with (work / "subprocess_stdout.log").open("rb") as tail:
                            tail.seek(
                                max(
                                    0,
                                    (work / "subprocess_stdout.log").stat().st_size
                                    - 3000,
                                )
                            )
                            detail = tail.read().decode(errors="replace")
                        raise RuntimeError(f"统计计算进程失败：{detail[-1800:]}")
                result = _read_json(result_path)
                frozen = await store.get_for_dispatch(task_id)
                fingerprint = frozen["execution_fingerprint"]
                rows = result_factors(task_id, fingerprint, params, result)
                result.update(
                    task_id=task_id,
                    market="a_share",
                    total_factors=len(rows),
                    persisted_factors=len(rows),
                    message=f"统计挖掘完成，入选 {len(rows)} 个因子",
                )
                completed = await store.complete_with_result(
                    task_id,
                    **owner,
                    execution_fingerprint=fingerprint,
                    result=result,
                    factors=rows,
                )
                if completed:
                    task.status = TaskStatus.COMPLETED
                    task.phase = "completed"
                    task.progress_pct = 100
                    task.progress = f"统计挖掘完成，入选 {len(rows)} 个因子"
                    task.result = {
                        "total_factors": len(rows),
                        "persisted_factors": len(rows),
                        "message": task.progress,
                    }
            except asyncio.CancelledError:
                task._cancel_requested = True
                raise
            except Exception as exc:
                task.status = TaskStatus.FAILED
                task.error_message = str(exc)[:2000]
                task.progress = f"统计挖掘失败：{task.error_message}"
                await store.update(
                    task_id,
                    **owner,
                    expected_status=("running",),
                    status="failed",
                    phase="failed",
                    error_message=task.error_message,
                    progress=task.progress,
                )
            finally:
                if process is not None:
                    await asyncio.shield(launcher._terminate_process_group(process.pid))
                    await asyncio.to_thread(process.wait)
                task.process = None
                if task._cancel_requested:
                    task.status = TaskStatus.CANCELLED
                    await store.confirm_cancelled(task_id, **owner)
                launcher._persist_task(task)
    except ExecutionCapacityFull:
        return
