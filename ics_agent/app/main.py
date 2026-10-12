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
from ics_agent.faq.repositories import ensure_faq_table
from ics_agent.rag.graph import get_rag_graph
from ics_agent.rag.memory import get_conversation_memory_manager
from reminder.backend.routes import router as reminder_router
from reminder.backend.repository import get_repository as get_reminder_repository

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """初始化基础设施、后台任务和可选的 RAG 预热。"""
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

    try:
        faq_table_created = await asyncio.to_thread(ensure_faq_table)
        log_event(
            logger,
            logging.INFO,
            "app.faq_table.ready",
            created=faq_table_created,
        )
    except Exception as exc:
        logger.exception(
            "事件=应用.FAQ数据表.初始化失败 错误类型=%s",
            type(exc).__name__,
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

    log_event(logger, logging.INFO, "app.lifecycle.started")
    try:
        yield
    finally:
        await stop_chatwoot_webhook_workers()
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
