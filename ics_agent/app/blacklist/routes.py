"""坐席客户端黑名单接口。

路径放在 /reminder/ 下，沿用现有 Nginx 对客户端开放的前缀；认证使用坐席在客户端
中的 Chatwoot 登录会话（通过请求头转发），不需要新的密码或令牌。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from ics_agent.app.core.config import get_settings
from .agent_session import AgentForbidden, AgentSession, AgentSessionInvalid, ChatwootUnavailable
from .options import ACTION_LABELS, CLIENT_TOO_OLD, DURATIONS, LEGACY_REASONS, SOURCE_LABELS, options_payload
from .repository import BlacklistConflict
from .runtime import get_blacklist_runtime
from .service import BlacklistInputError, BlacklistService, BlacklistUpstreamError

router = APIRouter(prefix="/reminder/blacklist", tags=["blacklist"])

Duration = Literal["2h", "24h", "72h", "permanent"]
PositiveID = Annotated[int, Field(ge=1, le=2**63 - 1)]


def service() -> BlacklistService:
    if not get_settings().chatwoot_blacklist_enabled:
        raise HTTPException(404, "黑名单功能未启用。")
    runtime = get_blacklist_runtime()
    if not runtime.ready or runtime.service is None:
        raise HTTPException(503, "黑名单服务正在启动或暂不可用，请稍后重试。", headers={"Retry-After": "10"})
    return runtime.service


def agent_session(
    access_token: str | None = Header(None, alias="X-Chatwoot-Access-Token", max_length=512),
    client: str | None = Header(None, alias="X-Chatwoot-Client", max_length=512),
    uid: str | None = Header(None, alias="X-Chatwoot-Uid", max_length=512),
) -> AgentSession:
    if not access_token or not client or not uid:
        raise HTTPException(401, "请先在客户端工作台登录 Chatwoot。")
    return AgentSession(access_token=access_token, client=client, uid=uid)


def _describe(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    if result.get("event_id") is not None:
        result["event_id"] = str(result["event_id"])
    if "action" in result:
        result["action_label"] = ACTION_LABELS.get(result["action"], result["action"])
    if "source" in result and result["source"]:
        result["source_label"] = SOURCE_LABELS.get(result["source"], result["source"])
    if result.get("reason_code"):
        # 仅旧记录有预设原因代码。
        result["reason_label"] = LEGACY_REASONS.get(result["reason_code"], result["reason_code"])
    if result.get("duration_code"):
        result["duration_label"] = DURATIONS[result["duration_code"]]["label"]
    return result


async def _guard(operation):
    try:
        return await operation
    except AgentSessionInvalid as error:
        raise HTTPException(401, str(error)) from None
    except AgentForbidden as error:
        raise HTTPException(403, str(error)) from None
    except BlacklistInputError as error:
        raise HTTPException(422, str(error)) from None
    except BlacklistConflict as error:
        raise HTTPException(409, str(error)) from None
    except (BlacklistUpstreamError, ChatwootUnavailable) as error:
        detail = str(error) if isinstance(error, BlacklistUpstreamError) else "暂时无法连接 Chatwoot，请稍后重试。"
        raise HTTPException(502, detail) from None
    except (DatabaseError, PoolTimeout):
        raise HTTPException(503, "黑名单数据库暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None


class BlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: PositiveID
    conversation_id: PositiveID
    contact_id: PositiveID
    duration: Duration
    # 坐席人工填写的拉黑原因（必填，去空格后 1–500 字，服务层校验）。
    reason: str | None = Field(default=None, max_length=1000)
    # 旧版客户端字段：原因为预设代码、说明放在 remark。出现即判定为旧客户端并拒绝。
    remark: str | None = Field(default=None, max_length=1000)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: PositiveID
    contact_id: PositiveID
    version: int = Field(ge=0, le=2**63 - 1)
    duration: Duration
    # 修改原因（必填，服务层校验）。
    remark: str | None = Field(default=None, max_length=1000)


class UnblockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: PositiveID
    contact_id: PositiveID
    version: int = Field(ge=0, le=2**63 - 1)
    remark: str = Field(max_length=1000)


@router.get("/options")
async def blacklist_options() -> dict[str, Any]:
    return options_payload()


@router.get("")
async def list_blacklist(
    account_id: int = Query(..., ge=1, le=2**63 - 1),
    session: AgentSession = Depends(agent_session), blacklist: BlacklistService = Depends(service),
) -> dict[str, Any]:
    async def run():
        await blacklist.operator(session, str(account_id))
        return [_describe(row) for row in await blacklist.list_active(str(account_id))]
    return {"items": await _guard(run())}


@router.get("/preview")
async def preview(
    account_id: int = Query(..., ge=1, le=2**63 - 1), conversation_id: int = Query(..., ge=1, le=2**63 - 1),
    session: AgentSession = Depends(agent_session), blacklist: BlacklistService = Depends(service),
) -> dict[str, Any]:
    async def run():
        await blacklist.operator(session, str(account_id))
        result = await blacklist.preview(session, str(account_id), str(conversation_id))
        return {**result, "history": [_describe(row) for row in result["history"]]}
    return await _guard(run())


@router.get("/contacts/{contact_id}")
async def contact_history(
    contact_id: int, account_id: int = Query(..., ge=1, le=2**63 - 1),
    session: AgentSession = Depends(agent_session), blacklist: BlacklistService = Depends(service),
) -> dict[str, Any]:
    async def run():
        await blacklist.operator(session, str(account_id))
        result = await blacklist.contact(str(account_id), str(contact_id))
        return {**result, "history": [_describe(row) for row in result["history"]]}
    return await _guard(run())


@router.post("/block")
async def block(body: BlockRequest, session: AgentSession = Depends(agent_session),
                blacklist: BlacklistService = Depends(service)) -> dict[str, Any]:
    if "remark" in body.model_fields_set:
        raise HTTPException(422, CLIENT_TOO_OLD)
    entry = await _guard(blacklist.block(
        session, account_id=str(body.account_id), conversation_id=str(body.conversation_id),
        contact_id=str(body.contact_id), duration=body.duration, reason=body.reason))
    return {"ok": True, "entry": _describe(entry)}


@router.post("/change")
async def change(body: ChangeRequest, session: AgentSession = Depends(agent_session),
                 blacklist: BlacklistService = Depends(service)) -> dict[str, Any]:
    entry = await _guard(blacklist.change(
        session, account_id=str(body.account_id), contact_id=str(body.contact_id), version=body.version,
        duration=body.duration, remark=body.remark))
    return {"ok": True, "entry": _describe(entry)}


@router.post("/unblock")
async def unblock(body: UnblockRequest, session: AgentSession = Depends(agent_session),
                  blacklist: BlacklistService = Depends(service)) -> dict[str, Any]:
    entry = await _guard(blacklist.unblock(
        session, account_id=str(body.account_id), contact_id=str(body.contact_id), version=body.version,
        remark=body.remark))
    return {"ok": True, "entry": _describe(entry)}
