"""把现有接管结果转换为提醒；试验版客户端通过短轮询接收。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ics_agent.app.chatwoot.client import ChatwootAPIError, get_chatwoot_client
from ics_agent.app.core.config import get_settings
from reminder.protocol import conversation_url, handoff_key
from .repository import current_handoff, get_repository

_locks: dict[str, asyncio.Lock] = {}
_last_sync: dict[str, float] = {}


def capture_hook(expected: Any, name: str, message: str):
    expected_key = handoff_key(expected.handoff_trigger_message_id, expected.handoff_at)

    def on_mark(cursor: Any, latest: Any) -> bool:
        if (not current_handoff(latest)
                or handoff_key(latest.handoff_trigger_message_id, latest.handoff_at) != expected_key):
            return False
        get_repository().capture(cursor, latest, name, message)
        return True

    return on_mark


def ensure_notification(control: Any, name: str = "", message: str = "") -> None:
    if current_handoff(control) and control.handoff_conversation_updated:
        get_repository().ensure_handoff(control, name, message)


def assignment(conversation: dict[str, Any]) -> tuple[str, str]:
    status = str(conversation.get("status") or "").lower()
    if status == "resolved":
        return "", status
    meta = conversation.get("meta")
    source = meta if isinstance(meta, dict) and "assignee" in meta else conversation
    if "assignee" in source:
        assignee = source["assignee"]
        agent_id = assignee.get("id") if isinstance(assignee, dict) else None
        if assignee is not None and agent_id is None:
            raise ChatwootAPIError("Conversation assignee is invalid.")
    elif "assignee_id" in source:
        agent_id = source["assignee_id"]
    else:
        raise ChatwootAPIError("Conversation assignee is missing.")
    if not status:
        raise ChatwootAPIError("Conversation status is missing.")
    return str(agent_id) if agent_id is not None else "", status


async def snapshot(device: dict[str, str], *, force: bool = False) -> list[dict[str, Any]]:
    account_id = device["account_id"]
    repository = get_repository()
    async with _locks.setdefault(account_id, asyncio.Lock()):
        items = await asyncio.to_thread(repository.active, account_id)
        if force or time.monotonic() - _last_sync.get(account_id, 0) >= 10:
            # 只核对本功能记录的会话；不会重新分配坐席或扫描全部 Chatwoot 会话。
            semaphore = asyncio.Semaphore(3)

            async def sync_one(item: dict[str, Any]) -> None:
                if item["is_test"]:
                    return
                async with semaphore:
                    conversation = await asyncio.to_thread(
                        get_chatwoot_client().get_conversation,
                        account_id=account_id, conversation_id=item["conversation_id"])
                    agent_id, status = assignment(conversation)
                    await asyncio.to_thread(repository.synchronize, item, agent_id, status)

            await asyncio.gather(*(sync_one(item) for item in items))
            _last_sync[account_id] = time.monotonic()
            items = await asyncio.to_thread(repository.active, account_id)
    public_base = get_settings().chatwoot_public_base_url
    result = []
    for item in items:
        if item["agent_id"] != device["agent_id"] or item["state"] != "pending":
            continue
        item["conversation_url"] = "" if item["is_test"] else conversation_url(
            public_base, account_id, item["conversation_id"])
        result.append(item)
    return result
