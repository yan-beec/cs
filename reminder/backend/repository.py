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
                connection.execute(f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS reminder_device_agent_idx
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
                        is_test BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        acknowledged_at TIMESTAMPTZ,
                        UNIQUE(account_id, conversation_id, source_key)
                    )
                """)
                connection.execute(f"""
                    CREATE INDEX IF NOT EXISTS reminder_active_idx
                    ON {NOTIFICATIONS}(account_id, agent_id, created_at)
                    WHERE state IN ('pending', 'acknowledged', 'waiting')
                """)
        finally:
            connection.close()

    def bind_device(
        self, account_id: str, agent_id: str, agent_name: str, *,
        auth_kind: str = "admin", auth_version: str = "",
    ) -> dict[str, Any]:
        device_id, token = uuid4().hex, secrets.token_urlsafe(32)
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (f"reminder:device:{account_id}",))
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
    ) -> dict[str, Any] | None:
        """把同一台共享电脑切换到另一个客服席位。

        切换时会吊销该目标席位在本账户中的其他活动设备，保持原有“一个
        席位一个有效设备”的安全约束；当前设备凭据本身保持不变。
        """
        connection = self.connections.acquire()
        try:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (f'reminder:device:{device["account_id"]}',))
                current = connection.execute(
                    "SELECT binding_version,auth_kind,auth_version FROM " + DEVICES
                    + " WHERE id=%s AND account_id=%s AND active=TRUE FOR UPDATE",
                    (device["id"], device["account_id"]),
                ).fetchone()
                if (current is None or current[0] != expected_version or current[1] != "plugin"
                        or current[2] != device.get("auth_version")):
                    return None
                connection.execute(
                    "UPDATE " + DEVICES
                    + " SET active=FALSE WHERE account_id=%s AND agent_id=%s AND id<>%s",
                    (device["account_id"], agent_id, device["id"]),
                )
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

    def capture(self, cursor: Any, control: Any, name: str = "", message: str = "") -> None:
        cursor.execute(f"""INSERT INTO {NOTIFICATIONS}
            (id,account_id,conversation_id,source_key,agent_id,customer_name,message)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id,conversation_id,source_key) DO NOTHING""",
            (uuid4().hex, control.account_id, control.conversation_id,
             handoff_key(control.handoff_trigger_message_id, control.handoff_at),
             control.assigned_agent_id, name[:100], message[:500]))

    def ensure_handoff(self, expected: Any, name: str = "", message: str = "") -> None:
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
                        self.capture(cursor, latest, name, message)
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
