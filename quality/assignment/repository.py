"""Deliver evidence before acknowledging the separate ICS source journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from psycopg.errors import UndefinedTable
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ics_agent.app.chatwoot.state import ASSIGNMENT_OUTBOX_TABLE
from .config import AssignmentSyncSettings


logger = logging.getLogger(__name__)
BATCH_SIZE = 20


def _positive_id(value) -> int:
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) <= 2**63-1:
        raise ValueError("Assignment evidence requires positive Chatwoot IDs")
    return int(text)


def _time(value) -> datetime:
    if not isinstance(value, (datetime, str)):
        raise ValueError("Assignment evidence requires a timestamp")
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Assignment evidence requires an explicit timestamp timezone")
    return result.astimezone(timezone.utc)


class AssignmentEvidenceInvalid(ValueError):
    """Source values cannot represent a valid assignment observation."""


class AssignmentEvidenceConflict(RuntimeError):
    """An immutable archive event already contains different evidence."""


@dataclass(frozen=True)
class AssignmentEvent:
    event_key: str
    account_id: int
    conversation_id: int
    source_time: datetime
    raw_body: bytes
    event_type: str = "ics_assignment_recorded"
    event_type: str = "ics_assignment_recorded"


def assignment_event(settings: AssignmentSyncSettings, row: dict) -> AssignmentEvent:
    account = _positive_id(row["account_id"])
    conversation = _positive_id(row["conversation_id"])
    if account not in settings.account_ids:
        raise ValueError("Assignment account is outside the configured archive source")
    business_round = str(row["business_round_id"])
    if not business_round.strip() or business_round != business_round.strip():
        raise ValueError("Assignment evidence requires the actual ICS business round")
    agent = str(_positive_id(row["agent_id"]))
    trigger = str(_positive_id(row["trigger_message_id"]))
    started = _time(row["session_started_at"])
    recorded = _time(row["recorded_at"])
    assigned = _time(row["assigned_at"]) if row["assigned_at"] is not None else None
    kind = row["evidence_kind"]
    if kind not in {"assignment_applied", "current_control_backfill"} or (kind == "assignment_applied") != (assigned is not None):
        raise ValueError("Assignment evidence must preserve unknown historical assignment times")
    source_time = assigned or recorded
    identity = json.dumps([settings.source_key, account, conversation, business_round], ensure_ascii=False, separators=(",", ":"))
    event_key = "ics_assignment:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    payload = {
        "event": "ics_assignment_recorded", "account": {"id": account},
        "conversation": {"id": conversation, "account_id": account},
        "updated_at": source_time.isoformat(),
        "assignment": {
            "schema_version": 1, "origin": "ics", "assignment_key": business_round,
            "business_round_id": business_round, "agent_id": agent,
            "trigger_message_id": trigger, "session_started_at": started.isoformat(),
            "assigned_at": assigned.isoformat() if assigned is not None else None,
            "recorded_at": recorded.isoformat(), "evidence_kind": kind,
            "assigned_at_unknown": assigned is None,
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return AssignmentEvent(event_key, account, conversation, source_time, raw)


class AssignmentSyncRepository:
    def __init__(self, settings: AssignmentSyncSettings, app_settings):
        self.settings = settings
        self.last_batch_blocked = 0
        self.last_batch_delivered = 0
        common = {
            "connect_timeout": 3, "application_name": "ics-quality-assignment-source",
            "options": "-c statement_timeout=2000 -c lock_timeout=1000", "row_factory": dict_row,
        }
        source = {"conninfo": app_settings.postgres_dsn, "kwargs": common} if app_settings.postgres_dsn else {
            "kwargs": {
                **common, "host": app_settings.postgres_host, "port": app_settings.postgres_port,
                "dbname": app_settings.postgres_database, "user": app_settings.postgres_user,
                "password": app_settings.postgres_password, "sslmode": app_settings.postgres_sslmode,
            },
        }
        limits = {"min_size": 0, "max_size": 1, "timeout": 1, "max_waiting": 1, "num_workers": 1, "open": False}
        self.source_pool = ConnectionPool(**source, **limits)
        try:
            self.target_pool = ConnectionPool(kwargs={**settings.connection_kwargs, "row_factory": dict_row}, **limits)
        except BaseException:
            self.source_pool.close()
            raise

    def open(self):
        self.source_pool.open(wait=False)
        self.target_pool.open(wait=False)
        with self.target_pool.connection() as connection:
            role = connection.execute("""
                SELECT r.rolsuper, pg_has_role(current_user, 'archive_owner', 'MEMBER') AS owner,
                    (has_table_privilege(current_user, 'archive.events', 'SELECT')
                        AND has_table_privilege(current_user, 'archive.events', 'INSERT')) AS evidence,
                    has_table_privilege(current_user, 'archive.events', 'UPDATE,DELETE,TRUNCATE') AS rewrites,
                    has_table_privilege(current_user, 'ops.event_jobs', 'INSERT') AS jobs,
                    has_table_privilege(current_user, 'ops.round_index', 'INSERT,UPDATE,DELETE,TRUNCATE') AS projection_writes
                FROM pg_roles r WHERE r.rolname=current_user
            """).fetchone()
            if not role or role["rolsuper"] or role["owner"] or role["rewrites"] or role["projection_writes"] or not role["evidence"] or not role["jobs"]:
                raise RuntimeError("Assignment sync requires the restricted archive assignment role")

    def close(self):
        try:
            self.target_pool.close()
        finally:
            self.source_pool.close()

    def pending(self) -> list[dict]:
        with self.source_pool.connection() as connection:
            return connection.execute(f"""
                SELECT account_id,conversation_id,business_round_id,agent_id,trigger_message_id,
                    session_started_at,assigned_at,recorded_at,evidence_kind
                FROM {ASSIGNMENT_OUTBOX_TABLE}
                WHERE delivered_at IS NULL AND delivery_error_code IS NULL AND account_id=ANY(%s)
                ORDER BY recorded_at,account_id,conversation_id,business_round_id LIMIT %s
            """, ([str(value) for value in sorted(self.settings.account_ids)], BATCH_SIZE)).fetchall()

    def append(self, row: dict) -> None:
        try:
            if "business_round_id" in row:
                event = assignment_event(self.settings, row)
            else:
                from .queue_evidence import queue_event
                event = queue_event(self.settings, row)
        except ValueError as error:
            raise AssignmentEvidenceInvalid("Invalid source assignment evidence") from error
        with self.target_pool.connection() as connection:
            inserted = connection.execute("""
                INSERT INTO archive.events
                    (source_key,account_id,event_key,event_type,ingestion_kind,conversation_id,
                     source_time,raw_body,payload)
                VALUES (%s,%s,%s,%s,'backfill',%s,%s,%s,convert_from(%s,'UTF8')::jsonb)
                ON CONFLICT (source_key,account_id,event_key) DO NOTHING RETURNING id
            """, (self.settings.source_key, event.account_id, event.event_key, event.event_type, event.conversation_id,
                  event.source_time, event.raw_body, event.raw_body)).fetchone()
            if inserted is None:
                inserted = connection.execute("""
                    SELECT id,raw_body FROM archive.events WHERE source_key=%s AND account_id=%s AND event_key=%s
                """, (self.settings.source_key, event.account_id, event.event_key)).fetchone()
                if inserted is None:
                    raise RuntimeError("Existing assignment evidence could not be read")
                if bytes(inserted["raw_body"]) != event.raw_body:
                    raise AssignmentEvidenceConflict("Assignment duplicate must preserve identical durable evidence")
            connection.execute("INSERT INTO ops.event_jobs (event_id) VALUES (%s) ON CONFLICT (event_id) DO NOTHING", (inserted["id"],))
        # Leaving the target connection commits both the evidence and the job.

    def acknowledge(self, row: dict) -> None:
        if "business_round_id" not in row:
            with self.source_pool.connection() as connection:
                connection.execute("""UPDATE ics_handoff_queue_event_outbox SET delivered_at=CURRENT_TIMESTAMP
                    WHERE account_id=%s AND conversation_id=%s AND trigger_message_id=%s AND delivered_at IS NULL""",
                    (row["account_id"],row["conversation_id"],row["trigger_message_id"]))
            return
        with self.source_pool.connection() as connection:
            connection.execute(f"""
                UPDATE {ASSIGNMENT_OUTBOX_TABLE} SET delivered_at=CURRENT_TIMESTAMP
                WHERE account_id=%s AND conversation_id=%s AND business_round_id=%s AND delivered_at IS NULL
            """, (row["account_id"], row["conversation_id"], row["business_round_id"]))

    def quarantine(self, row: dict, error_code: str) -> bool:
        if error_code not in {"evidence_conflict", "invalid_evidence"}:
            raise ValueError("Unsupported assignment isolation code")
        if "business_round_id" not in row:
            with self.source_pool.connection() as connection:
                result = connection.execute("""UPDATE ics_handoff_queue_event_outbox
                    SET delivery_error_code=%s,delivery_failed_at=CURRENT_TIMESTAMP
                    WHERE account_id=%s AND conversation_id=%s AND trigger_message_id=%s
                    AND delivered_at IS NULL AND delivery_error_code IS NULL""",
                    (error_code,row["account_id"],row["conversation_id"],row["trigger_message_id"]))
                return result.rowcount > 0
        with self.source_pool.connection() as connection:
            result = connection.execute(f"""
                UPDATE {ASSIGNMENT_OUTBOX_TABLE}
                SET delivery_error_code=%s, delivery_failed_at=CURRENT_TIMESTAMP
                WHERE account_id=%s AND conversation_id=%s AND business_round_id=%s
                    AND delivered_at IS NULL AND delivery_error_code IS NULL
            """, (error_code, row["account_id"], row["conversation_id"], row["business_round_id"]))
            changed = result.rowcount > 0
        return changed

    def queue_counts(self) -> dict[str, int]:
        """Sample source queue health; the runtime limits this query's frequency."""
        with self.source_pool.connection() as connection:
            row = connection.execute(f"""
                SELECT COUNT(*) FILTER (WHERE delivery_error_code IS NULL) AS pending_count,
                    COUNT(*) FILTER (WHERE delivery_error_code IS NOT NULL) AS blocked_count
                FROM {ASSIGNMENT_OUTBOX_TABLE}
                WHERE delivered_at IS NULL AND account_id=ANY(%s)
            """, ([str(value) for value in sorted(self.settings.account_ids)],)).fetchone()
            return {"pending_count": row["pending_count"], "blocked_count": row["blocked_count"]}

    def sync_once(self, stopping: threading.Event | None = None) -> int:
        self.last_batch_delivered = 0
        self.last_batch_blocked = 0
        self.last_batch_queue_delivered = 0
        self.last_batch_queue_blocked = 0
        assignments, queued = self.pending()[:BATCH_SIZE], self.pending_queue()[:BATCH_SIZE]
        rows = []
        groups = [assignments, queued]
        if getattr(self, "_queue_first", False):
            groups.reverse()
        self._queue_first = not getattr(self, "_queue_first", False)
        for index in range(max(len(assignments), len(queued))):
            for group in groups:
                if index < len(group):
                    rows.append(group[index])
        for row in rows[:BATCH_SIZE]:
            if stopping is not None and stopping.is_set():
                break
            try:
                self.append(row)
            except (AssignmentEvidenceConflict, AssignmentEvidenceInvalid) as error:
                code = "evidence_conflict" if isinstance(error, AssignmentEvidenceConflict) else "invalid_evidence"
                if self.quarantine(row, code):
                    self.last_batch_blocked += 1
                    if "business_round_id" not in row:
                        self.last_batch_queue_blocked += 1
                continue
            self.acknowledge(row)
            self.last_batch_delivered += 1
            if "business_round_id" not in row:
                self.last_batch_queue_delivered += 1
        return self.last_batch_delivered

    def pending_queue(self):
        try:
            with self.source_pool.connection() as connection:
                return connection.execute("""SELECT * FROM ics_handoff_queue_event_outbox
                    WHERE delivered_at IS NULL AND delivery_error_code IS NULL AND account_id=ANY(%s)
                    ORDER BY decided_at,account_id,conversation_id,trigger_message_id LIMIT %s""",
                    ([str(value) for value in sorted(self.settings.account_ids)],BATCH_SIZE)).fetchall()
        except UndefinedTable:
            logger.warning("archive.queue_evidence.source_table_unavailable")
            return []

    def queue_evidence_counts(self):
        try:
            with self.source_pool.connection() as connection:
                return connection.execute("""SELECT COUNT(*) FILTER(WHERE delivery_error_code IS NULL) AS pending_count,
                    COUNT(*) FILTER(WHERE delivery_error_code IS NOT NULL) AS blocked_count
                    FROM ics_handoff_queue_event_outbox WHERE delivered_at IS NULL AND account_id=ANY(%s)""",
                    ([str(value) for value in sorted(self.settings.account_ids)],)).fetchone()
        except UndefinedTable:
            return {"pending_count": None, "blocked_count": None}
