"""持久化 Chatwoot 会话的 AI/人工接管状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ics_agent.faq.repositories import PostgreSQLConnectionPool


CONVERSATION_CONTROL_TABLE = "chatwoot_conversation_control"
CONTACT_BLOCK_TABLE = "chatwoot_contact_block"
HANDOFF_CURSOR_TABLE = "chatwoot_handoff_round_robin"
HANDOFF_RESERVATION_TABLE = "chatwoot_handoff_reservation"
_CONTROL_COLUMNS = """
    account_id, conversation_id, active_thread_id,
    last_chatwoot_status,
    ai_enabled, handoff_required, handoff_reason, handoff_at,
    assigned_team_id, assigned_agent_id, last_user_message_id,
    last_ai_message_id, last_handoff_message_id,
    handoff_conversation_updated, handoff_label_applied,
    handoff_note_created, handoff_assignment_applied,
    handoff_notice_sent, handoff_trigger_message_id
""".strip()


@dataclass(frozen=True)
class ConversationControl:
    """一个 Chatwoot 会话当前线程的处理控制状态。"""

    account_id: str
    conversation_id: str
    active_thread_id: str | None
    last_chatwoot_status: str | None
    ai_enabled: bool
    handoff_required: bool
    handoff_reason: str | None
    handoff_at: str | None
    assigned_team_id: str | None
    assigned_agent_id: str | None
    last_user_message_id: str | None
    last_ai_message_id: str | None
    last_handoff_message_id: str | None
    handoff_conversation_updated: bool
    handoff_label_applied: bool
    handoff_note_created: bool
    handoff_assignment_applied: bool
    handoff_notice_sent: bool
    # 触发当前人工轮次的入站消息；用于队列重试续跑未完成动作。
    handoff_trigger_message_id: str | None = None


@dataclass(frozen=True)
class ContactBlock:
    """一个 Chatwoot 联系人的临时静音记录。"""

    account_id: str
    contact_id: str
    blocked_until: datetime


class ConversationControlRepository:
    """使用现有 PostgreSQL 连接池管理会话接管状态。"""

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
                        CREATE TABLE IF NOT EXISTS
                        {CONVERSATION_CONTROL_TABLE} (
                            account_id TEXT NOT NULL,
                            conversation_id TEXT NOT NULL,
                            active_thread_id TEXT,
                            last_chatwoot_status TEXT,
                            ai_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                            handoff_required BOOLEAN NOT NULL DEFAULT FALSE,
                            handoff_reason TEXT,
                            handoff_at TIMESTAMPTZ,
                            assigned_team_id TEXT,
                            assigned_agent_id TEXT,
                            last_user_message_id TEXT,
                            last_ai_message_id TEXT,
                            last_handoff_message_id TEXT,
                            handoff_conversation_updated BOOLEAN NOT NULL
                                DEFAULT FALSE,
                            handoff_label_applied BOOLEAN NOT NULL DEFAULT FALSE,
                            handoff_note_created BOOLEAN NOT NULL DEFAULT FALSE,
                            handoff_assignment_applied BOOLEAN NOT NULL
                                DEFAULT FALSE,
                            handoff_notice_sent BOOLEAN NOT NULL DEFAULT FALSE,
                            handoff_trigger_message_id TEXT,
                            created_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (account_id, conversation_id)
                        )
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        ALTER TABLE {CONVERSATION_CONTROL_TABLE}
                        ADD COLUMN IF NOT EXISTS active_thread_id TEXT
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        ALTER TABLE {CONVERSATION_CONTROL_TABLE}
                        ADD COLUMN IF NOT EXISTS last_chatwoot_status TEXT
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        ALTER TABLE {CONVERSATION_CONTROL_TABLE}
                        ADD COLUMN IF NOT EXISTS handoff_trigger_message_id TEXT
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET active_thread_id = (
                            'chatwoot:' || account_id || ':' || conversation_id
                        )
                        WHERE active_thread_id IS NULL
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS
                        {CONVERSATION_CONTROL_TABLE}_handoff_idx
                        ON {CONVERSATION_CONTROL_TABLE}
                            (handoff_required, updated_at)
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {HANDOFF_CURSOR_TABLE} (
                            scope_key TEXT PRIMARY KEY,
                            last_agent_id TEXT,
                            updated_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP
                        )
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {HANDOFF_RESERVATION_TABLE} (
                            account_id TEXT NOT NULL,
                            conversation_id TEXT NOT NULL,
                            scope_key TEXT NOT NULL,
                            agent_id TEXT NOT NULL,
                            active BOOLEAN NOT NULL DEFAULT TRUE,
                            created_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (account_id, conversation_id)
                        )
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS
                        {HANDOFF_RESERVATION_TABLE}_scope_agent_idx
                        ON {HANDOFF_RESERVATION_TABLE}
                            (scope_key, agent_id, active)
                        """.strip()
                    )
                    # 旧版本升级或服务重启时，已结束的会话不应继续占用
                    # 人工容量；此时 reservation 表已经存在。
                    cursor.execute(
                        f"""
                        UPDATE {HANDOFF_RESERVATION_TABLE} AS reservation
                        SET active = FALSE,
                            updated_at = CURRENT_TIMESTAMP
                        FROM {CONVERSATION_CONTROL_TABLE} AS control
                        WHERE reservation.account_id = control.account_id
                          AND reservation.conversation_id = control.conversation_id
                          AND control.last_chatwoot_status = 'resolved'
                        """.strip()
                    )
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET
                            ai_enabled = TRUE,
                            handoff_required = FALSE,
                            handoff_reason = NULL,
                            handoff_at = NULL,
                            assigned_team_id = NULL,
                            assigned_agent_id = NULL,
                            last_handoff_message_id = NULL,
                            handoff_conversation_updated = FALSE,
                            handoff_label_applied = FALSE,
                            handoff_note_created = FALSE,
                            handoff_assignment_applied = FALSE,
                            handoff_notice_sent = FALSE,
                            handoff_trigger_message_id = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE handoff_required = TRUE
                          AND last_chatwoot_status = 'resolved'
                        """.strip()
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    def get(
        self,
        *,
        account_id: str,
        conversation_id: str,
    ) -> ConversationControl | None:
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            try:
                control = self._fetch(
                    cursor,
                    account_id=account_id,
                    conversation_id=conversation_id,
                )
            finally:
                cursor.close()
        finally:
            connection.close()
        return control

    def record_user_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        thread_id: str | None,
        message_id: str | None,
        chatwoot_status: str | None = None,
    ) -> ConversationControl:
        """记录消息；切换线程时恢复该新线程的 AI 能力。"""
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        clean_thread_id = _clean_thread_id(thread_id)
        clean_status = _clean_status(chatwoot_status)
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"""
                        INSERT INTO {CONVERSATION_CONTROL_TABLE} (
                            account_id, conversation_id, active_thread_id,
                            last_chatwoot_status, last_user_message_id
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (account_id, conversation_id)
                        DO NOTHING
                        """.strip(),
                        (
                            account_id,
                            conversation_id,
                            clean_thread_id,
                            clean_status,
                            message_id,
                        ),
                    )
                    control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        for_update=True,
                    )
                    if control is None:
                        raise RuntimeError(
                            "Conversation control row was not created."
                        )

                    previous_status = control.last_chatwoot_status
                    release_handoff = control.handoff_required and (
                        clean_status == "resolved"
                        or (
                            clean_status == "open"
                            and previous_status == "resolved"
                        )
                    )
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET
                            active_thread_id = COALESCE(
                                active_thread_id, %s
                            ),
                            last_chatwoot_status = COALESCE(
                                %s, last_chatwoot_status
                            ),
                            last_user_message_id = COALESCE(
                                %s, last_user_message_id
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = %s AND conversation_id = %s
                        """.strip(),
                        (
                            clean_thread_id,
                            clean_status,
                            message_id,
                            account_id,
                            conversation_id,
                        ),
                    )
                    if release_handoff:
                        self._release_handoff_cursor(
                            cursor,
                            account_id=account_id,
                            conversation_id=conversation_id,
                            status=clean_status or previous_status,
                        )
                    control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()
        if control is None:
            raise RuntimeError("Conversation control row disappeared.")
        return control

    def request_handoff(
        self,
        *,
        account_id: str,
        conversation_id: str,
        thread_id: str | None,
        reason: str,
        message_id: str | None,
        chatwoot_status: str | None = None,
    ) -> tuple[ConversationControl, bool]:
        """关闭当前线程 AI，并返回是否由本次请求首次触发接管。"""
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        clean_thread_id = _clean_thread_id(thread_id)
        clean_status = _clean_status(chatwoot_status)
        clean_reason = reason.strip() or "explicit_user_request"
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"chatwoot:handoff:account={account_id}",),
                    )
                    cursor.execute(
                        f"""
                        INSERT INTO {CONVERSATION_CONTROL_TABLE} (
                            account_id, conversation_id, active_thread_id,
                            last_chatwoot_status, ai_enabled,
                            handoff_required, handoff_reason,
                            handoff_at, last_user_message_id,
                            handoff_trigger_message_id
                        )
                        VALUES (
                            %s, %s, %s, %s, FALSE, TRUE, %s,
                            CURRENT_TIMESTAMP, %s, %s
                        )
                        ON CONFLICT (account_id, conversation_id)
                        DO NOTHING
                        """.strip(),
                        (
                            account_id,
                            conversation_id,
                            clean_thread_id,
                            clean_status,
                            clean_reason,
                            message_id,
                            message_id,
                        ),
                    )
                    inserted = cursor.rowcount == 1
                    control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        for_update=True,
                    )
                    if control is None:
                        raise RuntimeError(
                            "Conversation control row was not created."
                        )

                    if inserted:
                        transitioned = True
                    elif control.handoff_required:
                        cursor.execute(
                            f"""
                            UPDATE {CONVERSATION_CONTROL_TABLE}
                            SET
                                last_chatwoot_status = COALESCE(
                                    %s, last_chatwoot_status
                                ),
                                last_user_message_id = COALESCE(
                                    %s, last_user_message_id
                                ),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE account_id = %s AND conversation_id = %s
                            """.strip(),
                            (
                                clean_status,
                                message_id,
                                account_id,
                                conversation_id,
                            ),
                        )
                        transitioned = False
                    else:
                        cursor.execute(
                            f"""
                            UPDATE {CONVERSATION_CONTROL_TABLE}
                            SET
                                ai_enabled = FALSE,
                                handoff_required = TRUE,
                                handoff_reason = %s,
                                assigned_team_id = NULL,
                                assigned_agent_id = NULL,
                                last_chatwoot_status = COALESCE(
                                    %s, last_chatwoot_status
                                ),
                                handoff_at = COALESCE(
                                    handoff_at, CURRENT_TIMESTAMP
                                ),
                                last_user_message_id = COALESCE(
                                    %s, last_user_message_id
                                ),
                                handoff_trigger_message_id = COALESCE(
                                    handoff_trigger_message_id, %s
                                ),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE account_id = %s AND conversation_id = %s
                            """.strip(),
                            (
                                clean_reason,
                                clean_status,
                                message_id,
                                message_id,
                                account_id,
                                conversation_id,
                            ),
                        )
                        transitioned = True

                    # 预留可以发生在首次 request_handoff 之前；将预留的
                    # 坐席绑定到控制行，后续动作重试即可复用该坐席。
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE} AS control
                        SET assigned_agent_id = COALESCE(
                                control.assigned_agent_id, reservation.agent_id
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        FROM {HANDOFF_RESERVATION_TABLE} AS reservation
                        WHERE control.account_id = %s
                          AND control.conversation_id = %s
                          AND reservation.account_id = control.account_id
                          AND reservation.conversation_id = control.conversation_id
                          AND reservation.active = TRUE
                        """.strip(),
                        (account_id, conversation_id),
                    )

                    control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()
        if control is None:
            raise RuntimeError("Conversation control row disappeared.")
        return control, transitioned

    def record_conversation_status(
        self,
        *,
        account_id: str,
        conversation_id: str,
        status: str,
        release_handoff: bool = True,
    ) -> ConversationControl:
        """记录 Chatwoot 状态，并按需结束当前人工接管轮次。"""
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        clean_status = _clean_status(status)
        if clean_status is None:
            raise ValueError("Chatwoot conversation status is required.")
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    if release_handoff and clean_status == "resolved":
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"chatwoot:handoff:account={account_id}",),
                        )
                    previous_control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        for_update=True,
                    )
                    cursor.execute(
                        f"""
                        INSERT INTO {CONVERSATION_CONTROL_TABLE} (
                            account_id, conversation_id,
                            last_chatwoot_status
                        )
                        VALUES (%s, %s, %s)
                        ON CONFLICT (account_id, conversation_id)
                        DO UPDATE SET
                            last_chatwoot_status = EXCLUDED.last_chatwoot_status,
                            updated_at = CURRENT_TIMESTAMP
                        """.strip(),
                        (account_id, conversation_id, clean_status),
                    )
                    if (
                        release_handoff
                        and previous_control is not None
                        and previous_control.handoff_required
                        and clean_status == "resolved"
                    ):
                        self._release_handoff_cursor(
                            cursor,
                            account_id=account_id,
                            conversation_id=conversation_id,
                            status=clean_status,
                        )
                    control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()
        if control is None:
            raise RuntimeError("Conversation control row was not created.")
        return control

    def release_handoff(
        self,
        *,
        account_id: str,
        conversation_id: str,
        status: str,
    ) -> ConversationControl | None:
        """结束人工接管，下一条用户消息可重新进入 AI。"""
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        clean_status = _clean_status(status)
        if clean_status != "resolved":
            raise ValueError(
                "Only resolved can release a handoff."
            )
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    self._release_handoff_cursor(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        status=clean_status,
                    )
                    control = self._fetch(
                        cursor,
                        account_id=account_id,
                        conversation_id=conversation_id,
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()
        return control

    def reserve_handoff_agent(
        self,
        *,
        account_id: str,
        conversation_id: str,
        scope_key: str,
        candidate_agent_ids: list[str],
        load_open_assignments: Callable[[], dict[str, set[str]]],
        max_active_conversations: int = 5,
        assigned_team_id: str | None = None,
    ) -> str | None:
        """在一个接管范围内按轮询顺序预留一个坐席。

        游标按 Inbox/Team 分开，容量按账号内坐席的全部 open 会话计算。
        在账号事务锁内获取最新负载，并将未完成转接的预留按会话去重。
        释放预留也获取同一把锁，防止旧负载快照漏掉刚完成的转接。
        """
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        scope_key = str(scope_key or "").strip()
        if not scope_key:
            raise ValueError("Chatwoot handoff scope_key is required.")
        candidates = sorted(
            {str(agent_id).strip() for agent_id in candidate_agent_ids if str(agent_id).strip()},
            key=lambda value: (
                0,
                int(value),
            )
            if value.isdigit()
            else (1, value),
        )
        if not candidates:
            return None
        capacity = int(max_active_conversations)
        if capacity < 1:
            raise ValueError("Chatwoot handoff capacity must be positive.")
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    # 不同 Inbox 可能共享同一坐席，必须按账号串行检查容量。
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"chatwoot:handoff:account={account_id}",),
                    )
                    # request_handoff 与 reservation 不在同一个事务中：若
                    # 控制行写入失败，临时预留可能留下。下一次分配前清理
                    # 没有进行中的人工接管的记录，避免幽灵容量长期占用。
                    cursor.execute(
                        f"""
                        UPDATE {HANDOFF_RESERVATION_TABLE} AS reservation
                        SET active = FALSE,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE reservation.account_id = %s
                          AND reservation.active = TRUE
                          AND reservation.updated_at < CURRENT_TIMESTAMP
                              - INTERVAL '15 minutes'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM {CONVERSATION_CONTROL_TABLE} AS control
                              WHERE control.account_id = reservation.account_id
                                AND control.conversation_id = reservation.conversation_id
                                AND control.handoff_required = TRUE
                          )
                        """.strip(),
                        (account_id,),
                    )
                    cursor.execute(
                        f"""
                        SELECT agent_id
                        FROM {HANDOFF_RESERVATION_TABLE}
                        WHERE account_id = %s AND conversation_id = %s
                          AND active = TRUE
                        FOR UPDATE
                        """.strip(),
                        (account_id, conversation_id),
                    )
                    existing = cursor.fetchone()
                    if existing and existing[0] is not None:
                        return str(existing[0])

                    # 必须在获得锁之后取快照。锁外先查后预留会在并发完成
                    # open 并释放预留时漏算名额。
                    assigned = load_open_assignments()
                    loads = {
                        str(agent_id): set(conversation_ids)
                        for agent_id, conversation_ids in assigned.items()
                    }

                    cursor.execute(
                        f"""
                        SELECT last_agent_id
                        FROM {HANDOFF_CURSOR_TABLE}
                        WHERE scope_key = %s
                        FOR UPDATE
                        """.strip(),
                        (scope_key,),
                    )
                    cursor_row = cursor.fetchone()
                    last_agent_id = (
                        None
                        if not cursor_row or cursor_row[0] is None
                        else str(cursor_row[0])
                    )
                    if cursor_row is None:
                        cursor.execute(
                            f"""
                            INSERT INTO {HANDOFF_CURSOR_TABLE} (scope_key)
                            VALUES (%s)
                            ON CONFLICT (scope_key) DO NOTHING
                            """.strip(),
                            (scope_key,),
                        )

                    start = 0
                    if last_agent_id in candidates:
                        start = (candidates.index(last_agent_id) + 1) % len(candidates)
                    ordered = candidates[start:] + candidates[:start]

                    cursor.execute(
                        f"""
                        SELECT agent_id, conversation_id
                        FROM {HANDOFF_RESERVATION_TABLE}
                        WHERE account_id = %s AND active = TRUE
                        """.strip(),
                        (account_id,),
                    )
                    for agent_id, reserved_conversation_id in cursor.fetchall():
                        loads.setdefault(str(agent_id), set()).add(
                            str(reserved_conversation_id)
                        )
                    selected: str | None = None
                    for agent_id in ordered:
                        # 当前会话可能已有历史 assignee；重新分配本身不额外
                        # 消耗该会话自己的名额。
                        used = len(loads.get(agent_id, set()) - {conversation_id})
                        if used < capacity:
                            selected = agent_id
                            break
                    if selected is None:
                        return None

                    cursor.execute(
                        f"""
                        INSERT INTO {HANDOFF_RESERVATION_TABLE} (
                            account_id, conversation_id, scope_key, agent_id,
                            active
                        )
                        VALUES (%s, %s, %s, %s, TRUE)
                        ON CONFLICT (account_id, conversation_id)
                        DO UPDATE SET
                            scope_key = EXCLUDED.scope_key,
                            agent_id = EXCLUDED.agent_id,
                            active = TRUE,
                            updated_at = CURRENT_TIMESTAMP
                        """.strip(),
                        (account_id, conversation_id, scope_key, selected),
                    )
                    cursor.execute(
                        f"""
                        UPDATE {HANDOFF_CURSOR_TABLE}
                        SET last_agent_id = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE scope_key = %s
                        """.strip(),
                        (selected, scope_key),
                    )
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET assigned_agent_id = %s,
                            assigned_team_id = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = %s AND conversation_id = %s
                        """.strip(),
                        (selected, assigned_team_id, account_id, conversation_id),
                    )
                    return selected
                finally:
                    cursor.close()
        finally:
            connection.close()

    def release_handoff_reservation(
        self,
        *,
        account_id: str,
        conversation_id: str,
        only_if_not_started: bool = False,
    ) -> None:
        """在 Chatwoot 已打开人工会话后释放本地容量预留。

        与取负载/预留使用同一账号锁。request_handoff 写入失败时可只
        取消尚未开始接管的预留，避免错误释放已进入人工流程的容量。
        """
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"chatwoot:handoff:account={account_id}",),
                    )
                    cursor.execute(
                        f"""
                        UPDATE {HANDOFF_RESERVATION_TABLE} AS reservation
                        SET active = FALSE,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = %s AND conversation_id = %s
                          AND (
                              %s = FALSE OR NOT EXISTS (
                                  SELECT 1 FROM {CONVERSATION_CONTROL_TABLE} AS control
                                  WHERE control.account_id = reservation.account_id
                                    AND control.conversation_id = reservation.conversation_id
                                    AND control.handoff_required = TRUE
                              )
                          )
                        """.strip(),
                        (account_id, conversation_id, only_if_not_started),
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    def mark_handoff_action(
        self,
        *,
        account_id: str,
        conversation_id: str,
        action: str,
        message_id: str | None = None,
    ) -> None:
        fields = {
            "conversation_updated": "handoff_conversation_updated",
            "label_applied": "handoff_label_applied",
            "note_created": "handoff_note_created",
            "assignment_applied": "handoff_assignment_applied",
            "notice_sent": "handoff_notice_sent",
        }
        column = fields.get(action)
        if column is None:
            raise ValueError(f"Unsupported handoff action: {action}")
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET
                            {column} = TRUE,
                            last_handoff_message_id = COALESCE(
                                %s, last_handoff_message_id
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = %s AND conversation_id = %s
                        """.strip(),
                        (message_id, account_id, conversation_id),
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    def mark_ai_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        message_id: str | None,
    ) -> None:
        account_id, conversation_id = _validate_reference(
            account_id,
            conversation_id,
        )
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        f"""
                        UPDATE {CONVERSATION_CONTROL_TABLE}
                        SET last_ai_message_id = COALESCE(
                                %s, last_ai_message_id
                            ),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = %s AND conversation_id = %s
                        """.strip(),
                        (message_id, account_id, conversation_id),
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()

    @staticmethod
    def _fetch(
        cursor: Any,
        *,
        account_id: str,
        conversation_id: str,
        for_update: bool = False,
    ) -> ConversationControl | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT {_CONTROL_COLUMNS}
            FROM {CONVERSATION_CONTROL_TABLE}
            WHERE account_id = %s AND conversation_id = %s
            {lock}
            """.strip(),
            (account_id, conversation_id),
        )
        return _control_from_row(cursor.fetchone())

    @staticmethod
    def _release_handoff_cursor(
        cursor: Any,
        *,
        account_id: str,
        conversation_id: str,
        status: str,
    ) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"chatwoot:handoff:account={account_id}",),
        )
        cursor.execute(
            f"""
            UPDATE {CONVERSATION_CONTROL_TABLE}
            SET
                ai_enabled = TRUE,
                handoff_required = FALSE,
                handoff_reason = NULL,
                handoff_at = NULL,
                last_chatwoot_status = %s,
                assigned_team_id = NULL,
                assigned_agent_id = NULL,
                last_handoff_message_id = NULL,
                handoff_conversation_updated = FALSE,
                handoff_label_applied = FALSE,
                handoff_note_created = FALSE,
                handoff_assignment_applied = FALSE,
                handoff_notice_sent = FALSE,
                handoff_trigger_message_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = %s AND conversation_id = %s
            """.strip(),
            (status, account_id, conversation_id),
        )
        cursor.execute(
            f"""
            UPDATE {HANDOFF_RESERVATION_TABLE}
            SET active = FALSE,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = %s AND conversation_id = %s
            """.strip(),
            (account_id, conversation_id),
        )


