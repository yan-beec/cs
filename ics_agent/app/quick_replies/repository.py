"""快捷回复的数据库读写。

使用独立的小连接池（最多 2 个连接，语句 3 秒、锁 2 秒上限），不占用 webhook / 转人工主连接池。
每个坐席一行，整份列表以 JSONB 保存；revision 用于乐观锁，避免两台电脑同时编辑互相覆盖。
"""

from __future__ import annotations

import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

TABLE = "agent_quick_replies"


class QuickRepliesConflict(RuntimeError):
    """快捷回复已在其他设备修改。"""


class QuickRepliesRepository:
    def __init__(self, app_settings, pool: ConnectionPool | None = None):
        if pool is None:
            common = {
                "connect_timeout": 3, "application_name": "ics-quick-replies",
                "options": "-c statement_timeout=3000 -c lock_timeout=2000", "row_factory": dict_row,
            }
            source = {"conninfo": app_settings.postgres_dsn, "kwargs": common} if app_settings.postgres_dsn else {
                "kwargs": {
                    **common, "host": app_settings.postgres_host, "port": app_settings.postgres_port,
                    "dbname": app_settings.postgres_database, "user": app_settings.postgres_user,
                    "password": app_settings.postgres_password, "sslmode": app_settings.postgres_sslmode,
                },
            }
            pool = ConnectionPool(**source, min_size=0, max_size=2, timeout=3, max_waiting=8, open=False)
        self.pool = pool

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    def ensure_table(self) -> None:
        with self.pool.connection() as connection:
            connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    account_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    revision UUID NOT NULL,
                    items JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(items) = 'array'),
                    item_count INTEGER NOT NULL DEFAULT 0,
                    updated_by_name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, agent_id)
                )
            """)

    def read(self, account_id: str, agent_id: str) -> dict:
        with self.pool.connection() as connection:
            row = connection.execute(
                f"SELECT revision, items, updated_at FROM {TABLE} WHERE account_id=%s AND agent_id=%s",
                (account_id, agent_id),
            ).fetchone()
        if row is None:
            return {"revision": "", "items": [], "updated_at": None}
        return {"revision": str(row["revision"]), "items": row["items"], "updated_at": row["updated_at"]}

    def save(self, account_id: str, agent_id: str, *, expected_revision: str, items: list[dict],
             operator_name: str) -> dict:
        """expected_revision 为空表示坐席首次保存；与库中版本不一致时抛出 QuickRepliesConflict。"""
        revision = uuid.uuid4()
        with self.pool.connection() as connection:
            with connection.transaction():
                current = connection.execute(
                    f"SELECT revision FROM {TABLE} WHERE account_id=%s AND agent_id=%s FOR UPDATE",
                    (account_id, agent_id),
                ).fetchone()
                if current is None:
                    if expected_revision:
                        raise QuickRepliesConflict("快捷回复已在其他设备修改，请重新打开后再保存。")
                    inserted = connection.execute(f"""
                        INSERT INTO {TABLE} (account_id, agent_id, revision, items, item_count, updated_by_name)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (account_id, agent_id) DO NOTHING
                        RETURNING updated_at
                    """, (account_id, agent_id, revision, Jsonb(items), len(items), operator_name)).fetchone()
                    if inserted is None:
                        raise QuickRepliesConflict("快捷回复已在其他设备修改，请重新打开后再保存。")
                    updated_at = inserted["updated_at"]
                else:
                    if str(current["revision"]) != expected_revision:
                        raise QuickRepliesConflict("快捷回复已在其他设备修改，请重新打开后再保存。")
                    updated_at = connection.execute(f"""
                        UPDATE {TABLE} SET revision=%s, items=%s, item_count=%s, updated_by_name=%s,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE account_id=%s AND agent_id=%s
                        RETURNING updated_at
                    """, (revision, Jsonb(items), len(items), operator_name, account_id, agent_id)).fetchone()["updated_at"]
        return {"revision": str(revision), "items": items, "updated_at": updated_at}
