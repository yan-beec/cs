"""黑名单业务编排：身份核对 → 写入到期时间与历史 → 结束进行中的人工接待。

黑名单与 Chatwoot 静音（contact.blocked）完全独立：拉黑、改时长、解除都不改动
Chatwoot 静音，网页端静音/取消静音也不会写入黑名单。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ics_agent.app.chatwoot.client import ChatwootAPIError, get_chatwoot_client
from .agent_session import (
    AgentForbidden, AgentSession, ChatwootAgentGateway, ChatwootUnavailable, conversation_contact,
)
from .options import REMARK_MAX_LENGTH, blocked_until_for
from .repository import BlacklistRepository, Operator

logger = logging.getLogger(__name__)


class BlacklistInputError(ValueError):
    pass


class BlacklistUpstreamError(RuntimeError):
    pass


def clean_remark(value: str | None, *, required: bool, label: str = "备注") -> str:
    text = (value or "").replace("\r\n", "\n").strip()
    if "\x00" in text or any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise BlacklistInputError(f"{label}包含无效字符。")
    if len(text) > REMARK_MAX_LENGTH:
        raise BlacklistInputError(f"{label}不能超过 {REMARK_MAX_LENGTH} 字。")
    if required and not text:
        raise BlacklistInputError(f"请填写{label}。")
    return text


def _positive_id(value, label: str) -> str:
    text = str(value or "").strip()
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) <= 2**63 - 1:
        raise BlacklistInputError(f"{label}无效。")
    return str(int(text))


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()[:200]
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    kinds = {str(item.get("file_type") or "") for item in attachments if isinstance(item, dict)}
    if "image" in kinds:
        return "【图片】"
    if "video" in kinds:
        return "【视频】"
    return "【附件】" if attachments else "（无文字内容）"


class BlacklistService:
    def __init__(self, settings, repository: BlacklistRepository, gateway: ChatwootAgentGateway | None = None,
                 chatwoot_client=None,
                 on_blocked: Callable[..., Awaitable[None]] | None = None):
        self.settings = settings
        self.repository = repository
        self.gateway = gateway or ChatwootAgentGateway(settings)
        self._client = chatwoot_client
        # 拉黑成功后结束该客户进行中的人工接待（由运行时注入，测试中可为空）。
        self.on_blocked = on_blocked

    @property
    def client(self):
        return self._client or get_chatwoot_client()

    async def operator(self, session: AgentSession, account_id: str) -> Operator:
        account_id = _positive_id(account_id, "账号")
        configured = str(self.settings.chatwoot_account_id or "").strip()
        if configured and configured != account_id:
            raise AgentForbidden("黑名单只支持 ICS 当前接入的 Chatwoot 账号。")
        profile = await asyncio.to_thread(self.gateway.profile, session)
        if account_id not in profile["accounts"]:
            raise AgentForbidden("当前坐席不属于该账号。")
        return Operator(agent_id=profile["id"], name=profile["name"])

    async def list_active(self, account_id: str) -> list[dict]:
        return await asyncio.to_thread(self.repository.list_active, account_id)

    async def contact(self, account_id: str, contact_id: str) -> dict:
        return await asyncio.to_thread(self.repository.contact, account_id, _positive_id(contact_id, "客户编号"))

    async def preview(self, session: AgentSession, account_id: str, conversation_id: str) -> dict:
        """拉黑弹窗的核对信息：以坐席自己的权限读取会话客户、最近客户消息和当前状态。"""
        conversation_id = _positive_id(conversation_id, "会话编号")
        conversation = await asyncio.to_thread(self.gateway.conversation, session, account_id, conversation_id)
        contact = conversation_contact(conversation)
        if not contact:
            raise BlacklistInputError("该会话没有可拉黑的客户。")
        messages = []
        try:
            raw = await asyncio.to_thread(self.gateway.messages, session, account_id, conversation_id)
            incoming = [item for item in raw if isinstance(item, dict) and item.get("message_type") in (0, "incoming")
                        and not item.get("private")]
            messages = [{"content": _message_text(item), "created_at": item.get("created_at")} for item in incoming[-3:]]
        except (AgentForbidden, ChatwootUnavailable):
            messages = []
        state = await asyncio.to_thread(self.repository.contact, account_id, contact["id"], 10)
        return {"conversation_id": conversation_id, "contact": contact, "recent_messages": messages, **state}

    async def block(self, session: AgentSession, *, account_id: str, conversation_id: str, contact_id: str,
                    duration: str, reason: str | None) -> dict:
        # 原因必须由坐席人工填写，先校验再核对身份，避免无效请求访问 Chatwoot。
        reason = clean_remark(reason, required=True, label="拉黑原因")
        operator = await self.operator(session, account_id)
        conversation_id = _positive_id(conversation_id, "会话编号")
        contact_id = _positive_id(contact_id, "客户编号")
        conversation = await asyncio.to_thread(self.gateway.conversation, session, account_id, conversation_id)
        contact = conversation_contact(conversation)
        if contact.get("id") != contact_id:
            raise BlacklistInputError("会话与客户不匹配，可能已切换会话，请关闭弹窗后重试。")
        now = datetime.now(timezone.utc)
        blocked_until = blocked_until_for(duration, now)
        entry = await asyncio.to_thread(
            self.repository.block, account_id=account_id, contact_id=contact_id,
            conversation_id=conversation_id, contact_name=contact.get("name"),
            contact_identifier=contact.get("identifier"), duration_code=duration, blocked_until=blocked_until,
            remark=reason, operator=operator)
        if self.on_blocked is not None:
            try:
                await self.on_blocked(account_id=account_id, contact_id=contact_id, blocked_at=now)
            except Exception as error:
                # 拉黑已生效；人工接待结束失败时，该会话的下一条客户消息会释放人工锁。
                logger.warning("事件=黑名单.结束人工接待失败 账号=%s 客户=%s 错误类型=%s",
                               account_id, contact_id, type(error).__name__)
        logger.info("事件=黑名单.拉黑 账号=%s 客户=%s 时长=%s 操作人=%s", account_id, contact_id, duration, operator.agent_id)
        return entry

    async def change(self, session: AgentSession, *, account_id: str, contact_id: str, version: int,
                     duration: str, remark: str | None) -> dict:
        remark = clean_remark(remark, required=True, label="修改原因")
        operator = await self.operator(session, account_id)
        contact_id = _positive_id(contact_id, "客户编号")
        blocked_until = blocked_until_for(duration, datetime.now(timezone.utc))
        entry = await asyncio.to_thread(
            self.repository.change, account_id=account_id, contact_id=contact_id, version=version,
            duration_code=duration, blocked_until=blocked_until, remark=remark, operator=operator)
        logger.info("事件=黑名单.修改时长 账号=%s 客户=%s 时长=%s 操作人=%s", account_id, contact_id, duration, operator.agent_id)
        return entry

    async def unblock(self, session: AgentSession, *, account_id: str, contact_id: str, version: int,
                      remark: str | None) -> dict:
        remark = clean_remark(remark, required=True, label="解除备注")
        operator = await self.operator(session, account_id)
        contact_id = _positive_id(contact_id, "客户编号")
        previous = await asyncio.to_thread(self.repository.check_unblock, account_id=account_id,
                                           contact_id=contact_id, version=version)
        if (previous.get("block") or {}).get("chatwoot_muted"):
            # 只有升级前的旧记录同时静音过 Chatwoot，解除时顺带取消一次。
            try:
                await asyncio.to_thread(self.client.set_contact_blocked, account_id=account_id,
                                        contact_id=contact_id, blocked=False)
            except ChatwootAPIError as error:
                logger.warning("事件=黑名单.解除失败 账号=%s 客户=%s 状态码=%s", account_id, contact_id, error.status_code)
                raise BlacklistUpstreamError("Chatwoot 取消静音未成功，请稍后重试。") from None
        entry = await asyncio.to_thread(self.repository.complete_unblock, account_id=account_id,
                                        contact_id=contact_id, remark=remark, operator=operator, previous=previous)
        logger.info("事件=黑名单.解除 账号=%s 客户=%s 操作人=%s", account_id, contact_id, operator.agent_id)
        return entry

    async def reconcile_once(self) -> int:
        """补记到期自动解除（及升级前网页端静音留下的旧记录）；只读写黑名单自己的记录。"""
        recorded = 0
        for candidate in await asyncio.to_thread(self.repository.reconcile_candidates):
            name = identifier = None
            if candidate["active"]:
                try:
                    contact = await asyncio.to_thread(self.client.get_contact, account_id=candidate["account_id"],
                                                      contact_id=candidate["contact_id"])
                    payload = contact.get("payload") if isinstance(contact.get("payload"), dict) else contact
                    name = str(payload.get("name") or "")[:200] or None
                    identifier = str(payload.get("identifier") or "")[:200] or None
                except ChatwootAPIError:
                    pass
            if await asyncio.to_thread(self.repository.record_reconciled, candidate,
                                       contact_name=name, contact_identifier=identifier):
                recorded += 1
        return recorded
