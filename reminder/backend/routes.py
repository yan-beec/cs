"""挂载在原 ICS 应用上的试验提醒接口。"""

from __future__ import annotations

import asyncio
from collections import deque
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ics_agent.app.chatwoot.client import ChatwootAPIError, get_chatwoot_client
from ics_agent.app.core.auth import require_admin
from ics_agent.app.core.config import get_settings
from .repository import get_repository
from .service import snapshot
from .auth import (
    allowed_agents, credentials_version, login as plugin_login, require_plugin,
    validate_plugin_device,
)

router = APIRouter(tags=["reminder"])
device_bearer = HTTPBearer(auto_error=False)
_login_attempts: dict[str, deque[float]] = {}


def limit_plugin_login(address: str) -> None:
    # 在请求进入密码计算前限制单来源频率；反向代理仍可使用现有 Nginx 限流。
    now = time.monotonic()
    for key in list(_login_attempts):
        if not _login_attempts[key] or _login_attempts[key][-1] <= now - 60:
            del _login_attempts[key]
    if address not in _login_attempts and len(_login_attempts) >= 2048:
        raise HTTPException(429, "登录请求过多，请稍后重试。", headers={"Retry-After": "60"})
    attempts = _login_attempts.setdefault(address, deque())
    while attempts and attempts[0] <= now - 60:
        attempts.popleft()
    if len(attempts) >= 20:
        raise HTTPException(429, "登录请求过多，请一分钟后重试。", headers={"Retry-After": "60"})
    attempts.append(now)


def enabled() -> None:
    if not get_settings().reminder_enabled:
        raise HTTPException(503, "提醒功能未启用，请设置 REMINDER_ENABLED=true 后重启 ICS。")


def account_id() -> str:
    value = get_settings().chatwoot_account_id.strip()
    if not value:
        raise HTTPException(503, "请先配置 CHATWOOT_ACCOUNT_ID。")
    return value


async def device_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(device_bearer),
) -> dict[str, Any]:
    enabled()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "设备认证信息缺失。")
    device = await asyncio.to_thread(get_repository().authenticate_device, credentials.credentials)
    if device is None:
        raise HTTPException(401, "设备凭据无效或已被吊销，请重新绑定。")
    if device.get("auth_kind") == "plugin":
        validate_plugin_device(device, get_settings())
    return device


async def plugin_device_identity(device: dict[str, Any] = Depends(device_identity)) -> dict[str, Any]:
    if device.get("auth_kind") != "plugin":
        raise HTTPException(403, "此设备需要使用插件账号登录后才能切换席位。")
    return device


class BindRequest(BaseModel):
    agent_id: str = Field(pattern=r"^[1-9][0-9]*$")


class TestRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)


class AckRequest(BaseModel):
    version: int = Field(ge=1)
    binding_version: int | None = Field(default=None, ge=1)


class SwitchRequest(BindRequest):
    binding_version: int = Field(ge=1)


class PluginLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


async def plugin_agents() -> list[dict[str, str]]:
    values = await agents()
    allowed = allowed_agents()
    return [item for item in values if allowed is None or item["id"] in allowed]


@router.post("/reminder/plugin/login", dependencies=[Depends(enabled)])
async def login_plugin(payload: PluginLoginRequest, request: Request) -> dict[str, Any]:
    limit_plugin_login(request.client.host if request.client else "unknown")
    return await asyncio.to_thread(plugin_login, payload.username, payload.password)


@router.get("/reminder/plugin/agents", dependencies=[Depends(require_plugin), Depends(enabled)])
async def list_plugin_agents() -> dict[str, Any]:
    return {"account_id": account_id(), "agents": await plugin_agents()}


@router.post("/reminder/plugin/devices", dependencies=[Depends(require_plugin), Depends(enabled)])
async def bind_plugin_device(payload: BindRequest) -> dict[str, Any]:
    target = next((item for item in await plugin_agents() if item["id"] == payload.agent_id), None)
    if target is None:
        raise HTTPException(403, "该坐席不在提醒插件授权范围内。")
    return await asyncio.to_thread(get_repository().bind_device, account_id(), payload.agent_id, target["name"],
                                   auth_kind="plugin", auth_version=credentials_version(get_settings()))


@router.get("/reminder/device/agents")
async def list_device_agents(device: dict[str, Any] = Depends(plugin_device_identity)) -> dict[str, Any]:
    return {"agents": await plugin_agents(), "agent_id": device["agent_id"],
            "agent_name": device["agent_name"], "binding_version": device["binding_version"]}


