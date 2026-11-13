"""坐席客户端快捷回复接口。

路径放在 /reminder/ 下，沿用现有 Nginx 对客户端开放的前缀（只用 GET/POST）；
认证使用坐席在客户端中的 Chatwoot 登录会话（通过请求头转发），与黑名单接口一致。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from ics_agent.app.blacklist.agent_session import AgentForbidden, AgentSession, AgentSessionInvalid, ChatwootUnavailable
from ics_agent.app.core.config import get_settings
from .repository import QuickRepliesConflict
from .runtime import get_quick_replies_runtime
from .service import MAX_ITEMS, QuickRepliesInputError, QuickRepliesService

router = APIRouter(prefix="/reminder/quick-replies", tags=["quick-replies"])

PositiveID = Annotated[int, Field(ge=1, le=2**63 - 1)]


def service() -> QuickRepliesService:
    if not get_settings().chatwoot_quick_replies_enabled:
        raise HTTPException(404, "服务器未启用快捷回复同步。")
    runtime = get_quick_replies_runtime()
    if not runtime.ready or runtime.service is None:
        raise HTTPException(503, "快捷回复服务正在启动或暂不可用，请稍后重试。", headers={"Retry-After": "10"})
    return runtime.service


def agent_session(
    access_token: str | None = Header(None, alias="X-Chatwoot-Access-Token", max_length=512),
    client: str | None = Header(None, alias="X-Chatwoot-Client", max_length=512),
    uid: str | None = Header(None, alias="X-Chatwoot-Uid", max_length=512),
) -> AgentSession:
    if not access_token or not client or not uid:
        raise HTTPException(401, "请先在客户端工作台登录 Chatwoot。")
    return AgentSession(access_token=access_token, client=client, uid=uid)


def _describe(data: dict) -> dict[str, Any]:
    updated_at = data.get("updated_at")
    return {"revision": data["revision"], "items": data["items"],
            "updated_at": updated_at.isoformat() if updated_at is not None else None,
            "agent_id": data.get("agent_id"), "agent_name": data.get("agent_name")}


async def _guard(operation):
    try:
        return await operation
    except AgentSessionInvalid as error:
        raise HTTPException(401, str(error)) from None
    except AgentForbidden as error:
        raise HTTPException(403, str(error)) from None
    except QuickRepliesInputError as error:
        raise HTTPException(422, str(error)) from None
    except QuickRepliesConflict as error:
        raise HTTPException(409, str(error)) from None
    except ChatwootUnavailable:
        raise HTTPException(502, "暂时无法连接 Chatwoot，请稍后重试。") from None
    except (DatabaseError, PoolTimeout):
        raise HTTPException(503, "快捷回复数据库暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None
    except RuntimeError:
        raise HTTPException(500, "服务器上的快捷回复数据无效，请联系管理员。") from None


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: PositiveID
    # 空字符串表示该坐席首次保存。
    revision: str = Field(default="", max_length=64, pattern=r"^(?:[0-9a-f-]{36})?$")
    items: list[dict[str, Any]] = Field(max_length=MAX_ITEMS)


@router.get("")
async def read_quick_replies(
    account_id: int = Query(..., ge=1, le=2**63 - 1),
    session: AgentSession = Depends(agent_session), quick_replies: QuickRepliesService = Depends(service),
) -> dict[str, Any]:
    return _describe(await _guard(quick_replies.read(session, str(account_id))))


@router.post("")
async def save_quick_replies(body: SaveRequest, session: AgentSession = Depends(agent_session),
                             quick_replies: QuickRepliesService = Depends(service)) -> dict[str, Any]:
    data = await _guard(quick_replies.save(session, str(body.account_id), revision=body.revision, items=body.items))
    return {"ok": True, **_describe(data)}
