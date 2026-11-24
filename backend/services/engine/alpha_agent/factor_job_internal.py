"""Engine execution receipt for the existing Celery factor-job dispatcher."""

from __future__ import annotations

import asyncio
import hmac
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.services.engine.auth_context import get_authenticated_identity
from backend.shared.process_witness import capture_process
from backend.shared.secret_redaction import redact_text

router = APIRouter(prefix="/api/v1/internal/alpha-factor-jobs")


def _internal_secret() -> str:
    from backend.shared.auth import get_internal_call_secret

    return get_internal_call_secret()


class FactorJobExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=128)
    execution_generation: int = Field(ge=1, strict=True)
    dispatch_token: str = Field(min_length=1, max_length=128)


@router.post("/run")
async def run_factor_job(payload: FactorJobExecutionRequest, request: Request):
    secret = _internal_secret()
    if not secret or not hmac.compare_digest(
        request.headers.get("X-Internal-Call", ""), secret
    ):
        raise HTTPException(403, "Internal factor dispatcher required")
    user_id, tenant_id = get_authenticated_identity(request)
    from .factor_jobs import AlphaFactorJobPersistence
    from .factor_job_budget import POOL, get_budget
    from .factor_job_executor import execute_factor_backtest, execute_factor_batch

    jobs = AlphaFactorJobPersistence()
    owner = {"tenant_id": tenant_id, "user_id": user_id}
    row = await jobs.get(payload.job_id, **owner, consistent=True)
    if row is None:
        raise HTTPException(404, "Factor job not found")
    if row["execution_generation"] != payload.execution_generation:
        raise HTTPException(409, "Factor execution generation changed")
    if row["status"] in {"completed", "failed", "cancelled"}:
        return {"job_id": payload.job_id, "status": row["status"]}
    budget = get_budget()
    lease = await asyncio.to_thread(
        budget.store.get_lease,
        POOL,
        payload.job_id,
        tenant_id,
        user_id,
        expected_dispatch_token=payload.dispatch_token,
    )
    if lease is None:
        raise HTTPException(409, "Factor execution lease changed")
    request_id = uuid.uuid4().hex
    claim = await asyncio.to_thread(
        budget.store.claim_engine_execution,
        POOL,
        lease,
        dispatch_token=payload.dispatch_token,
        request_id=request_id,
        engine_process=capture_process(),
        execution_generation=payload.execution_generation,
    )
    if claim == "stop_requested":
        await jobs.confirm_cancelled(
            payload.job_id, **owner, generation=payload.execution_generation
        )
        return {"job_id": payload.job_id, "status": "cancelled"}
    if claim != "claimed":
        return {"job_id": payload.job_id, "status": row["status"]}
    if row["kind"] not in {"backtest", "validate"}:
        await jobs.finish(
            payload.job_id,
            **owner,
            generation=payload.execution_generation,
            error_code="UnsupportedFactorJob",
            error_message="当前 OSS 执行入口尚未接入此类作业，请联系维护人员；回测名额已释放",
        )
        latest = await jobs.get(payload.job_id, **owner, consistent=True)
        return {"job_id": payload.job_id, "status": latest["status"]}

    body = row["request_snapshot"]["request"]
    items = body.get("factor_items") or [body]
    catalogue_only = all(item.get("factor_kind") == "qlib_expression" for item in items)
    if catalogue_only:
        await asyncio.to_thread(
            budget.store.transition,
            POOL,
            lease,
            "running",
            executor={"trusted_catalogue": True},
            expected_dispatch_token=payload.dispatch_token,
        )
    loop = asyncio.get_running_loop()
    lifecycle = {"create_started": False, "confirmed_stopped": False}

    def progress(snapshot):
        current = budget.store.task_snapshot(POOL, payload.job_id, tenant_id, user_id)
        if not current or current["stop_requested"]:
            raise RuntimeError("Factor job stop requested")
        asyncio.run_coroutine_threadsafe(
            jobs.update_progress(
                payload.job_id,
                **owner,
                generation=payload.execution_generation,
                progress=snapshot,
            ),
            loop,
        ).result(timeout=15)

    stopped = asyncio.Event()

    async def heartbeat():
        while not stopped.is_set():
            try:
                if not await asyncio.to_thread(budget.projection.heartbeat, lease):
                    raise RuntimeError("Factor lease heartbeat lost")
                if not await asyncio.to_thread(
                    budget.store.transition, POOL, lease, "running"
                ):
                    return
            except Exception:
                try:
                    await asyncio.to_thread(
                        budget.store.transition, POOL, lease, "suspect"
                    )
                except Exception:
                    pass
                return
            try:
                await asyncio.wait_for(stopped.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    beat = asyncio.create_task(heartbeat())
    operation = None
    result = None
    failure = None
    try:
        operation = asyncio.create_task(
            asyncio.to_thread(
                execute_factor_batch
                if body.get("factor_items")
                else execute_factor_backtest,
                body,
                **owner,
                lease=lease,
                budget=budget,
                lifecycle=lifecycle,
                progress=progress,
            )
        )
        # A disconnected HTTP caller must not release a still-running thread or
        # let the next delivery start the same factor computation twice.
        while True:
            try:
                result = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise RuntimeError(
                        "Factor execution receipt was interrupted"
                    ) from None
        lifecycle["confirmed_stopped"] = True
    except Exception as exc:
        failure = exc
    finally:
        stopped.set()
        await beat

    try:
        if not lifecycle["create_started"] or lifecycle["confirmed_stopped"]:
            latest = await jobs.get(payload.job_id, **owner, consistent=True)
            if latest["status"] == "stopping":
                await jobs.confirm_cancelled(
                    payload.job_id, **owner, generation=payload.execution_generation
                )
            elif failure is not None:
                await jobs.finish(
                    payload.job_id,
                    **owner,
                    generation=payload.execution_generation,
                    error_code=type(failure).__name__,
                    error_message=redact_text(str(failure))[:2000],
                )
            else:
                await jobs.finish(
                    payload.job_id,
                    **owner,
                    generation=payload.execution_generation,
                    result=result,
                )
    finally:
        await asyncio.to_thread(
            budget.store.mark_engine_request_finished,
            POOL,
            lease,
            dispatch_token=payload.dispatch_token,
            request_id=request_id,
            execution_generation=payload.execution_generation,
        )
    latest = await jobs.get(payload.job_id, **owner, consistent=True)
    return {"job_id": payload.job_id, "status": latest["status"]}