@router.post("/reminder/device/switch")
async def switch_plugin_device(
    payload: SwitchRequest, device: dict[str, Any] = Depends(plugin_device_identity),
) -> dict[str, Any]:
    target = next((item for item in await plugin_agents() if item["id"] == payload.agent_id), None)
    if target is None:
        raise HTTPException(403, "该坐席不在提醒插件授权范围内。")
    result = await asyncio.to_thread(get_repository().switch_device_agent, device, payload.agent_id,
                                   target["name"], payload.binding_version)
    if result is None:
        raise HTTPException(409, "设备席位已变化或凭据失效，请刷新席位列表后重试。")
    return result


@router.post("/reminder/device/test")
async def create_device_test(device: dict[str, Any] = Depends(plugin_device_identity)) -> dict[str, str]:
    value = await asyncio.to_thread(get_repository().create_test, device["id"], device["account_id"])
    if value is None:
        raise HTTPException(409, "设备席位已变化，请刷新后重试。")
    return {"notification_id": value}


@router.post("/reminder/device/logout")
async def logout_device(device: dict[str, Any] = Depends(device_identity)) -> dict[str, bool]:
    await asyncio.to_thread(get_repository().revoke_device, device["id"], device["account_id"])
    return {"logged_out": True}


async def agents() -> list[dict[str, str]]:
    try:
        values = await asyncio.to_thread(get_chatwoot_client().list_agents, account_id=account_id())
    except ChatwootAPIError as exc:
        raise HTTPException(502, "无法读取 Chatwoot 坐席，请检查用户 API Token 和账户配置。") from exc
    return [{"id": str(item["id"]), "name": str(item.get("name") or item["id"]),
             "availability_status": str(item.get("availability_status") or "unknown")}
            for item in values if isinstance(item, dict) and item.get("id") is not None]


@router.get("/admin/reminder/agents", dependencies=[Depends(require_admin), Depends(enabled)])
async def list_agents() -> dict[str, Any]:
    return {"account_id": account_id(), "agents": await agents()}


@router.post("/admin/reminder/devices", dependencies=[Depends(require_admin), Depends(enabled)])
async def bind_device(payload: BindRequest) -> dict[str, Any]:
    target = next((item for item in await agents() if item["id"] == payload.agent_id), None)
    if target is None:
        raise HTTPException(400, "坐席不属于当前 Chatwoot 账户。")
    return await asyncio.to_thread(get_repository().bind_device, account_id(), payload.agent_id, target["name"])


@router.delete("/admin/reminder/devices/{device_id}", dependencies=[Depends(require_admin), Depends(enabled)])
async def revoke_device(device_id: str) -> dict[str, bool]:
    if not await asyncio.to_thread(get_repository().revoke_device, device_id, account_id()):
        raise HTTPException(404, "设备不存在。")
    return {"revoked": True}


@router.post("/admin/reminder/test", dependencies=[Depends(require_admin), Depends(enabled)])
async def test_notification(payload: TestRequest) -> dict[str, str]:
    value = await asyncio.to_thread(get_repository().create_test, payload.device_id, account_id())
    if value is None:
        raise HTTPException(404, "未找到当前账户的有效设备。")
    return {"notification_id": value}


@router.get("/reminder/notifications")
async def notifications(device: dict[str, str] = Depends(device_identity)) -> dict[str, Any]:
    try:
        items = await snapshot(device)
    except ChatwootAPIError as exc:
        raise HTTPException(503, "Chatwoot 状态暂时无法核对，客户端将保留提醒并重试。") from exc
    return {"notifications": items, "agent_name": device["agent_name"],
            "agent_id": device["agent_id"], "binding_version": device.get("binding_version", 1),
            "switchable": device.get("auth_kind") == "plugin",
            "poll_seconds": get_settings().reminder_poll_seconds,
            "repeat_seconds": get_settings().reminder_repeat_seconds}


@router.post("/reminder/notifications/{notification_id}/ack")
async def acknowledge(
    notification_id: str, payload: AckRequest,
    device: dict[str, str] = Depends(device_identity),
) -> dict[str, bool]:
    if device.get("auth_kind") == "plugin" and payload.binding_version != device["binding_version"]:
        raise HTTPException(409, "当前席位已切换，旧席位的确认不能继续提交。")
    try:
        await snapshot(device, force=True)
    except ChatwootAPIError as exc:
        raise HTTPException(503, "暂时无法核对当前分配，请稍后确认。") from exc
    accepted = await asyncio.to_thread(get_repository().acknowledge, device, notification_id, payload.version)
    if not accepted:
        raise HTTPException(409, "提醒已结束、改派或版本已变化，请刷新列表。")
    return {"acknowledged": True}
