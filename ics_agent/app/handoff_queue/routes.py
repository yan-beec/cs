"""仅管理员可访问的排队配置与追加式修改历史接口。"""
import asyncio
import time
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from ics_agent.app.core.auth import AdminPrincipal, require_admin
from .runtime import get_handoff_queue_runtime
from .policy import validate_config, ConfigError
from .repository import ConfigConflict
router = APIRouter(prefix='/admin/api/handoff-queue', tags=['handoff-queue'])

class SaveConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=1, strict=True)
    config: dict

def runtime():
    value = get_handoff_queue_runtime()
    if not value.ready or value.service is None:
        raise HTTPException(503, '排队设置暂不可用，请稍后重试。')
    return value

@router.get('/config')
async def get_config(principal: AdminPrincipal=Depends(require_admin)):
    value = runtime()
    try:
        current = await asyncio.to_thread(value.repository.current_config)
        if current is None:
            raise RuntimeError('配置未初始化')
        return current
    except Exception:
        raise HTTPException(503, '排队设置读取失败，请稍后重试。') from None

@router.put('/config')
async def put_config(payload: SaveConfigRequest, principal: AdminPrincipal=Depends(require_admin)):
    try:
        validate_config(payload.config)
    except ConfigError as error:
        raise HTTPException(422, error.errors) from None
    value = runtime()
    try:
        result = await asyncio.to_thread(value.repository.save_config, payload.expected_version, payload.config, principal.username)
        if result['version'] >= value.service.cached['version']:
            value.service.cached = result
            value.service.loaded_at = time.monotonic()
        return result
    except ConfigConflict:
        raise HTTPException(409, '配置已被他人修改，请刷新后重试。') from None
    except Exception:
        raise HTTPException(503, '排队设置保存失败，请稍后重试。') from None

@router.get('/history')
async def get_history(limit: int=Query(20, ge=1, le=100), principal: AdminPrincipal=Depends(require_admin)):
    value = runtime()
    try:
        return {'items': await asyncio.to_thread(value.repository.history, limit)}
    except Exception:
        raise HTTPException(503, '修改记录读取失败，请稍后重试。') from None