def _validate_reference(
    account_id: str,
    conversation_id: str,
) -> tuple[str, str]:
    account_id = str(account_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if not account_id:
        raise ValueError("Chatwoot account_id is required.")
    if not conversation_id:
        raise ValueError("Chatwoot conversation_id is required.")
    return account_id, conversation_id


def _clean_thread_id(thread_id: str | None) -> str | None:
    value = str(thread_id or "").strip()
    return value or None


def _clean_status(status: str | None) -> str | None:
    value = str(status or "").strip().lower()
    return value or None


def _control_from_row(row: Any) -> ConversationControl | None:
    if row is None:
        return None
    return ConversationControl(
        account_id=str(row[0]),
        conversation_id=str(row[1]),
        active_thread_id=None if row[2] is None else str(row[2]),
        last_chatwoot_status=None if row[3] is None else str(row[3]),
        ai_enabled=bool(row[4]),
        handoff_required=bool(row[5]),
        handoff_reason=None if row[6] is None else str(row[6]),
        handoff_at=None if row[7] is None else row[7].isoformat(),
        assigned_team_id=None if row[8] is None else str(row[8]),
        assigned_agent_id=None if row[9] is None else str(row[9]),
        last_user_message_id=None if row[10] is None else str(row[10]),
        last_ai_message_id=None if row[11] is None else str(row[11]),
        last_handoff_message_id=(
            None if row[12] is None else str(row[12])
        ),
        handoff_conversation_updated=bool(row[13]),
        handoff_label_applied=bool(row[14]),
        handoff_note_created=bool(row[15]),
        handoff_assignment_applied=bool(row[16]),
        handoff_notice_sent=bool(row[17]),
        handoff_trigger_message_id=(
            None if len(row) <= 18 or row[18] is None else str(row[18])
        ),
    )


_REPOSITORY = ConversationControlRepository()


def get_conversation_control_repository() -> ConversationControlRepository:
    """返回进程级会话控制仓库。"""
    return _REPOSITORY


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
        account_id, contact_id = _validate_contact_reference(
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
        account_id, contact_id = _validate_contact_reference(
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


def _validate_contact_reference(
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


_CONTACT_BLOCK_REPOSITORY = ContactBlockRepository()


def get_contact_block_repository() -> ContactBlockRepository:
    """返回进程级联系人静音仓库。"""
    return _CONTACT_BLOCK_REPOSITORY
