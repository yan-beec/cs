"""Chatwoot Webhook 接收、队列处理、自动回复和人工接管编排。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ics_agent.app.core.context import (
    AI_CHATWOOT_STATUS,
    CHATWOOT_TYPING_HEARTBEAT_SECONDS,
    CONTACT_BLOCK_EVENTS,
    CONVERSATION_STATUS_EVENTS,
    EMOJI_ACK_MESSAGE,
    HANDOFF_NO_AGENT_MESSAGE,
    HANDOFF_NOTICE_MESSAGE,
    HANDOFF_RELEASE_STATUSES,
    MUTED_HANDOFF_MESSAGE,
    contact_block_repository,
    conversation_control_repository,
    rag_settings,
    settings,
    webhook_repository,
)
from .client import ChatwootAPIError, get_chatwoot_client
from .state import ConversationControl
from ics_agent.app.core.logging_config import log_event
from ics_agent.app.core.models import ChatwootWebhookEvent
from .payload import (
    contact_block_change_from_payload,
    contact_blocked_from_payload,
    is_emoji_only_text,
    is_explicit_handoff_request,
    parse_event,
    verify_signature,
)
from ics_agent.app.services.rag import _run_rag_query
from .jobs import (
    WebhookJob,
    WebhookJobQueue,
    WebhookQueueFull,
    payload_hash,
)

logger = logging.getLogger(__name__)
router = APIRouter()
webhook_queue: WebhookJobQueue | None = None
contact_block_reaper_task: asyncio.Task[None] | None = None
_conversation_locks: dict[str, asyncio.Lock] = {}


async def start_chatwoot_webhook_workers() -> None:
    """启动 Chatwoot Webhook 后台队列和联系人静音到期任务。"""
    global webhook_queue
    global contact_block_reaper_task
    webhook_queue = WebhookJobQueue(
        processor=_process_webhook_job,
        repository=webhook_repository,
    )
    await webhook_queue.start()
    contact_block_reaper_task = asyncio.create_task(
        _contact_block_reaper_loop(),
        name="chatwoot-contact-block-reaper",
    )


async def stop_chatwoot_webhook_workers() -> None:
    """停止 Chatwoot Webhook 后台任务。"""
    global webhook_queue
    global contact_block_reaper_task
    if contact_block_reaper_task is not None:
        contact_block_reaper_task.cancel()
        await asyncio.gather(
            contact_block_reaper_task,
            return_exceptions=True,
        )
        contact_block_reaper_task = None
    if webhook_queue is not None:
        await webhook_queue.stop()
        webhook_queue = None


def _should_process_webhook_event(event: ChatwootWebhookEvent) -> bool:
    """处理入站消息和会话状态事件，忽略 outgoing 消息。"""
    if _is_contact_block_event(event):
        return True
    if _is_conversation_status_event(event):
        return bool(event.conversation or _conversation_status(event))
    if not event.content or not event.content.strip():
        return False
    if event.message_type is None:
        return True
    return event.message_type.lower() == "incoming"


def _webhook_thread_id(event: ChatwootWebhookEvent) -> str | None:
    """按 Chatwoot 会话生成稳定的长期记忆 ID。"""
    conversation = event.conversation or {}
    account = event.account or {}
    conversation_id = conversation.get("id")
    account_id = account.get("id") or conversation.get("account_id")
    if conversation_id is not None:
        return f"chatwoot:{account_id or 'default'}:{conversation_id}"

    # 没有 conversation 对象时，再使用外部线程或 sender 维持连续性。
    if event.thread_id and event.thread_id.strip():
        return f"chatwoot:external:{event.thread_id.strip()}"
    sender_id = (event.sender or {}).get("id")
    if sender_id is not None:
        return f"chatwoot:sender:{sender_id}"
    if event.id is not None:
        return f"chatwoot:event:{event.id}"
    return None


def _webhook_conversation_reference(
    event: ChatwootWebhookEvent,
) -> tuple[str, str]:
    """返回 Chatwoot API 和会话控制共同使用的主键。"""
    conversation = event.conversation or {}
    account = event.account or {}
    account_id = str(
        account.get("id")
        or conversation.get("account_id")
        or event.raw.get("account_id")
        or settings.chatwoot_account_id
        or ""
    ).strip()
    conversation_id = str(
        conversation.get("id")
        or event.raw.get("conversation_id")
        or (
            event.id
            if _is_conversation_status_event(event)
            else ""
        )
        or ""
    ).strip()
    return account_id, conversation_id


def _webhook_event_key(
    event: ChatwootWebhookEvent,
    raw_body: bytes,
) -> str:
    """优先使用 Chatwoot 事件 ID，没有 ID 时使用请求体哈希。"""
    account = event.account or {}
    account_id = (
        account.get("id")
        or event.raw.get("account_id")
        or "default"
    )
    if event.id is not None:
        if _is_contact_block_event(event):
            return (
                f"chatwoot:{account_id}:contact:"
                f"{event.id}:{payload_hash(raw_body)}"
            )
        if _is_conversation_status_event(event):
            event_name = event.event.strip().lower().replace("_", ".")
            status = _conversation_status(event) or "unknown"
            body_hash = payload_hash(raw_body)
            return (
                f"chatwoot:{account_id}:conversation:"
                f"{event_name}:{event.id}:{status}:{body_hash}"
            )
        return f"chatwoot:{account_id}:{event.id}"
    return f"chatwoot:payload:{payload_hash(raw_body)}"


def _is_valid_chatwoot_signature(
    *,
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
) -> bool:
    webhook_secrets = tuple(
        dict.fromkeys(
            secret.strip()
            for secret in (
                settings.chatwoot_agent_bot_secret,
                settings.chatwoot_webhook_secret,
            )
            if secret and secret.strip()
        )
    )
    return (
        not webhook_secrets
        or any(
            verify_signature(
                raw_body=raw_body,
                signature=signature,
                timestamp=timestamp,
                secret=secret,
                tolerance_seconds=settings.chatwoot_webhook_tolerance_seconds,
            )
            for secret in webhook_secrets
        )
    )


async def _claim_webhook_event(
    *,
    event: ChatwootWebhookEvent,
    event_key: str,
    raw_body: bytes,
):
    try:
        return await asyncio.to_thread(
            webhook_repository.claim,
            event_key=event_key,
            event_id=None if event.id is None else str(event.id),
            event_type=event.event,
            body_hash=payload_hash(raw_body),
            stale_after_seconds=rag_settings.webhook_idempotency_stale_after_seconds,
        )
    except Exception as exc:
        logger.exception(
            "事件=Webhook幂等登记失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook idempotency storage is temporarily unavailable.",
        ) from exc


async def _enqueue_webhook_job(
    *,
    event: ChatwootWebhookEvent,
    event_key: str,
) -> None:
    if webhook_queue is None:
        await asyncio.to_thread(
            webhook_repository.mark_failed,
            event_key,
            "Webhook queue is not started.",
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook queue is temporarily unavailable.",
        )

    try:
        webhook_queue.enqueue_nowait(
            WebhookJob(event_key=event_key, event=event)
        )
    except WebhookQueueFull as exc:
        await asyncio.to_thread(
            webhook_repository.mark_failed,
            event_key,
            str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook queue is temporarily full.",
        ) from exc


def _conversation_status(event: ChatwootWebhookEvent) -> str | None:
    """提取 Chatwoot 当前会话状态。"""
    conversation = event.conversation or {}
    value = (
        conversation.get("status")
        or event.raw.get("status")
        or event.raw.get("conversation_status")
    )
    normalized = str(value or "").strip().lower()
    return normalized or None


def _webhook_contact_id(event: ChatwootWebhookEvent) -> str | None:
    """从 Webhook 载荷中提取联系人 ID，供静音状态兜底查询。"""
    conversation = event.conversation or {}
    meta = conversation.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    candidates = (
        (event.sender or {}).get("id"),
        (conversation.get("contact") or {}).get("id"),
        (conversation.get("sender") or {}).get("id"),
        (meta.get("contact") or {}).get("id"),
        (meta.get("sender") or {}).get("id"),
        (event.raw.get("contact") or {}).get("id"),
        event.raw.get("contact_id"),
    )
    if event.event.strip().lower().replace("_", ".") in CONTACT_BLOCK_EVENTS:
        candidates += (event.id,)
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _webhook_account_id(event: ChatwootWebhookEvent) -> str:
    account = event.account or {}
    conversation = event.conversation or {}
    return str(
        account.get("id")
        or conversation.get("account_id")
        or event.raw.get("account_id")
        or settings.chatwoot_account_id
        or ""
    ).strip()


def _webhook_inbox_id(event: ChatwootWebhookEvent) -> int | None:
    conversation = event.conversation or {}
    value = (
        conversation.get("inbox_id")
        or event.raw.get("inbox_id")
        or (event.raw.get("inbox") or {}).get("id")
    )
    try:
        return _optional_chatwoot_id(str(value or ""))
    except ValueError:
        return None


async def _chatwoot_contact_blocked(
    event: ChatwootWebhookEvent,
) -> bool | None:
    """读取 Chatwoot 静音/blocked 状态，未知时返回 None。"""
    blocked = contact_blocked_from_payload(event.raw)
    if blocked is not None:
        if blocked:
            await _arm_contact_block_from_message(event)
        return blocked

    account_id, conversation_id = _webhook_conversation_reference(event)
    if not account_id or not conversation_id:
        return None

    client = get_chatwoot_client()
    try:
        conversation = await asyncio.to_thread(
            client.get_conversation,
            account_id=account_id,
            conversation_id=conversation_id,
        )
        blocked = contact_blocked_from_payload(conversation)
        if blocked is not None:
            if blocked:
                await _arm_contact_block_from_message(event)
            return blocked

        contact_id = _webhook_contact_id(event)
        if contact_id:
            contact = await asyncio.to_thread(
                client.get_contact,
                account_id=account_id,
                contact_id=contact_id,
            )
            blocked = contact_blocked_from_payload(contact)
            if blocked is not None:
                if blocked:
                    await _arm_contact_block_from_message(event)
                return blocked
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.handoff.mute_lookup_failed",
            account_id=account_id,
            conversation_id=conversation_id,
            status_code=exc.status_code,
        )
        return None

    log_event(
        logger,
        logging.WARNING,
        "webhook.handoff.mute_state_missing",
        account_id=account_id,
        conversation_id=conversation_id,
    )
    return None


async def _arm_contact_block_from_message(
    event: ChatwootWebhookEvent,
) -> None:
    """为历史上已静音但未收到状态事件的联系人补建到期记录。"""
    account_id = _webhook_account_id(event)
    contact_id = _webhook_contact_id(event)
    if not account_id or not contact_id:
        return
    blocked_until = datetime.now(timezone.utc) + timedelta(
        seconds=settings.chatwoot_block_duration_seconds
    )
    await asyncio.to_thread(
        contact_block_repository.arm,
        account_id=account_id,
        contact_id=contact_id,
        blocked_until=blocked_until,
        refresh_existing=False,
    )


async def _suppress_muted_handoff(
    *,
    event: ChatwootWebhookEvent,
    event_key: str,
) -> dict[str, object]:
    """静音联系人不得被 agent 的转人工动作重新打开。"""
    account_id, conversation_id = _webhook_conversation_reference(event)
    status_synced = False
    try:
        await asyncio.to_thread(
            get_chatwoot_client().set_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status="resolved",
        )
        status_synced = True
        await asyncio.to_thread(
            conversation_control_repository.record_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status="resolved",
        )
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.handoff.muted_status_sync_failed",
            event_key=event_key,
            account_id=account_id,
            conversation_id=conversation_id,
            status_code=exc.status_code,
        )

    notice_sent = False
    notice_message_id: str | None = None
    try:
        notice_result = await asyncio.to_thread(
            get_chatwoot_client().send_message,
            account_id=account_id,
            conversation_id=conversation_id,
            content=MUTED_HANDOFF_MESSAGE,
        )
        notice_sent = True
        notice_message_id = _chatwoot_message_id(notice_result)
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.handoff.muted_notice_failed",
            event_key=event_key,
            account_id=account_id,
            conversation_id=conversation_id,
            status_code=exc.status_code,
        )

    log_event(
        logger,
        logging.INFO,
        "webhook.handoff.suppressed_for_muted_contact",
        event_key=event_key,
        account_id=account_id,
        conversation_id=conversation_id,
        status_synced=status_synced,
        notice_sent=notice_sent,
        notice_message_id=notice_message_id,
    )
    return {
        "route": "muted_contact",
        "faq_route": "muted_contact",
        "answer": MUTED_HANDOFF_MESSAGE if notice_sent else "",
        "intent_label": None,
        "intent_confidence": None,
        "handoff_required": False,
        "chatwoot_reply": {
            "enabled": True,
            "sent": notice_sent,
            "message_id": notice_message_id,
        },
    }


def _handoff_mute_check_unavailable_result() -> dict[str, object]:
    """无法确认静音状态时不执行可能打开会话的人工动作。"""
    return {
        "route": "handoff_suppressed",
        "faq_route": "handoff_suppressed",
        "answer": "",
        "intent_label": None,
        "intent_confidence": None,
        "handoff_required": False,
        "handoff_reason": "mute_state_unavailable",
        "chatwoot_reply": {
            "enabled": settings.chatwoot_auto_reply_enabled,
            "sent": False,
        },
    }


def _is_conversation_status_event(event: ChatwootWebhookEvent) -> bool:
    event_name = event.event.strip().lower().replace("_", ".")
    return event_name in CONVERSATION_STATUS_EVENTS


def _is_contact_block_event(event: ChatwootWebhookEvent) -> bool:
    event_name = event.event.strip().lower().replace("_", ".")
    if event_name in CONTACT_BLOCK_EVENTS:
        return True
    blocked_change = contact_block_change_from_payload(event.raw)
    if event_name == "conversation.updated" and blocked_change is not None:
        return True
    return (
        event_name == "conversation.updated"
        and contact_blocked_from_payload(event.raw) is True
    )


async def _contact_block_reaper_loop() -> None:
    """定期将到期的 Chatwoot 联系人解除 blocked。"""
    while True:
        await asyncio.sleep(settings.chatwoot_block_reaper_interval_seconds)
        try:
            due_blocks = await asyncio.to_thread(
                contact_block_repository.list_due,
                now=datetime.now(timezone.utc),
                limit=settings.chatwoot_block_reaper_batch_size,
            )
            for block in due_blocks:
                try:
                    await asyncio.to_thread(
                        get_chatwoot_client().set_contact_blocked,
                        account_id=block.account_id,
                        contact_id=block.contact_id,
                        blocked=False,
                    )
                    await asyncio.to_thread(
                        contact_block_repository.deactivate,
                        account_id=block.account_id,
                        contact_id=block.contact_id,
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "contact_block.expired",
                        account_id=block.account_id,
                        contact_id=block.contact_id,
                        blocked_until=block.blocked_until.isoformat(),
                    )
                except ChatwootAPIError as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "contact_block.expire_failed",
                        account_id=block.account_id,
                        contact_id=block.contact_id,
                        status_code=exc.status_code,
                        error=str(exc),
                    )
                except Exception as exc:
                    logger.exception(
                        "事件=Chatwoot联系人静音解除失败 account_id=%s contact_id=%s",
                        block.account_id,
                        block.contact_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "事件=应用.Chatwoot联系人静音到期扫描失败 错误类型=%s",
                type(exc).__name__,
            )


async def _process_contact_block_event(
    event: ChatwootWebhookEvent,
) -> dict[str, object]:
    """记录 Chatwoot 座席发起的静音或解除静音动作。"""
    account_id = _webhook_account_id(event)
    contact_id = _webhook_contact_id(event)
    blocked = contact_block_change_from_payload(event.raw)
    if blocked is None:
        blocked = contact_blocked_from_payload(event.raw)
    if not account_id or not contact_id or blocked is None:
        return {
            "route": "contact_block_ignored",
            "faq_route": "contact_block_ignored",
            "answer": "",
        }

    if blocked:
        blocked_until = datetime.now(timezone.utc) + timedelta(
            seconds=settings.chatwoot_block_duration_seconds
        )
        event_name = event.event.strip().lower().replace("_", ".")
        await asyncio.to_thread(
            contact_block_repository.arm,
            account_id=account_id,
            contact_id=contact_id,
            blocked_until=blocked_until,
            # conversation_updated 会伴随坐席后续操作重复发送；
            # 只有明确的 contact_updated 才刷新已有封禁的起算时间。
            refresh_existing=event_name in CONTACT_BLOCK_EVENTS,
        )
        log_event(
            logger,
            logging.INFO,
            "contact_block.armed",
            account_id=account_id,
            contact_id=contact_id,
            blocked_until=blocked_until.isoformat(),
        )
        return {
            "route": "contact_block_armed",
            "faq_route": "contact_block_armed",
            "answer": "",
            "blocked_until": blocked_until.isoformat(),
        }

    await asyncio.to_thread(
        contact_block_repository.deactivate,
        account_id=account_id,
        contact_id=contact_id,
    )
    log_event(
        logger,
        logging.INFO,
        "contact_block.deactivated",
        account_id=account_id,
        contact_id=contact_id,
    )
    return {
        "route": "contact_block_deactivated",
        "faq_route": "contact_block_deactivated",
        "answer": "",
    }


def _is_handoff_release_status(status: str | None) -> bool:
    return status in HANDOFF_RELEASE_STATUSES


async def _refresh_handoff_from_chatwoot_status(
    *,
    event: ChatwootWebhookEvent,
    control: ConversationControl,
) -> ConversationControl:
    """用 Webhook 或 Chatwoot 当前状态判断人工轮次是否结束。"""
    account_id, conversation_id = _webhook_conversation_reference(event)
    status = _conversation_status(event)
    if (
        control.handoff_required
        and status is None
        and account_id
        and conversation_id
    ):
        try:
            conversation = await asyncio.to_thread(
                get_chatwoot_client().get_conversation,
                account_id=account_id,
                conversation_id=conversation_id,
            )
            status = str(conversation.get("status") or "").strip().lower()
        except ChatwootAPIError as exc:
            log_event(
                logger,
                logging.WARNING,
                "webhook.handoff.status_lookup_failed",
                account_id=account_id,
                conversation_id=conversation_id,
                status_code=exc.status_code,
            )
            return control

    if status is None:
        return control

    if account_id and conversation_id:
        control = await asyncio.to_thread(
            conversation_control_repository.record_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status=status,
        )
    return control


async def _update_chatwoot_typing(
    *,
    account_id: str,
    conversation_id: str,
    typing_status: str,
) -> None:
    """更新 Chatwoot 输入状态；状态失败不应阻断 AI 回复。"""
    try:
        await asyncio.to_thread(
            get_chatwoot_client().toggle_typing_status,
            account_id=account_id,
            conversation_id=conversation_id,
            typing_status=typing_status,
        )
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.chatwoot_typing.failed",
            account_id=account_id,
            conversation_id=conversation_id,
            typing_status=typing_status,
            status_code=exc.status_code,
        )
    except Exception as exc:
        logger.warning(
            "事件=Chatwoot输入状态更新失败 状态=%s 错误类型=%s",
            typing_status,
            type(exc).__name__,
        )


async def _set_ai_chatwoot_status(
    *,
    account_id: str,
    conversation_id: str,
    event_key: str | None = None,
) -> bool:
    """尽力让 AI 会话保持 pending，但不阻断 RAG 或已发出的回复。"""
    try:
        await asyncio.to_thread(
            get_chatwoot_client().set_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status=AI_CHATWOOT_STATUS,
        )
        await asyncio.to_thread(
            conversation_control_repository.record_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status=AI_CHATWOOT_STATUS,
            release_handoff=False,
        )
        log_event(
            logger,
            logging.INFO,
            "webhook.ai_conversation.status_synced",
            event_key=event_key,
            account_id=account_id,
            conversation_id=conversation_id,
            status=AI_CHATWOOT_STATUS,
        )
        return True
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.ai_conversation.status_sync_failed",
            event_key=event_key,
            account_id=account_id,
            conversation_id=conversation_id,
            status=AI_CHATWOOT_STATUS,
            status_code=exc.status_code,
        )
        return False
    except Exception as exc:
        logger.warning(
            "事件=Chatwoot AI 会话状态同步失败 错误类型=%s",
            type(exc).__name__,
        )
        return False


async def _keep_chatwoot_typing(
    *,
    account_id: str,
    conversation_id: str,
) -> None:
    """在长 RAG 链路中周期性续期 typing_on，避免 Chatwoot 30 秒自动关闭。"""
    await _update_chatwoot_typing(
        account_id=account_id,
        conversation_id=conversation_id,
        typing_status="on",
    )
    while True:
        await asyncio.sleep(CHATWOOT_TYPING_HEARTBEAT_SECONDS)
        await _update_chatwoot_typing(
            account_id=account_id,
            conversation_id=conversation_id,
            typing_status="on",
        )


def _can_auto_reply(
    *,
    account_id: str,
    conversation_id: str,
) -> bool:
    return bool(
        settings.chatwoot_auto_reply_enabled
        and account_id
        and conversation_id
    )


async def _build_webhook_answer(
    *,
    content: str,
    thread_id: str | None,
    event_key: str,
) -> dict[str, object]:
    if is_emoji_only_text(content):
        log_event(
            logger,
            logging.INFO,
            "webhook.emoji_only",
            event_key=event_key,
            content_length=len(content),
        )
        return {
            "route": "emoji_ack",
            "faq_route": "emoji_ack",
            "answer": EMOJI_ACK_MESSAGE,
            "intent_label": None,
            "intent_confidence": None,
        }
    return await _run_rag_query(
        content,
        thread_id=thread_id,
    )


async def _stop_typing_task(
    typing_task: asyncio.Task[None] | None,
    *,
    account_id: str,
    conversation_id: str,
) -> None:
    if typing_task is None:
        return
    typing_task.cancel()
    with suppress(asyncio.CancelledError):
        await typing_task
    await _update_chatwoot_typing(
        account_id=account_id,
        conversation_id=conversation_id,
        typing_status="off",
    )


async def _send_chatwoot_auto_reply(
    *,
    event: ChatwootWebhookEvent,
    event_key: str,
    result: dict[str, object],
) -> dict[str, object]:
    reply_result: dict[str, object] = {
        "enabled": settings.chatwoot_auto_reply_enabled,
        "sent": False,
    }
    if not settings.chatwoot_auto_reply_enabled:
        return reply_result

    account_id, conversation_id = _webhook_conversation_reference(event)
    try:
        sent_message = await asyncio.to_thread(
            get_chatwoot_client().send_message,
            account_id=account_id,
            conversation_id=conversation_id,
            content=str(result.get("answer") or ""),
        )
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.chatwoot_reply.failed",
            event_key=event_key,
            status_code=exc.status_code,
            error=str(exc),
        )
        raise

    reply_result = {
        "enabled": True,
        "sent": True,
        "message_id": sent_message.get("id"),
    }
    if account_id and conversation_id:
        await asyncio.to_thread(
            conversation_control_repository.mark_ai_message,
            account_id=account_id,
            conversation_id=conversation_id,
            message_id=_chatwoot_message_id(sent_message),
        )
        await _set_ai_chatwoot_status(
            account_id=account_id,
            conversation_id=conversation_id,
            event_key=event_key,
        )
    return reply_result


async def _process_webhook_job(job: WebhookJob) -> dict[str, object]:
    """后台执行一条入站消息；结果由队列写入幂等记录。"""
    account_id, conversation_id = _webhook_conversation_reference(job.event)
    lock_key = f"{account_id}:{conversation_id}"
    lock = _conversation_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        return await _process_webhook_job_unlocked(job)


async def _process_webhook_job_unlocked(
    job: WebhookJob,
) -> dict[str, object]:
    """在同一会话串行处理，避免人工动作和 AI 回复并发交错。"""
    event = job.event
    content = event.content or ""
    account_id, conversation_id = _webhook_conversation_reference(event)
    message_id = None if event.id is None else str(event.id)
    thread_id = _webhook_thread_id(event)
    explicit_handoff = is_explicit_handoff_request(content)
    control: ConversationControl | None = None

    if _is_contact_block_event(event):
        return await _process_contact_block_event(event)

    if _is_conversation_status_event(event):
        status = _conversation_status(event)
        if account_id and conversation_id and status:
            control = await asyncio.to_thread(
                conversation_control_repository.record_conversation_status,
                account_id=account_id,
                conversation_id=conversation_id,
                status=status,
            )
        return {
            "route": "conversation_status",
            "faq_route": "conversation_status",
            "answer": "",
            "intent_label": None,
            "intent_confidence": None,
            "chatwoot_status": status,
            "handoff_released": _is_handoff_release_status(status),
        }

    if account_id and conversation_id:
        control = await asyncio.to_thread(
            conversation_control_repository.record_user_message,
            account_id=account_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            message_id=message_id,
            chatwoot_status=_conversation_status(event),
        )
        control = await _refresh_handoff_from_chatwoot_status(
            event=event,
            control=control,
        )

        if control.handoff_required:
            log_event(
                logger,
                logging.INFO,
                "webhook.ai_reply.suppressed_during_handoff",
                event_key=job.event_key,
                account_id=account_id,
                conversation_id=conversation_id,
            )
            return _handoff_suppressed_result(control)

        if explicit_handoff:
            has_available_agent = await _has_available_handoff_agent(event)
            if not has_available_agent:
                return await _suppress_no_agent_handoff(
                    event=event,
                    event_key=job.event_key,
                    availability_known=has_available_agent is not None,
                )

            blocked = await _chatwoot_contact_blocked(event)
            if blocked is True:
                return await _suppress_muted_handoff(
                    event=event,
                    event_key=job.event_key,
                )
            if blocked is None:
                # 不能确认静音状态时禁止执行 set status=open，
                # 否则 agent 会绕过 Chatwoot 的静音保护。
                return _handoff_mute_check_unavailable_result()

        if explicit_handoff:
            control, _ = await asyncio.to_thread(
                conversation_control_repository.request_handoff,
                account_id=account_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                reason="explicit_keyword",
                message_id=message_id,
                chatwoot_status=_conversation_status(event),
            )
    elif explicit_handoff:
        raise ValueError(
            "Chatwoot handoff requires account_id and conversation_id."
        )

    if explicit_handoff or (control is not None and control.handoff_required):
        if control is None:
            raise RuntimeError("Conversation control is unavailable.")
        return await _complete_handoff(
            event=event,
            job=job,
            control=control,
            content=content,
        )

    auto_reply_enabled = _can_auto_reply(
        account_id=account_id,
        conversation_id=conversation_id,
    )
    if auto_reply_enabled:
        # Chatwoot 会在收到用户入站消息后自动将会话置为 open。
        # 普通 AI 处理不应进入人工座席队列，因此立即压回 pending。
        await _set_ai_chatwoot_status(
            account_id=account_id,
            conversation_id=conversation_id,
            event_key=job.event_key,
        )

    typing_task = (
        asyncio.create_task(
            _keep_chatwoot_typing(
                account_id=account_id,
                conversation_id=conversation_id,
            )
        )
        if auto_reply_enabled
        else None
    )
    try:
        result = await _build_webhook_answer(
            content=content,
            thread_id=thread_id,
            event_key=job.event_key,
        )
    finally:
        await _stop_typing_task(
            typing_task,
            account_id=account_id,
            conversation_id=conversation_id,
        )

    if account_id and conversation_id:
        latest_control = await asyncio.to_thread(
            conversation_control_repository.get,
            account_id=account_id,
            conversation_id=conversation_id,
        )
        if latest_control is not None and (
            latest_control.handoff_required
            or not latest_control.ai_enabled
        ):
            log_event(
                logger,
                logging.INFO,
                "webhook.ai_reply.suppressed_after_handoff",
                event_key=job.event_key,
                account_id=account_id,
                conversation_id=conversation_id,
            )
            return _handoff_suppressed_result(latest_control)

    reply_result = await _send_chatwoot_auto_reply(
        event=event,
        event_key=job.event_key,
        result=result,
    )
    log_event(
        logger,
        logging.INFO,
        "webhook.job.answer_ready",
        event_key=job.event_key,
        route=result["route"],
    )
    return {
        "route": result["route"],
        "faq_route": result["faq_route"],
        "answer": result["answer"],
        "intent_label": result["intent_label"],
        "intent_confidence": result["intent_confidence"],
        "chatwoot_reply": reply_result,
    }


def _chatwoot_agent_id(agent: dict[str, object]) -> str | None:
    agent_id = agent.get("id") or agent.get("user_id")
    value = str(agent_id or "").strip()
    return value or None


def _is_available_agent(agent: dict[str, object]) -> bool:
    """Chatwoot 中 online 表示当前可接待；busy/offline 不接转人工。"""
    status = str(
        agent.get("availability_status")
        or agent.get("availability")
        or ""
    ).strip().lower()
    return status == "online"


async def _has_available_handoff_agent(
    event: ChatwootWebhookEvent,
) -> bool | None:
    """实时读取 Chatwoot，判断是否存在在线空闲坐席。"""
    account_id, _ = _webhook_conversation_reference(event)
    if not account_id:
        return None

    team_id = _optional_chatwoot_id(settings.chatwoot_handoff_team_id)
    assignee_id = _optional_chatwoot_id(settings.chatwoot_handoff_assignee_id)
    inbox_id = _webhook_inbox_id(event)
    client = get_chatwoot_client()
    try:
        if inbox_id is not None:
            agents = await asyncio.to_thread(
                client.list_inbox_members,
                account_id=account_id,
                inbox_id=inbox_id,
            )
        elif team_id is not None:
            agents = await asyncio.to_thread(
                client.list_team_members,
                account_id=account_id,
                team_id=team_id,
            )
        else:
            agents = await asyncio.to_thread(
                client.list_agents,
                account_id=account_id,
            )
        if inbox_id is not None and team_id is not None:
            team_agents = await asyncio.to_thread(
                client.list_team_members,
                account_id=account_id,
                team_id=team_id,
            )
            team_agent_ids = {
                agent_id
                for agent in team_agents
                if (agent_id := _chatwoot_agent_id(agent)) is not None
            }
            agents = [
                agent
                for agent in agents
                if _chatwoot_agent_id(agent) in team_agent_ids
            ]
    except ChatwootAPIError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.handoff.agent_availability_lookup_failed",
            account_id=account_id,
            inbox_id=inbox_id,
            team_id=team_id,
            status_code=exc.status_code,
        )
        return None

    if assignee_id is not None:
        assignee_text = str(assignee_id)
        agents = [
            agent
            for agent in agents
            if _chatwoot_agent_id(agent) == assignee_text
        ]

    available_count = sum(1 for agent in agents if _is_available_agent(agent))
    log_event(
        logger,
        logging.INFO,
        "webhook.handoff.agent_availability_checked",
        account_id=account_id,
        inbox_id=inbox_id,
        team_id=team_id,
        assignee_id=assignee_id,
        candidate_count=len(agents),
        available_count=available_count,
    )
    return available_count > 0


async def _suppress_no_agent_handoff(
    *,
    event: ChatwootWebhookEvent,
    event_key: str,
    availability_known: bool,
) -> dict[str, object]:
    """没有在线空闲坐席时只提示，不进入人工接管状态。"""
    account_id, conversation_id = _webhook_conversation_reference(event)
    status_synced = False
    notice_sent = False
    notice_message_id: str | None = None

    if account_id and conversation_id:
        status_synced = await _set_ai_chatwoot_status(
            account_id=account_id,
            conversation_id=conversation_id,
            event_key=event_key,
        )

    if (
        settings.chatwoot_handoff_notice_enabled
        and account_id
        and conversation_id
    ):
        try:
            sent_message = await asyncio.to_thread(
                get_chatwoot_client().send_message,
                account_id=account_id,
                conversation_id=conversation_id,
                content=HANDOFF_NO_AGENT_MESSAGE,
            )
            notice_sent = True
            notice_message_id = _chatwoot_message_id(sent_message)
        except ChatwootAPIError as exc:
            log_event(
                logger,
                logging.WARNING,
                "webhook.handoff.no_agent_notice_failed",
                event_key=event_key,
                status_code=exc.status_code,
            )
        except Exception as exc:
            logger.warning(
                "事件=Webhook转人工.无在线空闲坐席提示.发送失败 错误类型=%s",
                type(exc).__name__,
            )

    log_event(
        logger,
        logging.INFO,
        "webhook.handoff.suppressed_no_available_agent",
        event_key=event_key,
        account_id=account_id,
        conversation_id=conversation_id,
        availability_known=availability_known,
        status_synced=status_synced,
        notice_sent=notice_sent,
        notice_message_id=notice_message_id,
    )
    return {
        "route": "handoff_no_available_agent",
        "faq_route": "handoff_no_available_agent",
        "answer": HANDOFF_NO_AGENT_MESSAGE,
        "intent_label": None,
        "intent_confidence": None,
        "handoff_required": False,
        "handoff_reason": (
            "no_available_agent"
            if availability_known
            else "agent_availability_unavailable"
        ),
        "chatwoot_reply": {
            "enabled": settings.chatwoot_handoff_notice_enabled,
            "sent": notice_sent,
            "message_id": notice_message_id,
        },
    }


async def _complete_handoff(
    *,
    event: ChatwootWebhookEvent,
    job: WebhookJob,
    control: ConversationControl,
    content: str,
) -> dict[str, object]:
    """执行一次可重试、动作级幂等的 Chatwoot 人工接管。"""
    account_id, conversation_id = _webhook_conversation_reference(event)
    if not account_id or not conversation_id:
        raise ValueError(
            "Chatwoot handoff requires account_id and conversation_id."
        )

    if not settings.chatwoot_handoff_enabled:
        log_event(
            logger,
            logging.WARNING,
            "webhook.handoff.disabled",
            event_key=job.event_key,
        )
        return _handoff_result(
            control=control,
            actions={"enabled": False},
            notice_sent=False,
        )

    client = get_chatwoot_client()
    actions: dict[str, object] = {"enabled": True}

    if not control.handoff_conversation_updated:
        await asyncio.to_thread(
            client.set_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status="open",
        )
        await asyncio.to_thread(
            conversation_control_repository.record_conversation_status,
            account_id=account_id,
            conversation_id=conversation_id,
            status="open",
        )
        await asyncio.to_thread(
            conversation_control_repository.mark_handoff_action,
            account_id=account_id,
            conversation_id=conversation_id,
            action="conversation_updated",
        )
    actions["conversation_updated"] = True

    handoff_label = settings.chatwoot_handoff_label.strip()
    if not control.handoff_label_applied:
        if handoff_label:
            await asyncio.to_thread(
                client.add_conversation_labels,
                account_id=account_id,
                conversation_id=conversation_id,
                labels=[handoff_label],
            )
        await asyncio.to_thread(
            conversation_control_repository.mark_handoff_action,
            account_id=account_id,
            conversation_id=conversation_id,
            action="label_applied",
        )
    actions["label_applied"] = bool(handoff_label)

    if not control.handoff_note_created:
        note = _build_handoff_note(
            event=event,
            control=control,
            content=content,
        )
        note_result = await asyncio.to_thread(
            client.create_private_note,
            account_id=account_id,
            conversation_id=conversation_id,
            content=note,
        )
        await asyncio.to_thread(
            conversation_control_repository.mark_handoff_action,
            account_id=account_id,
            conversation_id=conversation_id,
            action="note_created",
            message_id=_chatwoot_message_id(note_result),
        )
    actions["note_created"] = True

    team_id = _optional_chatwoot_id(settings.chatwoot_handoff_team_id)
    assignee_id = _optional_chatwoot_id(
        settings.chatwoot_handoff_assignee_id
    )
    if not control.handoff_assignment_applied:
        if team_id is not None or assignee_id is not None:
            await asyncio.to_thread(
                client.assign_conversation,
                account_id=account_id,
                conversation_id=conversation_id,
                team_id=team_id,
                assignee_id=assignee_id,
            )
        await asyncio.to_thread(
            conversation_control_repository.mark_handoff_action,
            account_id=account_id,
            conversation_id=conversation_id,
            action="assignment_applied",
        )
    actions["assignment_applied"] = (
        team_id is not None or assignee_id is not None
    )

    notice_sent = control.handoff_notice_sent
    notice_message_id: str | None = None
    if not control.handoff_notice_sent:
        if settings.chatwoot_handoff_notice_enabled:
            notice_result = await asyncio.to_thread(
                client.send_message,
                account_id=account_id,
                conversation_id=conversation_id,
                content=HANDOFF_NOTICE_MESSAGE,
            )
            notice_message_id = _chatwoot_message_id(notice_result)
        await asyncio.to_thread(
            conversation_control_repository.mark_handoff_action,
            account_id=account_id,
            conversation_id=conversation_id,
            action="notice_sent",
            message_id=notice_message_id,
        )
        notice_sent = settings.chatwoot_handoff_notice_enabled
    actions["notice_sent"] = notice_sent

    log_event(
        logger,
        logging.INFO,
        "webhook.handoff.completed",
        event_key=job.event_key,
        account_id=account_id,
        conversation_id=conversation_id,
        reason=control.handoff_reason,
        notice_sent=notice_sent,
    )
    return _handoff_result(
        control=control,
        actions=actions,
        notice_sent=notice_sent,
    )


def _build_handoff_note(
    *,
    event: ChatwootWebhookEvent,
    control: ConversationControl,
    content: str,
) -> str:
    """构造坐席可读的接管备注，限制用户输入长度。"""
    original_content = content.strip()[:2000] or "(empty)"
    return (
        "智能客服已触发人工接管。\n"
        f"转人工原因：{control.handoff_reason or 'explicit_user_request'}\n"
        f"用户原始消息：{original_content}\n"
        f"Webhook message id：{event.id or 'unknown'}"
    )


def _optional_chatwoot_id(value: str) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid Chatwoot numeric id: {value}") from exc


def _chatwoot_message_id(result: dict[str, object]) -> str | None:
    message_id = result.get("id")
    return None if message_id is None else str(message_id)


def _handoff_suppressed_result(
    control: ConversationControl,
) -> dict[str, object]:
    return {
        "route": "handoff_suppressed",
        "faq_route": "handoff_suppressed",
        "answer": "",
        "intent_label": None,
        "intent_confidence": None,
        "handoff_required": True,
        "handoff_reason": control.handoff_reason,
        "chatwoot_reply": {
            "enabled": settings.chatwoot_auto_reply_enabled,
            "sent": False,
        },
    }


def _handoff_result(
    *,
    control: ConversationControl,
    actions: dict[str, object],
    notice_sent: bool,
) -> dict[str, object]:
    return {
        "route": "handoff",
        "faq_route": "handoff",
        "answer": HANDOFF_NOTICE_MESSAGE if notice_sent else "",
        "intent_label": None,
        "intent_confidence": None,
        "handoff_required": True,
        "handoff_reason": control.handoff_reason,
        "chatwoot_handoff": actions,
        "chatwoot_reply": {
            "enabled": (
                settings.chatwoot_handoff_enabled
                and settings.chatwoot_handoff_notice_enabled
            ),
            "sent": notice_sent,
        },
    }


@router.post("/webhooks/chatwoot")
async def chatwoot_webhook(
    request: Request,
    x_chatwoot_signature: str | None = Header(
        default=None,
        alias="X-Chatwoot-Signature",
    ),
    x_chatwoot_timestamp: str | None = Header(
        default=None,
        alias="X-Chatwoot-Timestamp",
    ),
) -> JSONResponse:
    raw_body = await request.body()

    if not _is_valid_chatwoot_signature(
        raw_body=raw_body,
        signature=x_chatwoot_signature,
        timestamp=x_chatwoot_timestamp,
    ):
        log_event(logger, logging.WARNING, "webhook.signature.invalid")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        event = parse_event(raw_body)
    except ValueError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.payload.invalid",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    account_id, conversation_id = _webhook_conversation_reference(event)
    log_event(
        logger,
        logging.INFO,
        "webhook.received",
        webhook_event=event.event,
        event_id=event.id,
        message_type=event.message_type,
        account_id=account_id,
        conversation_id=conversation_id,
        chatwoot_status=_conversation_status(event),
        contact_blocked=contact_blocked_from_payload(event.raw),
    )

    if not _should_process_webhook_event(event):
        log_event(
            logger,
            logging.DEBUG,
            "webhook.rag.skipped",
            reason="not_incoming_text_message",
        )
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "event": event.event,
                "event_id": event.id,
                "rag_processed": False,
            },
        )

    event_key = _webhook_event_key(event, raw_body)
    claim = await _claim_webhook_event(
        event=event,
        event_key=event_key,
        raw_body=raw_body,
    )

    if not claim.accepted:
        log_event(
            logger,
            logging.INFO,
            "webhook.duplicate",
            event_key=event_key,
            status=claim.status,
        )
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "duplicate": True,
                "status": claim.status,
                "event": event.event,
                "event_id": event.id,
                "event_key": event_key,
            },
        )

    handoff_detected = is_explicit_handoff_request(event.content)
    if handoff_detected:
        log_event(
            logger,
            logging.INFO,
            "webhook.handoff.detected",
            event_key=event_key,
            account_id=account_id,
            conversation_id=conversation_id,
        )

    await _enqueue_webhook_job(event=event, event_key=event_key)

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "duplicate": False,
            "status": "queued",
            "event": event.event,
            "event_id": event.id,
            "event_key": event_key,
            "handoff_detected": handoff_detected,
        },
    )
