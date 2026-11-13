"""黑名单历史与到期时间的数据库读写。

使用独立的小连接池，不占用 webhook / 转人工使用的主连接池；语句和锁等待都有上限。
所有写操作在同一联系人的事务级 advisory lock 下进行，避免客户端操作与后台补记并发。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ics_agent.app.chatwoot.state import CONTACT_BLOCK_TABLE
from .options import DURATIONS, EXPIRE_REMARK, WEB_BLOCK_REMARK, WEB_UNBLOCK_REMARK

HISTORY_TABLE = "chatwoot_contact_block_history"
# 时长档位改变时把 v2 递增，启动时自动替换数据库约束。
DURATION_CHECK = f"{HISTORY_TABLE}_duration_v2_check"
DURATION_LIST = ", ".join(f"'{code}'" for code in DURATIONS)
# 刚变化的记录可能正处于客户端两段式操作中间，后台补记留出宽限期。
RECONCILE_GRACE_SECONDS = 60
OPEN_ACTIONS = ("block", "change")


class BlacklistConflict(RuntimeError):
    """记录已被他人修改，或状态不允许当前操作。"""


class AlreadyBlocked(BlacklistConflict):
    pass


class NotBlocked(BlacklistConflict):
    pass


@dataclass(frozen=True)
class Operator:
    agent_id: str
    name: str


def _lock(connection, account_id: str, contact_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"ics-blacklist:{account_id}:{contact_id}",),
    )


# 永久拉黑以 infinity 保存；读取时一律转换为 NULL + permanent，避免驱动解析 infinity。
_BLOCK_COLUMNS = f"""
    b.account_id, b.contact_id, b.active, b.pinned, b.chatwoot_muted, b.created_at, b.updated_at,
    CASE WHEN isfinite(b.blocked_until) THEN b.blocked_until END AS blocked_until,
    NOT isfinite(b.blocked_until) AS permanent
"""
_HISTORY_COLUMNS = """
    id, event_id, account_id, contact_id, conversation_id, contact_name, contact_identifier,
    action, source, duration_code, blocked_until, permanent, previous_blocked_until,
    previous_permanent, reason_code, remark, operator_id, operator_name, occurred_at
