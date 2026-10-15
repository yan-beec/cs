"""每轮咨询结束后持久化 CSAT 邀约，独立发送并在重启后恢复。"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

from ics_agent.app.core.config import AppSettings
from ics_agent.app.core.logging_config import log_event
from ics_agent.faq.repositories import PostgreSQLConnectionPool
from .client import ChatwootAPIError, ChatwootClient, get_chatwoot_client
from .state import CONVERSATION_CONTROL_TABLE

logger = logging.getLogger(__name__)
CSAT_TABLE = "chatwoot_csat_invitations"


class _CsatConnectionPool(PostgreSQLConnectionPool):
    # 独立于问答/接管使用的池；后台单线程每次仅占用一个连接。
    _shared_pool: Any | None = None
    _pool_lock = threading.Lock()


def _set_timeouts(connection: Any) -> None:
    connection.execute("SET LOCAL lock_timeout = '250ms'")
    connection.execute("SET LOCAL statement_timeout = '2s'")


class CsatInvitationRepository:
    def __init__(self, connections: Any = None) -> None:
        self.connections = connections or _CsatConnectionPool()

    def ensure_table(self) -> None:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                _set_timeouts(connection)
                connection.execute(f"""
                    CREATE TABLE IF NOT EXISTS {CSAT_TABLE} (
                        account_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        round_id TEXT NOT NULL,
                        inbox_id TEXT,
                        assigned_agent_id TEXT,
                        after_message_id TEXT,
                        closed_at TIMESTAMPTZ NOT NULL,
                        message_id TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_error TEXT,
                        sent_at TIMESTAMPTZ,
                        PRIMARY KEY (account_id, conversation_id, round_id)
                    )
                """)
                connection.execute(f"""
                    CREATE INDEX IF NOT EXISTS chatwoot_csat_pending_idx
                    ON {CSAT_TABLE} (next_attempt_at) WHERE message_id IS NULL
                """)
        finally:
            connection.close()

    def stage_one(self) -> bool:
        """把一个已提交的结束快照转为邀约；事务失败时快照保留供重启补偿。"""
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                _set_timeouts(connection)
                with connection.cursor() as cursor:
                    # 不在访问评价任务表时锁住会话，避免表锁/慢请求阻塞接待。
                    cursor.execute(f"""
                        SELECT account_id, conversation_id, csat_pending_rounds -> 0
                        FROM {CONVERSATION_CONTROL_TABLE}
                        WHERE csat_pending_rounds <> '[]'::jsonb
                        ORDER BY updated_at, account_id, conversation_id
                        LIMIT 1
                    """)
                    row = cursor.fetchone()
                    if row is None:
                        return False
                    account_id, conversation_id, snapshot = row
                    cursor.execute(f"""
                        INSERT INTO {CSAT_TABLE}
                            (account_id, conversation_id, round_id, inbox_id,
                             assigned_agent_id, after_message_id, closed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (account_id, conversation_id, round_id) DO NOTHING
                    """, (
                        account_id, conversation_id, snapshot["round_id"], snapshot["inbox_id"],
                        snapshot["assigned_agent_id"], snapshot["after_message_id"], snapshot["closed_at"],
                    ))
                    # 插入和确认同一事务提交。并发新增的其他轮次不会被清掉，
                    # 也不更新业务 updated_at；这里只在本地短暂锁定会话行。
                    cursor.execute(f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET csat_pending_rounds = (
                            SELECT COALESCE(jsonb_agg(item), '[]'::jsonb)
                            FROM jsonb_array_elements(csat_pending_rounds) AS item
                            WHERE item ->> 'round_id' <> %s
                        )
                        WHERE account_id = %s AND conversation_id = %s
                    """, (snapshot["round_id"], account_id, conversation_id))
            return True
        finally:
            connection.close()

    def deliver_one(self, client: ChatwootClient, settings: AppSettings) -> bool:
        """只锁定一项邀约，不持有会话锁；其他进程跳过正在发送的任务。"""
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                _set_timeouts(connection)
                with connection.cursor() as cursor:
                    cursor.execute(f"""
                        SELECT account_id, conversation_id, round_id, assigned_agent_id,
                               after_message_id, closed_at, attempts
                        FROM {CSAT_TABLE}
                        WHERE message_id IS NULL AND next_attempt_at <= CURRENT_TIMESTAMP
                        ORDER BY next_attempt_at, closed_at
                        LIMIT 1 FOR UPDATE SKIP LOCKED
                    """)
                    row = cursor.fetchone()
                    if row is None:
                        return False
                    account_id, conversation_id, round_id, agent_id, after, closed_at, attempts = row
                    key = (account_id, conversation_id, round_id)
                    try:
                        result = client.find_csat_invitation(
                            account_id=account_id, conversation_id=conversation_id,
                            round_id=round_id, after_message_id=after,
                        )
                        if result is None:
                            result = client.send_csat_invitation(
                                account_id=account_id, conversation_id=conversation_id,
                                round_id=round_id, assigned_agent_id=agent_id,
                                closed_at=closed_at.isoformat(),
                                content=settings.chatwoot_csat_message,
                                display_type=settings.chatwoot_csat_display_type,
                            )
                        message_id = result.get("id")
                        if message_id is None:
                            raise ChatwootAPIError("Chatwoot CSAT response has no message ID.")
                    except Exception as exc:
                        # 包括请求超时：下次先核对远端；Chatwoot 补丁按 round_id 原子防重。
                        delay = min(3600, 60 * 2 ** min(attempts, 6))
                        cursor.execute(f"""
                            UPDATE {CSAT_TABLE}
                            SET attempts = attempts + 1, last_error = %s,
                                next_attempt_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second'
                            WHERE account_id = %s AND conversation_id = %s AND round_id = %s
                        """, (str(exc)[:1000], delay, *key))
                        log_event(
                            logger, logging.WARNING, "csat.invitation.retry",
                            account_id=account_id, conversation_id=conversation_id,
                            round_id=round_id, error_type=type(exc).__name__, retry_seconds=delay,
                        )
                    else:
                        cursor.execute(f"""
                            UPDATE {CSAT_TABLE}
                            SET message_id = %s, sent_at = CURRENT_TIMESTAMP,
                                attempts = attempts + 1, last_error = NULL
                            WHERE account_id = %s AND conversation_id = %s AND round_id = %s
                        """, (str(message_id), *key))
                        log_event(
                            logger, logging.INFO, "csat.invitation.sent",
                            account_id=account_id, conversation_id=conversation_id,
                            round_id=round_id, message_id=str(message_id),
                        )
            return True
        finally:
            connection.close()


