"""FastAPI 应用装配和生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .admin.routes import router as admin_router
from .core.context import (
    contact_block_repository,
    conversation_control_repository,
    rag_settings,
    settings,
    webhook_repository,
)
from .core.auth import ensure_admin_user
from .chatwoot.routes import (
    router as chatwoot_webhook_router,
    start_chatwoot_webhook_workers,
    stop_chatwoot_webhook_workers,
)
from .core.logging_config import log_event
from .public.routes import router as public_router
from ics_agent.channels import list_channels
from ics_agent.channels.bootstrap import initialize_channel
from ics_agent.rag.graph import get_rag_graph
from ics_agent.rag.memory import get_conversation_memory_manager
from reminder.backend.routes import router as reminder_router
from .blacklist.routes import router as blacklist_router
from .blacklist.runtime import get_blacklist_runtime
from .handoff_queue.runtime import get_handoff_queue_runtime
from .handoff_queue.routes import router as handoff_queue_router
from .quick_replies.routes import router as quick_replies_router
from .quick_replies.runtime import get_quick_replies_runtime
from quality.blacklist.runtime import start_blacklist_sync, stop_blacklist_sync
from reminder.backend.repository import get_repository as get_reminder_repository
from quality.query.routes import router as quality_query_router
from ics_agent.statistics.routes import router as statistics_router
from quality.query.runtime import get_query_runtime
from quality.offline.routes import router as offline_archive_router
from quality.offline.jobs import get_offline_jobs
from quality.assignment.runtime import start_assignment_sync, stop_assignment_sync
from quality.presence.runtime import get_presence_runtime
from .chatwoot.schema_recovery import ConversationControlSchemaRecovery

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """初始化基础设施、后台任务和可选的 RAG 预热。"""
    control_schema_failed = False
    control_schema_recovery = ConversationControlSchemaRecovery()
    memory_manager = get_conversation_memory_manager()
    try:
        await memory_manager.startup()
        await memory_manager.start_cleanup_task()
    except Exception as exc:
        logger.exception(
            "事件=应用.RAG对话记忆.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    try:
        await asyncio.to_thread(webhook_repository.ensure_table)
        log_event(logger, logging.INFO, "app.webhook_idempotency.ready")
    except Exception as exc:
        logger.exception(
            "事件=应用.Webhook幂等.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    try:
        await asyncio.to_thread(conversation_control_repository.ensure_table)
        log_event(logger, logging.INFO, "app.conversation_control.ready")
    except Exception as exc:
        control_schema_failed = True
        logger.exception(
            "事件=应用.Chatwoot会话控制.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    try:
        await asyncio.to_thread(contact_block_repository.ensure_table)
        log_event(logger, logging.INFO, "app.contact_block.ready")
    except Exception as exc:
        logger.exception(
            "事件=应用.Chatwoot联系人静音.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    if settings.reminder_enabled:
        # 转人工的成功标记与提醒使用同一事务，先准备增量表再启动队列。
        await asyncio.to_thread(get_reminder_repository().ensure_table)
        log_event(logger, logging.INFO, "app.reminder.ready")

    await start_chatwoot_webhook_workers()

    try:
        await asyncio.to_thread(ensure_admin_user, settings)
        log_event(logger, logging.INFO, "app.admin_auth.ready")
    except Exception as exc:
        logger.exception(
            "事件=应用.管理员认证.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    for channel in list_channels():
        try:
            result = await asyncio.to_thread(initialize_channel, channel.key)
            log_event(logger, logging.INFO, "app.channel.ready", inboxes=channel.inboxes, **result)
        except Exception as exc:
            logger.exception(
                "事件=应用.渠道.初始化失败 渠道=%s 错误类型=%s",
                channel.key, type(exc).__name__,
            )

    if rag_settings.rag_warm_up_on_startup:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "app.rag_warmup.started")
        try:
            await asyncio.to_thread(
                get_rag_graph,
                warm_up=True,
                checkpointer=memory_manager.checkpointer,
            )
        except Exception as exc:
            logger.exception(
                "事件=应用.RAG.预热.失败 错误类型=%s",
                type(exc).__name__,
            )
            log_event(
                logger,
                logging.WARNING,
                "app.rag_warmup.skipped",
                reason="warmup_failed",
                elapsed_seconds=round(time.perf_counter() - started_at, 3),
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "app.rag_warmup.completed",
                elapsed_seconds=round(time.perf_counter() - started_at, 3),
            )
    else:
        log_event(
            logger,
            logging.WARNING,
            "app.rag_warmup.skipped",
            reason="disabled_by_config",
        )

    await get_query_runtime().start()
    try:
        await get_offline_jobs().start()
    except Exception as exc:
        logger.warning("Offline archive startup unavailable: %s", type(exc).__name__)
    try:
        await get_presence_runtime().start(get_query_runtime())
    except Exception as exc:
        logger.warning("Archive presence startup unavailable: %s", type(exc).__name__)
    await get_handoff_queue_runtime().start(settings)
    await start_assignment_sync()
    # 黑名单为独立功能：自有连接池和后台任务，启动失败只影响黑名单接口。
    try:
        await get_blacklist_runtime().start(settings)
        await start_blacklist_sync()
    except Exception as exc:
        logger.warning("事件=应用.黑名单.启动失败 错误类型=%s", type(exc).__name__)
    # 坐席快捷回复同步：独立连接池，启动失败只影响快捷回复接口。
    try:
        await get_quick_replies_runtime().start(settings)
    except Exception as exc:
        logger.warning("事件=应用.快捷回复.启动失败 错误类型=%s", type(exc).__name__)
    if control_schema_failed:
        await control_schema_recovery.start(settings)
    log_event(logger, logging.INFO, "app.lifecycle.started")
    try:
        yield
    finally:
        await stop_chatwoot_webhook_workers()
        await control_schema_recovery.close()
        await get_handoff_queue_runtime().close()
        await stop_assignment_sync()
        try:
            await stop_blacklist_sync()
            await get_blacklist_runtime().close()
        except Exception as exc:
            logger.warning("事件=应用.黑名单.关闭失败 错误类型=%s", type(exc).__name__)
        try:
            await get_quick_replies_runtime().close()
        except Exception as exc:
            logger.warning("事件=应用.快捷回复.关闭失败 错误类型=%s", type(exc).__name__)
        try:
            await get_presence_runtime().close()
        except Exception as exc:
            logger.warning("Archive presence shutdown unavailable: %s", type(exc).__name__)
        await get_query_runtime().close()
        try:
            await get_offline_jobs().close()
        except Exception as exc:
            logger.warning("Offline archive shutdown unavailable: %s", type(exc).__name__)
        await memory_manager.shutdown()
        log_event(logger, logging.INFO, "app.lifecycle.stopped")


app = FastAPI(
    title="ics_agent",
    version="0.1.0",
    description="Chatwoot webhook receiver and intelligent customer-service agent.",
    lifespan=lifespan,
)


def _chatwoot_profile_origins() -> list[str]:
    return [
        origin.strip()
        for origin in settings.chatwoot_profile_allowed_origins.split(",")
        if origin.strip()
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_chatwoot_profile_origins(),
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
)
app.include_router(public_router)
app.include_router(admin_router)
app.include_router(chatwoot_webhook_router)
app.include_router(reminder_router)
app.include_router(blacklist_router)
app.include_router(handoff_queue_router)
app.include_router(quick_replies_router)
app.include_router(quality_query_router)
app.include_router(offline_archive_router)
app.include_router(statistics_router)