"""


class BlacklistRepository:
    def __init__(self, app_settings, pool: ConnectionPool | None = None):
        if pool is None:
            common = {
                "connect_timeout": 3, "application_name": "ics-blacklist",
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
                CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    event_id UUID NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL,
                    conversation_id TEXT,
                    contact_name TEXT,
                    contact_identifier TEXT,
                    action TEXT NOT NULL CHECK (action IN ('block', 'change', 'unblock', 'expire')),
                    source TEXT NOT NULL CHECK (source IN ('client', 'chatwoot', 'system')),
                    duration_code TEXT,
                    blocked_until TIMESTAMPTZ,
                    permanent BOOLEAN NOT NULL DEFAULT FALSE,
                    previous_blocked_until TIMESTAMPTZ,
                    previous_permanent BOOLEAN,
                    reason_code TEXT,
                    remark TEXT NOT NULL DEFAULT '',
                    operator_id TEXT,
                    operator_name TEXT,
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TIMESTAMPTZ,
                    delivery_error_code TEXT,
                    delivery_failed_at TIMESTAMPTZ
                )
            """)
            # 时长档位约束单独命名维护：早期版本建表时是列内匿名约束（只含 24h/72h/permanent）。
            # 约束名带版本号，档位变化时改 DURATION_CHECK 的版本号，启动时替换旧约束；已有数据不受影响。
            connection.execute(f"""
                DO $$
                DECLARE item record;
                BEGIN
                    -- 多个 ICS 实例同时启动时串行执行，后到者看到新约束后直接跳过。
                    PERFORM pg_advisory_xact_lock(hashtext('{DURATION_CHECK}'));
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                                   WHERE conrelid = '{HISTORY_TABLE}'::regclass AND conname = '{DURATION_CHECK}') THEN
                        FOR item IN SELECT conname FROM pg_constraint
                            WHERE conrelid = '{HISTORY_TABLE}'::regclass AND contype = 'c'
                              AND pg_get_constraintdef(oid) LIKE '%duration_code%'
                        LOOP
                            EXECUTE format('ALTER TABLE {HISTORY_TABLE} DROP CONSTRAINT %I', item.conname);
                        END LOOP;
                        ALTER TABLE {HISTORY_TABLE} ADD CONSTRAINT {DURATION_CHECK}
                            CHECK (duration_code IN ({DURATION_LIST}));
                    END IF;
                END $$
            """)
            connection.execute(f"""
                CREATE INDEX IF NOT EXISTS {HISTORY_TABLE}_contact_idx
                ON {HISTORY_TABLE} (account_id, contact_id, id DESC)
            """)
            connection.execute(f"""
                CREATE INDEX IF NOT EXISTS {HISTORY_TABLE}_pending_idx
                ON {HISTORY_TABLE} (id) WHERE delivered_at IS NULL AND delivery_error_code IS NULL
            """)

    # ---- 读取 ----

    @staticmethod
    def _state(connection, account_id: str, contact_id: str) -> dict:
        block = connection.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM {CONTACT_BLOCK_TABLE} b WHERE b.account_id=%s AND b.contact_id=%s",
            (account_id, contact_id),
        ).fetchone()
        latest = connection.execute(
            f"SELECT {_HISTORY_COLUMNS} FROM {HISTORY_TABLE} WHERE account_id=%s AND contact_id=%s ORDER BY id DESC LIMIT 1",
            (account_id, contact_id),
        ).fetchone()
        return {"block": dict(block) if block else None, "latest": dict(latest) if latest else None,
                "version": latest["id"] if latest else 0}

    def contact(self, account_id: str, contact_id: str, history_limit: int = 50) -> dict:
        with self.pool.connection() as connection:
            state = self._state(connection, account_id, contact_id)
            rows = connection.execute(
                f"SELECT {_HISTORY_COLUMNS} FROM {HISTORY_TABLE} WHERE account_id=%s AND contact_id=%s ORDER BY id DESC LIMIT %s",
                (account_id, contact_id, history_limit),
            ).fetchall()
        block = state["block"]
        return {"active": bool(block and block["active"]), "block": block, "version": state["version"],
                "history": [dict(row) for row in rows]}

    def list_active(self, account_id: str, limit: int = 500) -> list[dict]:
        with self.pool.connection() as connection:
            rows = connection.execute(f"""
                SELECT {_BLOCK_COLUMNS},
                    COALESCE(latest.id, 0) AS version,
                    opened.occurred_at AS blocked_at, opened.reason_code, opened.remark,
                    opened.operator_id, opened.operator_name, opened.source, opened.conversation_id,
                    named.contact_name, named.contact_identifier,
                    latest.action AS latest_action, latest.operator_name AS latest_operator_name,
                    latest.occurred_at AS latest_at
                FROM {CONTACT_BLOCK_TABLE} b
                LEFT JOIN LATERAL (
                    SELECT id, action, operator_name, occurred_at FROM {HISTORY_TABLE} h
                    WHERE h.account_id=b.account_id AND h.contact_id=b.contact_id ORDER BY h.id DESC LIMIT 1
                ) latest ON TRUE
                LEFT JOIN LATERAL (
                    SELECT occurred_at, reason_code, remark, operator_id, operator_name, source, conversation_id
                    FROM {HISTORY_TABLE} h
                    WHERE h.account_id=b.account_id AND h.contact_id=b.contact_id AND h.action='block'
                    ORDER BY h.id DESC LIMIT 1
                ) opened ON TRUE
                LEFT JOIN LATERAL (
                    SELECT contact_name, contact_identifier FROM {HISTORY_TABLE} h
                    WHERE h.account_id=b.account_id AND h.contact_id=b.contact_id AND h.contact_name IS NOT NULL
                    ORDER BY h.id DESC LIMIT 1
                ) named ON TRUE
                WHERE b.active AND b.account_id=%s
                ORDER BY COALESCE(opened.occurred_at, b.updated_at) DESC, b.contact_id
                LIMIT %s
            """, (account_id, limit)).fetchall()
        return [dict(row) for row in rows]

    # ---- 客户端操作 ----

    def block(self, *, account_id: str, contact_id: str, conversation_id: str,
              contact_name: str | None, contact_identifier: str | None, duration_code: str,
              blocked_until: datetime | None, remark: str, operator: Operator) -> dict:
        """写入到期时间与拉黑历史（同一事务）。黑名单不再静音 Chatwoot 联系人。

        remark 即坐席填写的拉黑原因；reason_code 只在旧记录中存在。
        """
        with self.pool.connection() as connection:
            with connection.transaction():
                _lock(connection, account_id, contact_id)
                state = self._state(connection, account_id, contact_id)
                if state["block"] and state["block"]["active"]:
                    raise AlreadyBlocked("该客户已在黑名单中，可在黑名单管理里修改时长或解除。")
                connection.execute(f"""
                    INSERT INTO {CONTACT_BLOCK_TABLE} (account_id, contact_id, blocked_until, active, pinned, chatwoot_muted)
                    VALUES (%s, %s, CASE WHEN %s THEN 'infinity'::timestamptz ELSE %s END, TRUE, TRUE, FALSE)
                    ON CONFLICT (account_id, contact_id) DO UPDATE SET
                        blocked_until = EXCLUDED.blocked_until, active = TRUE, pinned = TRUE,
                        chatwoot_muted = FALSE, updated_at = CURRENT_TIMESTAMP
                """, (account_id, contact_id, blocked_until is None, blocked_until))
                return self.record(
                    connection, account_id=account_id, contact_id=contact_id, action="block", source="client",
                    conversation_id=conversation_id, contact_name=contact_name,
                    contact_identifier=contact_identifier, duration_code=duration_code,
                    blocked_until=blocked_until, permanent=blocked_until is None,
                    remark=remark, operator=operator)

    def record(self, connection, *, account_id: str, contact_id: str, action: str, source: str,
               conversation_id: str | None = None, contact_name: str | None = None,
               contact_identifier: str | None = None, duration_code: str | None = None,
               blocked_until: datetime | None = None, permanent: bool = False,
               previous_blocked_until: datetime | None = None, previous_permanent: bool | None = None,
               reason_code: str | None = None, remark: str = "", operator: Operator | None = None,
               occurred_at: datetime | None = None) -> dict:
        row = connection.execute(f"""
            INSERT INTO {HISTORY_TABLE} (
                event_id, account_id, contact_id, conversation_id, contact_name, contact_identifier,
                action, source, duration_code, blocked_until, permanent, previous_blocked_until,
                previous_permanent, reason_code, remark, operator_id, operator_name, occurred_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, CURRENT_TIMESTAMP))
            RETURNING {_HISTORY_COLUMNS}
        """, (uuid.uuid4(), account_id, contact_id, conversation_id, contact_name, contact_identifier,
              action, source, duration_code, blocked_until, permanent, previous_blocked_until,
              previous_permanent, reason_code, remark or "", operator.agent_id if operator else None,
              operator.name if operator else None, occurred_at)).fetchone()
        return dict(row)

    def change(self, *, account_id: str, contact_id: str, version: int, duration_code: str,
               blocked_until: datetime | None, remark: str, operator: Operator) -> dict:
        with self.pool.connection() as connection:
            with connection.transaction():
                _lock(connection, account_id, contact_id)
                state = self._state(connection, account_id, contact_id)
                block = state["block"]
                if not block or not block["active"]:
                    raise NotBlocked("该客户已不在黑名单中，请刷新列表。")
                if state["version"] != version:
                    raise BlacklistConflict("该记录已被其他坐席修改，请刷新后重试。")
                connection.execute(f"""
                    UPDATE {CONTACT_BLOCK_TABLE}
                    SET blocked_until = CASE WHEN %s THEN 'infinity'::timestamptz ELSE %s END,
                        pinned = TRUE, updated_at = CURRENT_TIMESTAMP
                    WHERE account_id=%s AND contact_id=%s AND active
                """, (blocked_until is None, blocked_until, account_id, contact_id))
                named = state["latest"] or {}
                return self.record(
                    connection, account_id=account_id, contact_id=contact_id, action="change", source="client",
                    contact_name=named.get("contact_name"), contact_identifier=named.get("contact_identifier"),
                    duration_code=duration_code, blocked_until=blocked_until, permanent=blocked_until is None,
                    previous_blocked_until=block["blocked_until"], previous_permanent=block["permanent"],
                    remark=remark, operator=operator)

    def check_unblock(self, *, account_id: str, contact_id: str, version: int) -> dict:
        with self.pool.connection() as connection:
            state = self._state(connection, account_id, contact_id)
        block = state["block"]
        if not block or not block["active"]:
            raise NotBlocked("该客户已不在黑名单中，请刷新列表。")
        if state["version"] != version:
            raise BlacklistConflict("该记录已被其他坐席修改，请刷新后重试。")
        return state

    def complete_unblock(self, *, account_id: str, contact_id: str, remark: str, operator: Operator,
                         previous: dict) -> dict:
        with self.pool.connection() as connection:
            with connection.transaction():
                _lock(connection, account_id, contact_id)
                # 到期扫描可能已先把记录置为未激活，这里重复执行无副作用。
                connection.execute(f"""
                    UPDATE {CONTACT_BLOCK_TABLE} SET active = FALSE, pinned = FALSE, chatwoot_muted = FALSE,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE account_id=%s AND contact_id=%s AND active
                """, (account_id, contact_id))
                block = previous.get("block") or {}
                named = previous.get("latest") or {}
                return self.record(
                    connection, account_id=account_id, contact_id=contact_id, action="unblock", source="client",
                    contact_name=named.get("contact_name"), contact_identifier=named.get("contact_identifier"),
                    previous_blocked_until=block.get("blocked_until"), previous_permanent=block.get("permanent"),
                    remark=remark, operator=operator)

    # ---- 后台补记：到期自动解除（以及升级前网页端静音留下的旧记录） ----

    def reconcile_candidates(self, limit: int = 100) -> list[dict]:
        with self.pool.connection() as connection:
            rows = connection.execute(f"""
                SELECT {_BLOCK_COLUMNS}, latest.action AS latest_action
                FROM {CONTACT_BLOCK_TABLE} b
                LEFT JOIN LATERAL (
                    SELECT action FROM {HISTORY_TABLE} h
                    WHERE h.account_id=b.account_id AND h.contact_id=b.contact_id ORDER BY h.id DESC LIMIT 1
                ) latest ON TRUE
                WHERE b.updated_at < CURRENT_TIMESTAMP - make_interval(secs => %s)
                  AND ((b.active AND (latest.action IS NULL OR latest.action NOT IN ('block', 'change')))
                    OR (NOT b.active AND latest.action IN ('block', 'change')))
                ORDER BY b.updated_at LIMIT %s
            """, (RECONCILE_GRACE_SECONDS, limit)).fetchall()
        return [dict(row) for row in rows]

    def record_reconciled(self, candidate: dict, *, contact_name: str | None = None,
                          contact_identifier: str | None = None) -> dict | None:
        account_id, contact_id = candidate["account_id"], candidate["contact_id"]
        with self.pool.connection() as connection:
            with connection.transaction():
                _lock(connection, account_id, contact_id)
                state = self._state(connection, account_id, contact_id)
                block, latest = state["block"], state["latest"]
                if not block:
                    return None
                is_open = bool(latest and latest["action"] in OPEN_ACTIONS)
                if block["active"] and not is_open:
                    return self.record(
                        connection, account_id=account_id, contact_id=contact_id, action="block", source="chatwoot",
                        contact_name=contact_name, contact_identifier=contact_identifier,
                        blocked_until=block["blocked_until"], permanent=block["permanent"],
                        remark=WEB_BLOCK_REMARK, occurred_at=block["updated_at"])
                if not block["active"] and is_open:
                    expired = (not block["permanent"] and block["blocked_until"] is not None
                               and block["blocked_until"] <= block["updated_at"])
                    return self.record(
                        connection, account_id=account_id, contact_id=contact_id,
                        action="expire" if expired else "unblock", source="system" if expired else "chatwoot",
                        contact_name=latest.get("contact_name"), contact_identifier=latest.get("contact_identifier"),
                        previous_blocked_until=block["blocked_until"], previous_permanent=block["permanent"],
                        remark=EXPIRE_REMARK if expired else WEB_UNBLOCK_REMARK, occurred_at=block["updated_at"])
        return None

    # ---- 归档同步 ----

    def pending_delivery(self, limit: int = 20) -> list[dict]:
        with self.pool.connection() as connection:
            rows = connection.execute(f"""
                SELECT {_HISTORY_COLUMNS} FROM {HISTORY_TABLE}
                WHERE delivered_at IS NULL AND delivery_error_code IS NULL
                ORDER BY id LIMIT %s
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(self, history_id: int) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                f"UPDATE {HISTORY_TABLE} SET delivered_at=CURRENT_TIMESTAMP WHERE id=%s AND delivered_at IS NULL",
                (history_id,))

    def quarantine(self, history_id: int, code: str) -> None:
        with self.pool.connection() as connection:
            connection.execute(f"""
                UPDATE {HISTORY_TABLE} SET delivery_error_code=%s, delivery_failed_at=CURRENT_TIMESTAMP
                WHERE id=%s AND delivered_at IS NULL AND delivery_error_code IS NULL
            """, (code, history_id))

    def delivery_counts(self) -> dict:
        with self.pool.connection() as connection:
            row = connection.execute(f"""
                SELECT COUNT(*) FILTER (WHERE delivery_error_code IS NULL) AS pending_count,
                    COUNT(*) FILTER (WHERE delivery_error_code IS NOT NULL) AS blocked_count
                FROM {HISTORY_TABLE} WHERE delivered_at IS NULL
            """).fetchone()
        return dict(row)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