async def run_csat_worker(
    settings: AppSettings,
    repository: CsatInvitationRepository | None = None,
    client: ChatwootClient | None = None,
) -> None:
    ready = False

    def work_once() -> bool:
        nonlocal ready, repository, client
        repository = repository or get_csat_repository()
        client = client or get_chatwoot_client()
        if not ready:
            # 建表、客户端初始化都在后台重试，不占用应用启动路径。
            repository.ensure_table()
            ready = True
        staged = False
        try:
            staged = repository.stage_one()
        except Exception:
            ready = False
            logger.exception("事件=评价邀约.快照转存失败")
        return repository.deliver_one(client, settings) or staged

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chatwoot-csat")
    try:
        while True:
            delivery = asyncio.get_running_loop().run_in_executor(executor, work_once)
            try:
                delivered = await asyncio.shield(delivery)
            except asyncio.CancelledError:
                # 等待有超时限制的当前操作结束，防止关闭后仍有发送线程。
                await asyncio.gather(delivery, return_exceptions=True)
                raise
            except Exception:
                ready = False
                logger.exception("事件=评价邀约.后台处理失败")
                delivered = False
            await asyncio.sleep(0 if delivered else 5)
    finally:
        executor.shutdown(wait=False)


@lru_cache
def get_csat_repository() -> CsatInvitationRepository:
    return CsatInvitationRepository()
