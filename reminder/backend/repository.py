"""复用 ICS 数据库池，保存设备绑定与待确认通知。"""

from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache
from typing import Any
from uuid import uuid4

from ics_agent.app.chatwoot.state import ConversationControlRepository
from ics_agent.faq.repositories import PostgreSQLConnectionPool
from reminder.protocol import handoff_key

NOTIFICATIONS = "reminder_notifications"
DEVICES = "reminder_devices"
FIELDS = (
    "id", "account_id", "conversation_id", "source_key", "agent_id", "version",
    "state", "customer_name", "message", "created_at", "is_test",
    "inbox_id", "inbox_name",
)


def current_handoff(control: Any) -> bool:
    return bool(
        control and control.handoff_required and control.assigned_agent_id
        and control.handoff_assignment_applied
        and control.last_chatwoot_status == "open"
        and not (
            control.session_closed_at is not None
            and (control.session_started_at is None
                 or control.session_closed_at >= control.session_started_at)
        )
    )


def as_dict(row: Any) -> dict[str, Any]:
    result = dict(zip(FIELDS, row))
    result["created_at"] = result["created_at"].isoformat()
    return result


class ReminderRepository:
    def __init__(self, connections: Any = None) -> None:
        self.connections = connections or PostgreSQLConnectionPool()

    def ensure_table(self) -> None:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute(f"""
                    CREATE TABLE IF NOT EXISTS {DEVICES} (
                        id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE, active BOOLEAN NOT NULL DEFAULT TRUE,
                        last_seen_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # 插件席位允许多个活动设备；启动时把旧版“一席位唯一设备”索引
                # 迁移为普通索引。实际数量由插件配置和下方事务内的容量检查控制。
                connection.execute("DROP INDEX IF EXISTS reminder_device_agent_idx")
                connection.execute(f"""
                    CREATE INDEX IF NOT EXISTS reminder_device_agent_idx
                    ON {DEVICES}(account_id, agent_id) WHERE active
                """)
                # 兼容已有设备表；旧管理员绑定的凭据仍只能访问原席位。
                for column in (
                    "auth_kind TEXT NOT NULL DEFAULT 'admin'",
                    "auth_version TEXT NOT NULL DEFAULT ''",
                    "binding_version INTEGER NOT NULL DEFAULT 1",
                ):
                    connection.execute(f"ALTER TABLE {DEVICES} ADD COLUMN IF NOT EXISTS {column}")
                connection.execute(f"""
                    CREATE TABLE IF NOT EXISTS {NOTIFICATIONS} (
                        id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL, source_key TEXT NOT NULL,
                        agent_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                        state TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'acknowledged', 'closed', 'waiting')),
                        customer_name TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                        inbox_id TEXT NOT NULL DEFAULT '', inbox_name TEXT NOT NULL DEFAULT '',
                        is_test BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        acknowledged_at TIMESTAMPTZ,
                        UNIQUE(account_id, conversation_id, source_key)
                    )
                """)
                # 已部署版本自动补齐展示元数据，不影响提醒确认状态和版本。
                for column in ("inbox_id", "inbox_name"):
                    connection.execute(
                        f"ALTER TABLE {NOTIFICATIONS} ADD COLUMN IF NOT EXISTS "
                        f"{column} TEXT NOT NULL DEFAULT ''"
                    )
                connection.execute(f"""
                    CREATE INDEX IF NOT EXISTS reminder_active_idx
                    ON {NOTIFICATIONS}(account_id, agent_id, created_at)
                    WHERE state IN ('pending', 'acknowledged', 'waiting')
                """)
        finally:
            connection.close()

    def _make_plugin_capacity(
        self, connection: Any, account_id: str, agent_id: str, max_devices: int,
    ) -> None:
        """为一台新的/切入的插件设备腾出一个席位。

        调用方已经持有账户级 advisory lock。超过上限时按最后心跳和创建时间
        选择最旧设备吊销；前 max_devices 台设备可以同时保持有效并收到提醒。
        """
        limit = max(1, int(max_devices))
        row = connection.execute(
            f"SELECT COUNT(*) FROM {DEVICES} "
            "WHERE account_id=%s AND agent_id=%s AND auth_kind='plugin' AND active=TRUE",
            (account_id, agent_id),
        ).fetchone()
        revoke_count = max(0, int(row[0]) - limit + 1)
        if revoke_count:
            connection.execute(
                f"""UPDATE {DEVICES} SET active=FALSE WHERE id IN (
                    SELECT id FROM {DEVICES}
                    WHERE account_id=%s AND agent_id=%s AND auth_kind='plugin' AND active=TRUE
                    ORDER BY COALESCE(last_seen_at, created_at), created_at, id
                    LIMIT %s
                )""",
                (account_id, agent_id, revoke_count),
            )

    def bind_device(
        self, account_id: str, agent_id: str, agent_name: str, *,
        auth_kind: str = "admin", auth_version: str = "",
        max_devices: int | None = None,
    ) -> dict[str, Any]:
        device_id, token = uuid4().hex, secrets.token_urlsafe(32)
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (f"reminder:device:{account_id}",))
                if auth_kind == "plugin" and max_devices is not None:
                    self._make_plugin_capacity(connection, account_id, agent_id, max_devices)
                else:
                    # 管理员绑定保持旧行为：重新绑定会吊销该席位的已有设备。
                    connection.execute(f"UPDATE {DEVICES} SET active=FALSE WHERE account_id=%s AND agent_id=%s",
                                       (account_id, agent_id))
                connection.execute(f"""INSERT INTO {DEVICES}
                    (id, account_id, agent_id, agent_name, token_hash, auth_kind, auth_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (device_id, account_id, agent_id, agent_name,
                     hashlib.sha256(token.encode()).hexdigest(), auth_kind, auth_version))
        finally:
            connection.close()
        return {"device_id": device_id, "token": token, "agent_name": agent_name,
                "agent_id": agent_id, "binding_version": 1, "switchable": auth_kind == "plugin"}

    def switch_device_agent(
        self, device: dict[str, Any], agent_id: str, agent_name: str, expected_version: int,
        max_devices: int | None = None,
    ) -> dict[str, Any] | None:
        """把同一台共享电脑切换到另一个客服席位。

        插件设备使用 max_devices 控制目标席位容量；未达到上限时保留所有设备，
        超过上限时吊销最旧设备。max_devices 为空时保留旧版单设备行为。
        """
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (f'reminder:device:{device["account_id"]}',))
                current = connection.execute(
                    "SELECT binding_version,auth_kind,auth_version,agent_id FROM " + DEVICES
                    + " WHERE id=%s AND account_id=%s AND active=TRUE FOR UPDATE",
                    (device["id"], device["account_id"]),
                ).fetchone()
                if (current is None or current[0] != expected_version or current[1] != "plugin"
                        or current[2] != device.get("auth_version")):
                    return None
                if current[3] != agent_id:
                    if max_devices is None:
                        connection.execute(
                            "UPDATE " + DEVICES
                            + " SET active=FALSE WHERE account_id=%s AND agent_id=%s AND id<>%s",
                            (device["account_id"], agent_id, device["id"]),
                        )
                    else:
                        self._make_plugin_capacity(connection, device["account_id"], agent_id, max_devices)
                updated = connection.execute(
                    "UPDATE " + DEVICES
                    + " SET agent_id=%s, agent_name=%s, binding_version=binding_version+1, "
                    + "last_seen_at=CURRENT_TIMESTAMP WHERE id=%s AND account_id=%s AND active=TRUE "
                    + "RETURNING binding_version",
                    (agent_id, agent_name, device["id"], device["account_id"]),
                )
                version = updated.fetchone()[0]
        finally:
            connection.close()
        return {
            "device_id": device["id"],
            "agent_id": agent_id,
            "agent_name": agent_name,
            "binding_version": version,
            "switchable": True,
        }

    def authenticate_device(self, token: str) -> dict[str, Any] | None:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                row = connection.execute(f"""UPDATE {DEVICES}
                    SET last_seen_at=CURRENT_TIMESTAMP
                    WHERE token_hash=%s AND active=TRUE
                    RETURNING id,account_id,agent_id,agent_name,auth_kind,auth_version,binding_version""",
                    (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                return dict(zip(("id", "account_id", "agent_id", "agent_name", "auth_kind",
                                 "auth_version", "binding_version"), row)) if row else None
        finally:
            connection.close()

    def revoke_device(self, device_id: str, account_id: str) -> bool:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                return connection.execute(f"UPDATE {DEVICES} SET active=FALSE WHERE id=%s AND account_id=%s",
                                          (device_id, account_id)).rowcount == 1
        finally:
            connection.close()

    def capture(
        self, cursor: Any, control: Any, name: str = "", message: str = "", *,
        inbox_id: str = "", inbox_name: str = "",
    ) -> None:
        cursor.execute(f"""INSERT INTO {NOTIFICATIONS}
            (id,account_id,conversation_id,source_key,agent_id,customer_name,message,inbox_id,inbox_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id,conversation_id,source_key) DO UPDATE SET
                inbox_id=CASE WHEN EXCLUDED.inbox_id<>'' THEN EXCLUDED.inbox_id
                              ELSE {NOTIFICATIONS}.inbox_id END,
                inbox_name=CASE WHEN EXCLUDED.inbox_name<>'' THEN EXCLUDED.inbox_name
                    WHEN EXCLUDED.inbox_id<>'' AND EXCLUDED.inbox_id<>{NOTIFICATIONS}.inbox_id THEN ''
                    ELSE {NOTIFICATIONS}.inbox_name END""",
            (uuid4().hex, control.account_id, control.conversation_id,
             handoff_key(control.handoff_trigger_message_id, control.handoff_at),
             control.assigned_agent_id, name[:100], message[:500],
             str(inbox_id or "").strip()[:100], str(inbox_name or "").strip()[:200]))

    def ensure_handoff(
        self, expected: Any, name: str = "", message: str = "", *,
        inbox_id: str = "", inbox_name: str = "",
    ) -> None:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    latest = ConversationControlRepository._fetch(
                        cursor, account_id=expected.account_id,
                        conversation_id=expected.conversation_id, for_update=True)
                    if (current_handoff(latest) and latest.handoff_conversation_updated
                            and handoff_key(latest.handoff_trigger_message_id, latest.handoff_at)
                            == handoff_key(expected.handoff_trigger_message_id, expected.handoff_at)):
                        self.capture(cursor, latest, name, message,
                                     inbox_id=inbox_id, inbox_name=inbox_name)
        finally:
            connection.close()

    def create_test(self, device_id: str, account_id: str) -> str | None:
        notification_id = uuid4().hex
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                row = connection.execute(f"SELECT agent_id FROM {DEVICES} WHERE id=%s AND account_id=%s AND active",
                                         (device_id, account_id)).fetchone()
                if not row:
                    return None
                connection.execute(f"""INSERT INTO {NOTIFICATIONS}
                    (id,account_id,conversation_id,source_key,agent_id,customer_name,message,is_test)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)""",
                    (notification_id, account_id, "test-" + notification_id, "test:" + notification_id,
                     row[0], "试验提醒", "这是 ICS 桌面提醒试验，不涉及真实客户会话。"))
        finally:
            connection.close()
        return notification_id

    def active(self, account_id: str) -> list[dict[str, Any]]:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                # 原 ICS 释放人工接管后会清空来源字段；旧轮提醒即使没有直接回调也会撤销。
                connection.execute(f"""UPDATE {NOTIFICATIONS} AS n
                    SET state='closed', version=version+1
                    WHERE account_id=%s AND state IN ('pending','acknowledged','waiting') AND (
                        (is_test AND created_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes') OR
                        (NOT is_test AND NOT EXISTS (
                            SELECT 1 FROM chatwoot_conversation_control AS c
                            WHERE c.account_id=n.account_id AND c.conversation_id=n.conversation_id
                              AND c.handoff_required AND c.handoff_assignment_applied
                              AND c.handoff_conversation_updated
                              AND (c.session_closed_at IS NULL OR
                                   c.session_closed_at < c.session_started_at)
                              AND (('message:' || c.handoff_trigger_message_id)=n.source_key OR
                                   (c.handoff_trigger_message_id IS NULL AND
                                    c.handoff_at=CASE WHEN left(n.source_key,3)='at:'
                                        THEN (substring(n.source_key from 4))::timestamptz END))
                        ))
                    )""", (account_id,))
                rows = connection.execute(f"SELECT {','.join(FIELDS)} FROM {NOTIFICATIONS} "
                                          "WHERE account_id=%s AND state IN ('pending','acknowledged','waiting') "
                                          "ORDER BY created_at", (account_id,)).fetchall()
                return [as_dict(row) for row in rows]
        finally:
            connection.close()

    def active_notification(self, account_id: str, notification_id: str) -> dict[str, Any] | None:
        """读取一条仍可处理的提醒，不执行账户范围的过期清理。"""
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                row = connection.execute(
                    f"SELECT {','.join(FIELDS)} FROM {NOTIFICATIONS} "
                    "WHERE account_id=%s AND id=%s "
                    "AND state IN ('pending','acknowledged','waiting')",
                    (account_id, notification_id),
                ).fetchone()
                return as_dict(row) if row else None
        finally:
            connection.close()

    def update_inbox(self, item: dict[str, Any], inbox_id: str, inbox_name: str) -> None:
        """补充或更新收件箱元数据；不使已确认提醒重新出现。"""
        if inbox_id == item.get("inbox_id", "") and inbox_name == item.get("inbox_name", ""):
            return
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute(f"""UPDATE {NOTIFICATIONS}
                    SET inbox_id=%s, inbox_name=%s
                    WHERE id=%s AND account_id=%s
                      AND state IN ('pending','acknowledged','waiting')""",
                    (inbox_id[:100], inbox_name[:200], item["id"], item["account_id"]))
        finally:
            connection.close()

    def synchronize(self, item: dict[str, Any], agent_id: str, status: str) -> None:
        if status == "resolved":
            next_state = "closed"
        elif status != "open" or not agent_id:
            next_state = "waiting"
        elif agent_id != item["agent_id"] or item["state"] == "waiting":
            next_state = "pending"
        else:
            return
        if next_state == item["state"] and agent_id == item["agent_id"]:
            return
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute(f"""UPDATE {NOTIFICATIONS}
                    SET agent_id=%s, state=%s, version=version+1, acknowledged_at=NULL
                    WHERE id=%s AND version=%s AND state IN ('pending','acknowledged','waiting')""",
                    (agent_id, next_state, item["id"], item["version"]))
        finally:
            connection.close()

    def acknowledge(self, device: dict[str, str], notification_id: str, version: int) -> bool:
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                if device.get("id"):
                    current = connection.execute(f"""SELECT agent_id,binding_version,active
                        FROM {DEVICES} WHERE id=%s AND account_id=%s FOR UPDATE""",
                        (device["id"], device["account_id"])).fetchone()
                    if (not current or not current[2] or current[0] != device["agent_id"]
                            or current[1] != device.get("binding_version", 1)):
                        return False
                return connection.execute(f"""UPDATE {NOTIFICATIONS}
                    SET state='acknowledged', acknowledged_at=COALESCE(acknowledged_at,CURRENT_TIMESTAMP)
                    WHERE id=%s AND account_id=%s AND agent_id=%s AND version=%s
                      AND state IN ('pending','acknowledged')""",
                    (notification_id, device["account_id"], device["agent_id"], version)).rowcount == 1
        finally:
            connection.close()


@lru_cache
def get_repository() -> ReminderRepository:
    return ReminderRepository()
