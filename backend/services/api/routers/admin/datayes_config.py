"""Datayes 数据商城 Token 配置：持久化凭据并脱敏返回。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.runtime_secrets import get_secret, mask_secret, set_secret
from backend.services.engine.data_platform.datayes_catalog import public_catalog

router = APIRouter(dependencies=[Depends(require_admin)])


class DatayesConfigRequest(BaseModel):
    token: SecretStr


def _config_payload() -> dict:
    token = get_secret("DATAYES_TOKEN")
    return {
        "success": True,
        "data": {
            "token_configured": bool(token),
            "token_masked": mask_secret(token),
            "datasets": public_catalog(),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


@router.get("/config")
async def get_datayes_config():
    """仅向管理员返回 Token 是否已配置及掩码。"""
    return _config_payload()


@router.post("/config")
async def save_datayes_config(payload: DatayesConfigRequest):
    """写入共享 runtime.env；不验证账号权限或自动启用数据源。"""
    raw_token = payload.token.get_secret_value()
    token = raw_token.strip()
    if (
        not token
        or len(token) > 4096
        or not token.isprintable()
        or any(c.isspace() for c in token)
    ):
        raise HTTPException(status_code=400, detail="请输入有效的 Datayes Token")
    if "\r" in raw_token or "\n" in raw_token:
        raise HTTPException(status_code=400, detail="Token 不能包含换行")
    try:
        set_secret("DATAYES_TOKEN", token)
    except OSError:
        raise HTTPException(
            status_code=500, detail="保存 Datayes Token 失败，请检查配置目录权限"
        ) from None
    return _config_payload()
