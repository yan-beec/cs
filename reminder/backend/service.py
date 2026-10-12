"""把现有接管结果转换为提醒；试验版客户端通过短轮询接收。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ics_agent.app.chatwoot.client import ChatwootAPIError, get_chatwoot_client
from ics_agent.app.core.config import get_settings
from reminder.protocol import conversation_url, handoff_key, workbench_url
from .repository import current_handoff, get_repository

_locks: dict[str, asyncio.Lock] = {}
_last_sync: dict[str, float] = {}
_inbox_cache: dict[tuple[str, str], tuple[float, str]] = {}
_inbox_locks: dict[tuple[str, str], asyncio.Lock] = {}
_INBOX_SYNC_BUDGET_SECONDS = 2.0
_logger = logging.getLogger(__name__)


def capture_hook(
    expected: Any, name: str, message: str, *, inbox_id: str = "", inbox_name: str = "",
):
    expected_key = handoff_key(expected.handoff_trigger_message_id, expected.handoff_at)

    def on_mark(cursor: Any, latest: Any) -> bool:
        if (not current_handoff(latest)
                or handoff_key(latest.handoff_trigger_message_id, latest.handoff_at) != expected_key):
            return False
        get_repository().capture(cursor, latest, name, message,
                                 inbox_id=inbox_id, inbox_name=inbox_name)
        return True

    return on_mark


def ensure_notification(
    control: Any, name: str = "", message: str = "", *, inbox_id: str = "", inbox_name: str = "",
) -> None:
    if current_handoff(control) and control.handoff_conversation_updated:
        get_repository().ensure_handoff(control, name, message,
                                       inbox_id=inbox_id, inbox_name=inbox_name)


def inbox_details(conversation: dict[str, Any]) -> tuple[str, str]:
    """兼容会话/Webhook 只带 inbox_id 或嵌套 inbox 的响应。"""
    inbox = conversation.get("inbox")
    meta = conversation.get("meta")
    if not isinstance(inbox, dict) and isinstance(meta, dict):
        inbox = meta.get("inbox")
    inbox = inbox if isinstance(inbox, dict) else {}
    inbox_id = conversation.get("inbox_id")
    if not isinstance(inbox_id, (str, int)) or not str(inbox_id).strip():
        inbox_id = inbox.get("id")
    name = inbox.get("name") or conversation.get("inbox_name")
    return (
        str(inbox_id).strip()[:100] if isinstance(inbox_id, (str, int)) else "",
        name.strip()[:200] if isinstance(name, str) else "",
    )


async def _resolve_inbox(
    account_id: str, conversation: dict[str, Any], item: dict[str, Any],
    semaphore: asyncio.Semaphore | None,
    deadline: float,
) -> tuple[str, str]:
    inbox_id, name = inbox_details(conversation)
    inbox_id = inbox_id or item.get("inbox_id", "")
    if not inbox_id:
        return "", name or item.get("inbox_name", "")
    key = (account_id, inbox_id)
    if name:
        _inbox_cache[key] = (time.monotonic() + 300, name)
        return inbox_id, name
    previous = item.get("inbox_name", "") if item.get("inbox_id") == inbox_id else ""
    async with _inbox_locks.setdefault(key, asyncio.Lock()):
        expires, cached = _inbox_cache.get(key, (0, ""))
        if expires > time.monotonic():
            return inbox_id, cached or previous
        fetch_started = False
        try:
            async def fetch() -> dict[str, Any]:
                def read_inbox() -> dict[str, Any]:
                    nonlocal fetch_started
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError("Inbox synchronization budget exhausted.")
                    fetch_started = True
                    return get_chatwoot_client().get_inbox(
                        account_id=account_id, inbox_id=inbox_id, timeout_seconds=remaining)

                return await asyncio.to_thread(read_inbox)

            if semaphore is None:
                inbox = await fetch()
            else:
                async with semaphore:
                    inbox = await fetch()
            if not isinstance(inbox, dict) or not isinstance(inbox.get("name"), str):
                raise ChatwootAPIError("Chatwoot inbox name is missing.")
            name = inbox["name"].strip()[:200]
            if not name:
                raise ChatwootAPIError("Chatwoot inbox name is empty.")
        except asyncio.CancelledError:
            # 只缓存已经实际发起的查询失败；排队项下轮仍有机会查询。
            if fetch_started:
                _inbox_cache[key] = (time.monotonic() + 60, "")
            raise
        except Exception:
            # 名称暂时不可读仍保留原提醒，并短暂缓存失败，避免频繁重试。
            if fetch_started:
                _inbox_cache[key] = (time.monotonic() + 60, "")
            _logger.warning("Failed to read inbox %s for account %s", inbox_id, account_id,
                            exc_info=True)
            return inbox_id, previous
        _inbox_cache[key] = (time.monotonic() + 300, name)
        return inbox_id, name


async def _enrich_inbox_safely(
    repository: Any, account_id: str, item: dict[str, Any], conversation: dict[str, Any],
    semaphore: asyncio.Semaphore, deadline: float,
) -> None:
    """名称同步的整批共享预算包含锁排队、信号量排队和网络等待。"""
    inbox_id, name = inbox_details(conversation)
    inbox_id = inbox_id or item.get("inbox_id", "")
    previous = item.get("inbox_name", "") if item.get("inbox_id", "") == inbox_id else ""
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("Inbox synchronization budget exhausted.")
        inbox_id, name = await asyncio.wait_for(
            _resolve_inbox(account_id, conversation, item, semaphore, deadline),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        name = name or previous
    except Exception:
        _logger.warning("Failed to resolve inbox metadata for reminder %s", item.get("id"),
                        exc_info=True)
        name = name or previous
    if inbox_id or name:
        try:
            await asyncio.to_thread(repository.update_inbox, item, inbox_id, name)
        except Exception:
            _logger.warning("Failed to update inbox metadata for reminder %s", item.get("id"),
                            exc_info=True)


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
    synchronized: list[tuple[dict[str, Any], dict[str, Any]]] = []
    did_sync = False
    async with _locks.setdefault(account_id, asyncio.Lock()):
        items = await asyncio.to_thread(repository.active, account_id)
        if force or time.monotonic() - _last_sync.get(account_id, 0) >= 10:
            # 只核对本功能记录的会话；不会重新分配坐席或扫描全部 Chatwoot 会话。
            semaphore = asyncio.Semaphore(3)

            async def sync_one_safely(item: dict[str, Any]) -> None:
                try:
                    conversation = await _synchronize_item(repository, account_id, item, semaphore)
                    if conversation is not None and str(conversation.get("status") or "").lower() != "resolved":
                        synchronized.append((item, conversation))
                except Exception:
                    # 一个会话的 Chatwoot 异常不能中断同一账户其他提醒的同步。
                    # 保留该条提醒当前状态，下一轮再重试，避免把临时故障误判为已结束。
                    _logger.warning(
                        "Failed to synchronize reminder %s for account %s",
                        item.get("id"), account_id, exc_info=True,
                    )

            await asyncio.gather(*(sync_one_safely(item) for item in items))
            _last_sync[account_id] = time.monotonic()
            did_sync = True
    # 展示名称不持有账户锁；客服确认只需核对会话状态，毋须等待名称查询。
    if synchronized:
        deadline = time.monotonic() + _INBOX_SYNC_BUDGET_SECONDS
        semaphore = asyncio.Semaphore(3)
        await asyncio.gather(*(
            _enrich_inbox_safely(repository, account_id, item, conversation, semaphore, deadline)
            for item, conversation in synchronized
        ))
    if did_sync:
        items = await asyncio.to_thread(repository.active, account_id)
    public_base = get_settings().chatwoot_public_base_url
    result = []
    for item in items:
        if item["agent_id"] != device["agent_id"] or item["state"] != "pending":
            continue
        item["conversation_url"] = (
            workbench_url(public_base, account_id) if item["is_test"]
            else conversation_url(public_base, account_id, item["conversation_id"])
        )
        result.append(item)
    return result


async def _synchronize_item(
    repository: Any,
    account_id: str,
    item: dict[str, Any],
    semaphore: asyncio.Semaphore | None = None,
) -> dict[str, Any] | None:
    if item["is_test"]:
        return
    if semaphore is None:
        conversation = await asyncio.to_thread(
            get_chatwoot_client().get_conversation,
            account_id=account_id, conversation_id=item["conversation_id"])
        agent_id, status = assignment(conversation)
    else:
        async with semaphore:
            conversation = await asyncio.to_thread(
                get_chatwoot_client().get_conversation,
                account_id=account_id, conversation_id=item["conversation_id"])
            agent_id, status = assignment(conversation)
    await asyncio.to_thread(repository.synchronize, item, agent_id, status)
    return conversation


async def synchronize_notification(
    device: dict[str, str], notification_id: str, version: int,
) -> bool:
    """核对单条提醒，供确认接口使用。

    只读取目标提醒并查询对应的 Chatwoot 会话，不触发账户级 active/snapshot 扫描。
    返回 False 表示提醒已不存在、已改派或版本已经变化。
    """
    account_id = device["account_id"]
    repository = get_repository()
    async with _locks.setdefault(account_id, asyncio.Lock()):
        item = await asyncio.to_thread(
            repository.active_notification, account_id, notification_id)
        if not item or item["agent_id"] != device["agent_id"]:
            return False
        if item["version"] != version or item["state"] not in {"pending", "acknowledged"}:
            return False
        await _synchronize_item(repository, account_id, item)
        return True
