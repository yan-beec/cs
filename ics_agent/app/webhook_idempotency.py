"""持久化 Webhook 幂等状态，避免重复入队和重复处理。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from ics_agent.app.logging_config import log_event
from ics_agent.faq.repositories import PostgreSQLConnectionPool


logger = logging.getLogger(__name__)
WEBHOOK_TABLE_NAME = "chatwoot_webhook_idempotency"


@dataclass(frozen=True)
class WebhookClaim:
    """Webhook 幂等登记结果。"""

    accepted: bool
    status: str
    attempts: int


def payload_hash(raw_body: bytes) -> str:
    """返回请求体哈希，供没有事件 ID 的事件稳定去重。"""
    return hashlib.sha256(raw_body).hexdigest()


class WebhookIdempotencyRepository:
    """使用现有 PostgreSQL 连接池保存 Webhook 处理状态。"""

    def __init__(self, connections: PostgreSQLConnectionPool | None = None):
        self._connections = connections or PostgreSQLConnectionPool()

    def ensure_table(self) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {WEBHOOK_TABLE_NAME} (
                        event_key TEXT PRIMARY KEY,
                        event_id TEXT,
                        event_type TEXT NOT NULL DEFAULT '',
                        payload_hash TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN (
                                'queued', 'processing', 'completed', 'failed'
                            )),
                        attempts INTEGER NOT NULL DEFAULT 1,
                        last_error TEXT,
                        result JSONB,
                        first_seen_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMPTZ
                    )
                    """.strip()
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS
                    {WEBHOOK_TABLE_NAME}_updated_idx
                    ON {WEBHOOK_TABLE_NAME} (updated_at)
                    """.strip()
                )
        finally:
            connection.close()

    def claim(
        self,
        *,
        event_key: str,
        event_id: str | None,
        event_type: str,
        body_hash: str,
        stale_after_seconds: int,
    ) -> WebhookClaim:
        """登记一个事件；成功事件永远不会再次入队。"""
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    INSERT INTO {WEBHOOK_TABLE_NAME} (
                        event_key, event_id, event_type, payload_hash
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (event_key) DO NOTHING
                    RETURNING status, attempts
                    """.strip(),
                    (event_key, event_id, event_type, body_hash),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    return WebhookClaim(
                        accepted=True,
                        status=str(inserted[0]),
                        attempts=int(inserted[1]),
                    )

                cursor.execute(
                    f"""
                    SELECT status, attempts
                    FROM {WEBHOOK_TABLE_NAME}
                    WHERE event_key = %s
                    FOR UPDATE
                    """.strip(),
                    (event_key,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError(
                        "Webhook idempotency row disappeared during claim."
                    )

                status = str(existing[0])
                attempts = int(existing[1])
                if status in {"queued", "completed"}:
                    return WebhookClaim(
                        accepted=False,
                        status=status,
                        attempts=attempts,
                    )

                # failed 事件允许再次被 Chatwoot 投递时重新入队；
                # processing 超过陈旧时间时允许回收 worker 崩溃遗留状态。
                cursor.execute(
                    f"""
                    UPDATE {WEBHOOK_TABLE_NAME}
                    SET
                        status = 'queued',
                        event_id = %s,
                        event_type = %s,
                        payload_hash = %s,
                        attempts = attempts + 1,
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP,
                        completed_at = NULL
                    WHERE event_key = %s
                      AND (
                          status = 'failed'
                          OR (
                              status = 'processing'
                              AND updated_at < CURRENT_TIMESTAMP
                                  - (%s * INTERVAL '1 second')
                          )
                      )
                    RETURNING status, attempts
                    """.strip(),
                    (
                        event_id,
                        event_type,
                        body_hash,
                        event_key,
                        stale_after_seconds,
                    ),
                )
                reclaimed = cursor.fetchone()
                if reclaimed is None:
                    return WebhookClaim(
                        accepted=False,
                        status=status,
                        attempts=attempts,
                    )
                return WebhookClaim(
                    accepted=True,
                    status=str(reclaimed[0]),
                    attempts=int(reclaimed[1]),
                )
        finally:
            connection.close()

    def mark_processing(self, event_key: str) -> None:
        self._update_status(
            event_key,
            status="processing",
            last_error=None,
        )

    def mark_completed(
        self,
        event_key: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    UPDATE {WEBHOOK_TABLE_NAME}
                    SET
                        status = 'completed',
                        result = %s,
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE event_key = %s
                    """.strip(),
                    (Jsonb(result) if result is not None else None, event_key),
                )
        finally:
            connection.close()

    def mark_failed(self, event_key: str, error: str) -> None:
        self._update_status(
            event_key,
            status="failed",
            last_error=error[:2000],
        )

    def get(self, event_key: str) -> dict[str, Any] | None:
        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            cursor.execute(
                f"""
                SELECT event_key, event_id, event_type, status, attempts,
                       last_error, result, first_seen_at, updated_at,
                       completed_at
                FROM {WEBHOOK_TABLE_NAME}
                WHERE event_key = %s
                """.strip(),
                (event_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "event_key": row[0],
                "event_id": row[1],
                "event_type": row[2],
                "status": row[3],
                "attempts": row[4],
                "last_error": row[5],
                "result": row[6],
                "first_seen_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
                "completed_at": row[9].isoformat() if row[9] else None,
            }
        finally:
            connection.close()

    def cleanup_expired(self, *, retention_days: int, batch_size: int) -> int:
        """清理已完成或最终失败且超过保留期的幂等记录。"""
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    DELETE FROM {WEBHOOK_TABLE_NAME}
                    WHERE event_key IN (
                        SELECT event_key
                        FROM {WEBHOOK_TABLE_NAME}
                        WHERE status IN ('completed', 'failed')
                          AND updated_at < CURRENT_TIMESTAMP
                              - (%s * INTERVAL '1 day')
                        ORDER BY updated_at
                        LIMIT %s
                    )
                    """.strip(),
                    (retention_days, batch_size),
                )
                deleted = cursor.rowcount
            if deleted:
                log_event(
                    logger,
                    logging.INFO,
                    "webhook.idempotency.cleanup.completed",
                    deleted=deleted,
                )
            return deleted
        finally:
            connection.close()

    def _update_status(
        self,
        event_key: str,
        *,
        status: str,
        last_error: str | None,
    ) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    UPDATE {WEBHOOK_TABLE_NAME}
                    SET status = %s,
                        last_error = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE event_key = %s
                    """.strip(),
                    (status, last_error, event_key),
                )
        finally:
            connection.close()


_REPOSITORY = WebhookIdempotencyRepository()


def get_webhook_idempotency_repository() -> WebhookIdempotencyRepository:
    """返回进程级 Webhook 幂等仓库。"""
    return _REPOSITORY
