"""个人中心的数据平台 Doris 配置；沿用现有管理员配置权限。"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.data_platform import doris_source

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/config")
async def get_config():
    try:
        return {"success": True, "data": doris_source.public_config()}
    except (ValueError, OSError):
        raise HTTPException(500, "读取 Doris 配置失败，请检查配置文件") from None


@router.post("/config")
async def save_config(payload: doris_source.DorisConfigRequest):
    try:
        await asyncio.to_thread(doris_source.save_config, payload)
        return {"success": True, "data": doris_source.public_config()}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError:
        raise HTTPException(500, "保存 Doris 配置失败，请检查配置目录权限") from None


def _inspect(payload, discover):
    client = doris_source.DorisClient(payload)
    try:
        data = (
            client.discover()
            if discover
            else {"connected": not client._query("SELECT 1 AS connected").empty}
        )
        return {"success": True, "data": data}
    finally:
        client.close()


@router.post("/test")
async def test_connection(payload: doris_source.DorisConfigRequest):
    try:
        return await asyncio.to_thread(_inspect, payload, False)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/discover")
async def discover_tables(payload: doris_source.DorisConfigRequest):
    try:
        return await asyncio.to_thread(_inspect, payload, True)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
