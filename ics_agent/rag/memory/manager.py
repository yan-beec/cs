"""管理 LangGraph 的 PostgreSQL 异步 checkpoint 生命周期。"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Mapping
from typing import Any

from ics_agent.app.config import get_settings
from ics_agent.app.logging_config import log_event
from ics_agent.rag.config import get_rag_settings


logger = logging.getLogger(__name__)

# psycopg 的异步连接在 Windows 不支持 ProactorEventLoop。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class ConversationMemoryManager:
    """为 RAG 图提供进程级、可复用的 PostgreSQL checkpoint。"""

    def __init__(self) -> None:
        self._pool: Any | None = None
        self._checkpointer: Any | None = None
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def checkpointer(self) -> Any | None:
        """返回已初始化的 checkpoint；未初始化时返回 ``None``。"""
        return self._checkpointer

    async def startup(self) -> Any | None:
        """创建连接池、执行 checkpoint 表迁移并返回 checkpoint。"""
        if not get_rag_settings().rag_memory_enabled:
            log_event(
                logger,
                logging.INFO,
                "rag.memory.disabled",
                reason="disabled_by_config",
            )
            return None

        if self._checkpointer is not None:
            return self._checkpointer

        async with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer

            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from psycopg.rows import dict_row
                from psycopg_pool import AsyncConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "langgraph-checkpoint-postgres is required for RAG memory."
                ) from exc

            settings = get_settings()
            pool: Any | None = None
            try:
                pool = AsyncConnectionPool(
                    conninfo=settings.postgres_connection_string,
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "row_factory": dict_row,
                    },
                    min_size=0,
                    max_size=settings.postgres_pool_size,
                    timeout=max(1.0, settings.postgres_connection_timeout),
                    name=f"{settings.postgres_pool_name}_checkpoint",
                    open=False,
                )
                await pool.open(
                    wait=True,
                    timeout=max(1.0, settings.postgres_connection_timeout),
                )
                checkpointer = AsyncPostgresSaver(pool)
                await checkpointer.setup()
                self._pool = pool
                self._checkpointer = checkpointer
                await self._ensure_thread_table(pool)
                await self.cleanup_expired()
            except Exception:
                if pool is not None:
                    await pool.close()
                self._pool = None
                self._checkpointer = None
                raise

            log_event(
                logger,
                logging.INFO,
                "rag.memory.ready",
                pool_size=settings.postgres_pool_size,
                window_turns=get_rag_settings().rag_memory_window_turns,
                summary_interval=get_rag_settings().rag_memory_summary_interval,
            )
            return checkpointer

    async def start_cleanup_task(self) -> None:
        """启动会话过期清理任务。"""
        if self._checkpointer is None or self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="rag-memory-cleanup",
        )

    async def shutdown(self) -> None:
        """关闭 checkpoint 连接池。"""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        pool = self._pool
        self._checkpointer = None
        self._pool = None
        if pool is not None:
            await pool.close()
            log_event(logger, logging.INFO, "rag.memory.stopped")

    async def touch_thread(self, thread_id: str) -> None:
        """记录会话最近活动时间，供过期清理使用。"""
        if self._pool is None:
            return
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO rag_memory_threads (
                        thread_id, created_at, last_activity_at
                    )
                    VALUES (%s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (thread_id) DO UPDATE
                    SET last_activity_at = CURRENT_TIMESTAMP
                    """.strip(),
                    (thread_id,),
                )

    async def delete_thread(self, thread_id: str) -> bool:
        """删除一个会话的全部 checkpoint 和活动记录。"""
        if self._checkpointer is None or self._pool is None:
            return False
        await self._checkpointer.adelete_thread(thread_id)
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM rag_memory_threads WHERE thread_id = %s",
                    (thread_id,),
                )
        log_event(
            logger,
            logging.INFO,
            "rag.memory.thread.deleted",
            thread_id=thread_id,
        )
        return True

    async def cleanup_expired(self) -> int:
        """删除超过保留期且没有近期活动的会话。"""
        if self._checkpointer is None or self._pool is None:
            return 0
        settings = get_rag_settings()
        async with self._pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT thread_id
                    FROM rag_memory_threads
                    WHERE last_activity_at < CURRENT_TIMESTAMP
                        - (%s * INTERVAL '1 day')
                    ORDER BY last_activity_at
                    LIMIT %s
                    """.strip(),
                    (
                        settings.rag_memory_retention_days,
                        settings.rag_memory_cleanup_batch_size,
                    ),
                )
                rows = await cursor.fetchall()

        deleted = 0
        for row in rows:
            thread_id = str(
                row["thread_id"] if isinstance(row, Mapping) else row[0]
            )
            try:
                await self.delete_thread(thread_id)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "rag.memory.thread.delete_failed",
                    thread_id=thread_id,
                    error_type=type(exc).__name__,
                )
            else:
                deleted += 1
        if deleted:
            log_event(
                logger,
                logging.INFO,
                "rag.memory.cleanup.completed",
                deleted=deleted,
            )
        return deleted

    async def _ensure_thread_table(self, pool: Any) -> None:
        async with pool.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rag_memory_threads (
                        thread_id TEXT PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        last_activity_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """.strip()
                )
                await cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS rag_memory_threads_activity_idx
                    ON rag_memory_threads (last_activity_at)
                    """.strip()
                )

    async def _cleanup_loop(self) -> None:
        while True:
            settings = get_rag_settings()
            await asyncio.sleep(settings.rag_memory_cleanup_interval_seconds)
            try:
                await self.cleanup_expired()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "rag.memory.cleanup.failed",
                    error_type=type(exc).__name__,
                )


_MEMORY_MANAGER = ConversationMemoryManager()


def get_conversation_memory_manager() -> ConversationMemoryManager:
    """返回进程级对话记忆管理器。"""
    return _MEMORY_MANAGER
