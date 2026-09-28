"""持久化 Chatwoot 联系人的临时静音到期时间。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ics_agent.faq.repositories import PostgreSQLConnectionPool


CONTACT_BLOCK_TABLE = "chatwoot_contact_block"


@dataclass(frozen=True)
class ContactBlock:
    account_id: str
    contact_id: str
    blocked_until: datetime


class ContactBlockRepository:
    """保存 Chatwoot 静音联系人及其自动解除时间。"""

    def __init__(
        self,
        connections: PostgreSQLConnectionPool | None = None,
    ) -> None:
        self._connections = connections or PostgreSQLConnectionPool()

    def ensure_table(self) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {CONTACT_BLOCK_TABLE} (
                            account_id TEXT NOT NULL,
                            contact_id TEXT NOT NULL,
                            blocked_until TIMESTAMPTZ NOT NULL,
                            active BOOLEAN NOT NULL DEFAULT TRUE,
                            created_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (account_id, contact_id)
                        )
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS
                        {CONTACT_BLOCK_TABLE}_due_idx
                        ON {CONTACT_BLOCK_TABLE} (active, blocked_until)
                        """.strip()
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    def arm(
        self,
        *,
        account_id: str,
        contact_id: str,
        blocked_until: datetime,
        refresh_existing: bool = True,
    ) -> None:
        account_id, contact_id = _validate_reference(
            account_id,
            contact_id,
        )
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"""
                        INSERT INTO {CONTACT_BLOCK_TABLE} AS current_block (
                            account_id, contact_id, blocked_until, active
                        )
                        VALUES (%s, %s, %s, TRUE)
                        ON CONFLICT (account_id, contact_id)
                        DO UPDATE SET
                            blocked_until = CASE
                                WHEN current_block.active AND %s
                                    THEN EXCLUDED.blocked_until
                                WHEN current_block.active
                                    THEN current_block.blocked_until
                                ELSE EXCLUDED.blocked_until
                            END,
                            active = TRUE,
                            updated_at = CURRENT_TIMESTAMP
                        """.strip(),
                        (
                            account_id,
                            contact_id,
                            blocked_until,
                            refresh_existing,
                        ),
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    def deactivate(
        self,
        *,
        account_id: str,
        contact_id: str,
    ) -> None:
        account_id, contact_id = _validate_reference(
            account_id,
            contact_id,
        )
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"""
                        UPDATE {CONTACT_BLOCK_TABLE}
                        SET active = FALSE,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = %s AND contact_id = %s
                        """.strip(),
                        (account_id, contact_id),
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    def list_due(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[ContactBlock]:
        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT account_id, contact_id, blocked_until
                    FROM {CONTACT_BLOCK_TABLE}
                    WHERE active = TRUE AND blocked_until <= %s
                    ORDER BY blocked_until
                    LIMIT %s
                    """.strip(),
                    (now, max(1, limit)),
                )
                return [
                    ContactBlock(
                        account_id=str(row[0]),
                        contact_id=str(row[1]),
                        blocked_until=row[2],
                    )
                    for row in cursor.fetchall()
                ]
            finally:
                cursor.close()
        finally:
            connection.close()


def _validate_reference(
    account_id: str,
    contact_id: str,
) -> tuple[str, str]:
    account_id = str(account_id or "").strip()
    contact_id = str(contact_id or "").strip()
    if not account_id:
        raise ValueError("Chatwoot account_id is required.")
    if not contact_id:
        raise ValueError("Chatwoot contact_id is required.")
    return account_id, contact_id


_REPOSITORY = ContactBlockRepository()


def get_contact_block_repository() -> ContactBlockRepository:
    """返回进程级联系人静音仓库。"""
    return _REPOSITORY
