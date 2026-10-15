"""每轮咨询结束后持久化 CSAT 邀约，独立发送并在重启后恢复。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from functools import lru_cache
from typing import Any

from ics_agent.app.core.config import AppSettings
from ics_agent.app.core.logging_config import log_event
from ics_agent.faq.repositories import PostgreSQLConnectionPool
from .client import ChatwootAPIError, ChatwootClient
from .state import ConversationControl

logger = logging.getLogger(__name__)
CSAT_TABLE = "chatwoot_csat_invitations"


class CsatInvitationRepository:
    def __init__(self, connections: Any = None) -> None:
        self.connections = connections or PostgreSQLConnectionPool()

    def ensure_table(self) -> None:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
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

    def capture(
        self, cursor: Any, control: ConversationControl, closed_at: datetime,
        *, inbox_id: str | None, assignee: dict[str, Any],
    ) -> None:
        """在关闭咨询的事务内保存快照；没有真实咨询的状态变更不发邀约。"""
        if not control.active_thread_id or control.session_started_at is None:
            return
        agent_id = assignee.get("id", control.assigned_agent_id)
        cursor.execute(f"""
            INSERT INTO {CSAT_TABLE}
                (account_id, conversation_id, round_id, inbox_id,
                 assigned_agent_id, after_message_id, closed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, conversation_id, round_id) DO NOTHING
        """, (
            control.account_id, control.conversation_id, control.active_thread_id,
            inbox_id, str(agent_id) if agent_id is not None else None,
            control.last_user_message_id, closed_at,
        ))

    def deliver_one(self, client: ChatwootClient, settings: AppSettings) -> bool:
        """只锁定一项邀约，不持有会话锁；其他进程跳过正在发送的任务。"""
        connection = self.connections.acquire()
        try:
            with connection.transaction():
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
    repository: CsatInvitationRepository, client: ChatwootClient, settings: AppSettings,
) -> None:
    while True:
        delivery = asyncio.create_task(asyncio.to_thread(repository.deliver_one, client, settings))
        try:
            delivered = await asyncio.shield(delivery)
        except asyncio.CancelledError:
            # 等待正在执行的 HTTP/事务结束，避免取消后遗留未确认发送。
            await delivery
            raise
        except Exception:
            logger.exception("事件=评价邀约.后台处理失败")
            delivered = False
        await asyncio.sleep(0 if delivered else 5)


@lru_cache
def get_csat_repository() -> CsatInvitationRepository:
    return CsatInvitationRepository()
