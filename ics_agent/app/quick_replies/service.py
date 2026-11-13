"""快捷回复业务编排：核对坐席身份 → 校验内容 → 读写数据库。"""

from __future__ import annotations

import asyncio
import re

from ics_agent.app.blacklist.agent_session import AgentForbidden, AgentSession, ChatwootAgentGateway
from .repository import QuickRepliesRepository

# 与坐席客户端 src/quick-replies.cjs 的 LIMITS 保持一致。
MAX_ITEMS = 200
MAX_TITLE = 80
MAX_CONTENT = 12000
# 标题和内容合计字符数上限，保证整份列表的请求体在 Nginx 默认 1 MB 限制以内。
MAX_TOTAL = 300000
ITEM_ID = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


class QuickRepliesInputError(ValueError):
    pass


def _positive_id(value, label: str) -> str:
    text = str(value or "").strip()
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) <= 2**63 - 1:
        raise QuickRepliesInputError(f"{label}无效。")
    return str(int(text))


def _clean_text(value, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise QuickRepliesInputError(f"{label}格式无效。")
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise QuickRepliesInputError(f"{label}包含无效字符。")
    if not value.strip() or len(value) > limit:
        raise QuickRepliesInputError(f"请输入不超过 {limit} 字的{label}。")
    return value


def clean_items(items) -> list[dict]:
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise QuickRepliesInputError(f"最多保存 {MAX_ITEMS} 条快捷回复。")
    seen: set[str] = set()
    result, total = [], 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ITEM_ID.match(item["id"]) \
                or item["id"] in seen:
            raise QuickRepliesInputError("快捷回复编号无效或重复。")
        seen.add(item["id"])
        title = _clean_text(item.get("title"), "标题", MAX_TITLE).strip()
        content = _clean_text(item.get("content"), "回复内容", MAX_CONTENT).replace("\r\n", "\n").replace("\r", "\n")
        total += len(title) + len(content)
        result.append({"id": item["id"], "title": title, "content": content})
    if total > MAX_TOTAL:
        raise QuickRepliesInputError(f"快捷回复总字数不能超过 {MAX_TOTAL} 字，请删减后再保存。")
    return result


class QuickRepliesService:
    def __init__(self, settings, repository: QuickRepliesRepository, gateway: ChatwootAgentGateway | None = None):
        self.settings = settings
        self.repository = repository
        self.gateway = gateway or ChatwootAgentGateway(settings)

    async def agent(self, session: AgentSession, account_id) -> tuple[str, str, str]:
        """以 Chatwoot 为准确认坐席身份和账号归属，返回 (account_id, agent_id, 坐席名)。"""
        account_id = _positive_id(account_id, "工作区")
        profile = await asyncio.to_thread(self.gateway.profile, session)
        if account_id not in profile["accounts"]:
            raise AgentForbidden("当前坐席不属于该工作区。")
        return account_id, profile["id"], profile["name"]

    async def read(self, session: AgentSession, account_id) -> dict:
        account_id, agent_id, name = await self.agent(session, account_id)
        data = await asyncio.to_thread(self.repository.read, account_id, agent_id)
        try:
            items = clean_items(data["items"])
        except QuickRepliesInputError:
            # 库中数据被人为改坏时不返回半份列表，避免客户端用残缺数据覆盖本地缓存。
            raise RuntimeError("stored quick replies are invalid") from None
        return {**data, "items": items, "agent_id": agent_id, "agent_name": name}

    async def save(self, session: AgentSession, account_id, *, revision: str, items) -> dict:
        account_id, agent_id, name = await self.agent(session, account_id)
        clean = clean_items(items)
        data = await asyncio.to_thread(self.repository.save, account_id, agent_id,
                                       expected_revision=revision, items=clean, operator_name=name)
        return {**data, "agent_id": agent_id, "agent_name": name}
