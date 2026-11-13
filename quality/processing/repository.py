"""Persistent leasing and atomic append/replay using a dedicated ingestion pool."""

from datetime import datetime
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import ProcessingSettings
from .projector import project_conversation
from .replay import RoundReplay
from . import rollups, service_facts


logger = logging.getLogger(__name__)


@dataclass
class RebuildReport:
    """逐会话记录已完成、锁忙及失败结果。"""
    completed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    @property
    def successful(self):
        return not self.skipped and not self.failed


def json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    return value


def scope_lock(source, account, conversation):
    raw = f"{len(source)}:{source}:{account}:{conversation}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


class ProcessingRepository:
    def __init__(self, settings: ProcessingSettings, channel_bindings=None):
        self.settings = settings
        self.channel_bindings = channel_bindings or {}
        self.replay = RoundReplay(self)
        self.facts_backfill = None
        self.facts_backfill_failures = 0
        self.rollups = None
        self.rollups_idle_until = 0.0
        self.pool = ConnectionPool(kwargs={**settings.connection_kwargs, "row_factory": dict_row},
            min_size=1, max_size=settings.processor_pool_size, timeout=2, open=False)

    def open(self):
        try:
            self.pool.open(wait=True, timeout=10)
            with self.pool.connection() as connection:
                row = connection.execute("""
                    SELECT r.rolsuper, pg_has_role(current_user,'archive_owner','MEMBER') AS owner_member,
                      has_table_privilege(current_user,'archive.events','UPDATE,DELETE,TRUNCATE') AS can_mutate,
                      has_table_privilege(current_user,'archive.message_snapshots','INSERT') AS can_append,
                      has_table_privilege(current_user,'ops.round_index','SELECT,INSERT,UPDATE,DELETE') AS can_project,
                      has_table_privilege(current_user,'ops.conversation_replay_state','SELECT,INSERT,UPDATE') AS can_resume
                    FROM pg_roles r WHERE r.rolname=current_user
                """).fetchone()
                if not row or row["rolsuper"] or row["owner_member"] or row["can_mutate"] or not row["can_append"] or not row["can_project"] or not row["can_resume"]:
                    raise RuntimeError("Processor requires restricted ingestion credentials and query migrations")
            self.facts_backfill = service_facts.FactBackfill(self.pool, scope_lock)
        except BaseException:
            self.pool.close()
            raise

    def close(self):
        self.pool.close()

    def claim(self, worker_id):
        token = worker_id[:100] + ":" + uuid.uuid4().hex
        with self.pool.connection() as connection:
            connection.execute("""
                WITH exhausted AS (
                    SELECT event_id FROM ops.event_jobs WHERE status='processing'
                    AND locked_until<CURRENT_TIMESTAMP AND attempts>=%s
                    FOR UPDATE SKIP LOCKED LIMIT 100
                )
                UPDATE ops.event_jobs j SET status='failed',locked_by=NULL,locked_until=NULL,
                    last_error='Worker lease repeatedly expired',updated_at=CURRENT_TIMESTAMP
                FROM exhausted e WHERE j.event_id=e.event_id
            """, (self.settings.processor_max_attempts,))
            row = connection.execute("""
                SELECT j.event_id FROM ops.event_jobs j
                WHERE ((j.status IN ('queued','retry') AND j.available_at<=CURRENT_TIMESTAMP)
                    OR (j.status='processing' AND j.locked_until<CURRENT_TIMESTAMP))
                ORDER BY j.available_at,j.event_id FOR UPDATE SKIP LOCKED LIMIT 1
            """).fetchone()
            if row is None:
                return None
            return connection.execute("""
                UPDATE ops.event_jobs SET status='processing',locked_by=%s,
                  locked_until=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),
                  attempts=attempts+1,updated_at=CURRENT_TIMESTAMP,completed_at=NULL
                WHERE event_id=%s RETURNING event_id,locked_by,attempts
            """, (token, self.settings.processor_lease_seconds, row["event_id"])).fetchone()

    def backfill_facts(self):
        """回填一批旧轮次的统计事实，连续失败时升级日志等级。"""
        if self.facts_backfill is None:
            self.facts_backfill = service_facts.FactBackfill(self.pool, scope_lock)
        try:
            advanced = self.facts_backfill.step()
            self.facts_backfill_failures = 0
            return advanced
        except Exception as error:
            self.facts_backfill_failures += 1
            log = logger.error if self.facts_backfill_failures >= 10 else logger.warning
            log("Archive service facts backfill failed: %s", type(error).__name__)
            return False

    def maintenance_step(self):
        """先推进事实回填，确认完成后才允许构建日汇总。"""
        if self.facts_backfill is None or not self.facts_backfill.done:
            return self.backfill_facts()
        return self.build_rollups()

    def build_rollups(self):
        """确认事实回填完成后构建一天；无任务时休息一分钟。"""
        if self.facts_backfill is None or not self.facts_backfill.done:
            return False
        if time.monotonic() < self.rollups_idle_until:
            return False
        if self.rollups is None:
            self.rollups = rollups.RollupBuilder(self.pool)
        try:
            built = self.rollups.step()
        except Exception as error:
            logger.warning("Archive statistics rollup failed: %s", type(error).__name__)
            built = False
        if not built:
            self.rollups_idle_until = time.monotonic() + 60
        return built

    def process_next(self, worker_id="quality-worker"):
        claim = self.claim(worker_id)
        if claim is None:
            return False
        try:
            self.process_claim(claim)
        except Exception as error:
            # DB exceptions can include customer content. Persist/log only the
            # exception class, never the SQL, row, URL, or raw request body.
            self._retry(claim, type(error).__name__)
            logger.warning("Archive projection failed event_id=%s error=%s", claim["event_id"], type(error).__name__)
        return True

    def _retry(self, claim, reason, contention=False):
        attempts = claim["attempts"]
        status = "retry" if contention or attempts < self.settings.processor_max_attempts else "failed"
        delay = 1 if contention else min(300, 2 ** min(attempts, 8))
        with self.pool.connection() as connection:
            connection.execute("""
                UPDATE ops.event_jobs SET status=%s,available_at=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),
                  locked_by=NULL,locked_until=NULL,last_error=%s,updated_at=CURRENT_TIMESTAMP,
                  attempts=attempts-%s
                WHERE event_id=%s AND status='processing' AND locked_by=%s
            """, (status, delay, reason, 1 if contention else 0, claim["event_id"], claim["locked_by"]))

    def process_claim(self, claim):
        contention = False
        with self.pool.connection() as connection:
            event = connection.execute("""SELECT id,source_key,account_id,conversation_id
                FROM archive.events WHERE id=%s""", (claim["event_id"],)).fetchone()
            if event is None:
                raise RuntimeError("Claim has no evidence")
            scope = (event["source_key"], event["account_id"], event["conversation_id"])
            locked = connection.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (scope_lock(*scope),)).fetchone()["locked"]
            if not locked:
                contention = True
            else:
                owned = connection.execute("""
                    SELECT event_id FROM ops.event_jobs WHERE event_id=%s AND status='processing'
                    AND locked_by=%s AND locked_until>CURRENT_TIMESTAMP FOR UPDATE
                """, (claim["event_id"], claim["locked_by"])).fetchone()
                if owned is None:
                    return
                if event["conversation_id"] is None:
                    self._complete(connection, [event["id"]], claim, "Unindexed: missing conversation ID")
                    return
                self.replay.step(connection, scope, claim)
                # A resumed plan may finish before this newly claimed event's
                # generation. Release any still-owned job for the next step.
                connection.execute("""UPDATE ops.event_jobs SET status='retry',available_at=CURRENT_TIMESTAMP,
                    locked_by=NULL,locked_until=NULL,attempts=GREATEST(0,attempts-1),last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE event_id=%s AND status='processing' AND locked_by=%s
                """, (claim["event_id"], claim["locked_by"]))
        if contention:
            self._retry(claim, "Conversation is being processed by another worker", contention=True)

    def _complete(self, connection, event_ids, claim=None, reason=None, include_failed=False):
        # Other active leases are deliberately left untouched, even if replay
        # has materialized their evidence. Never complete an event not loaded.
        connection.execute("""
            UPDATE ops.event_jobs SET status='completed',locked_by=NULL,locked_until=NULL,
              completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,last_error=%s
            WHERE event_id=ANY(%s) AND (status IN ('queued','retry') OR (%s AND status='failed') OR
              (event_id=%s AND status='processing' AND locked_by=%s))
        """, (reason, event_ids, include_failed, claim["event_id"] if claim else None, claim["locked_by"] if claim else None))

    def rebuild(self, source_key, account_id=None, conversation_id=None, *, stopped=None):
        """逐会话重放不可变证据，保留每个事务窗口的完成边界。"""
        if not source_key:
            raise ValueError("An explicit source key is required for rebuild")
        with self.pool.connection() as connection:
            scopes = connection.execute("""
                SELECT DISTINCT source_key,account_id,conversation_id FROM archive.events
                WHERE source_key=%s AND (%s::bigint IS NULL OR account_id=%s)
                  AND (%s::bigint IS NULL OR conversation_id=%s) AND conversation_id IS NOT NULL
                ORDER BY account_id,conversation_id
            """, (source_key, account_id, account_id, conversation_id, conversation_id)).fetchall()
        report = RebuildReport()
        if not scopes and account_id is not None and conversation_id is not None:
            report.failed.append(dict(source_key=source_key, account_id=account_id, conversation_id=conversation_id,
                status="failed", error_code="NO_EVENTS", error_message="No archived events"))
        for scope_row in scopes:
            scope = (scope_row["source_key"], scope_row["account_id"], scope_row["conversation_id"])
            result = {**scope_row, "error_code": "", "error_message": ""}
            first = True
            try:
                while True:
                    if stopped is not None and stopped.is_set():
                        raise KeyboardInterrupt
                    with self.pool.connection() as connection:
                        if not connection.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (scope_lock(*scope),)).fetchone()["locked"]:
                            result.update(status="skipped_busy", error_code="BUSY")
                            report.skipped.append(result)
                            break
                        done = self.replay.step(connection, scope, rebuild=first)
                    if stopped is not None and stopped.is_set():
                        raise KeyboardInterrupt
                    first = False
                    if done:
                        result["status"] = "completed"
                        report.completed.append(result)
                        break
            except Exception as error:
                from psycopg import OperationalError, InterfaceError
                from psycopg_pool import PoolTimeout
                if isinstance(error, (InterfaceError, PoolTimeout)) or (isinstance(error, OperationalError)
                        and (not error.sqlstate or error.sqlstate.startswith("08") or error.sqlstate in {"57P01", "57P02", "57P03"})):
                    raise
                result.update(status="failed", error_code=type(error).__name__, error_message=type(error).__name__)
                report.failed.append(result)
        return report

    def _write_projection(self, connection, events, *, replay_seed=None, round_no_offset=0,
                          following_started_at=None, window=(None, None), rating_context=None, assignment_context=None):
        projection = project_conversation(events, self.settings.idle_timeout_seconds, self.channel_bindings,
            replay_seed=replay_seed, round_no_offset=round_no_offset, following_started_at=following_started_at,
            rating_context=rating_context, assignment_context=assignment_context)
        scope = (events[0]["source_key"], events[0]["account_id"], events[0]["conversation_id"])
        snapshot_ids = {}
        for snapshot in projection.snapshots:
            evidence = {key: value for key, value in snapshot.items() if key not in ("attachments", "is_rating")}
            snapshot_id = self._append(connection, "archive.message_snapshots", evidence, ("event_id", "message_id"))
            snapshot_ids[snapshot["event_id"]] = snapshot_id
            for attachment in snapshot["attachments"]:
                attachment_id = self._append(connection, "archive.attachments", {"snapshot_id": snapshot_id, **attachment}, ("snapshot_id", "attachment_key"))
                connection.execute("INSERT INTO ops.attachment_jobs (attachment_id) VALUES (%s) ON CONFLICT DO NOTHING", (attachment_id,))
        for snapshot in projection.conversation_snapshots:
            self._append(connection, "archive.conversation_snapshots", snapshot, ("event_id", "conversation_id"))
        rating_ids = {}
        for snapshot in projection.rating_snapshots:
            rating_ids[snapshot["event_id"]] = self._append(connection, "archive.rating_snapshots", snapshot, ("event_id",))
        self._customer(connection, projection.customer)
        # A previously unidentified conversation can acquire its authoritative
        # contact later. Reparent retained query rows without loading old bodies.
        connection.execute("""UPDATE ops.message_index SET customer_id=%s
            WHERE source_key=%s AND account_id=%s AND conversation_id=%s AND customer_id<>%s""",
            (projection.customer["customer_id"], *scope, projection.customer["customer_id"]))
        connection.execute("""UPDATE ops.round_index SET customer_id=%s
            WHERE source_key=%s AND account_id=%s AND conversation_id=%s AND customer_id<>%s""",
            (projection.customer["customer_id"], *scope, projection.customer["customer_id"]))
        for identity in projection.identities:
            self._upsert(connection, "ops.identity_map", identity, ("source_key", "account_id", "identity_kind", "identity_value"))
        # Only this replay window is replaced. Frozen historical rows and their
        # snapshots remain queryable as the recent-round window advances.
        connection.execute("""DELETE FROM ops.round_agent_index a USING ops.round_index r
            WHERE a.round_id=r.round_id AND r.source_key=%s AND r.account_id=%s AND r.conversation_id=%s
              AND (%s::timestamptz IS NULL OR r.started_at>=%s)
              AND (%s::timestamptz IS NULL OR r.started_at<%s)""", (*scope, window[0], window[0], window[1], window[1]))
        for round_row in projection.rounds:
            self._upsert(connection, "ops.round_index", round_row, ("round_id",))
        for message in projection.messages:
            row = {key: value for key, value in message.items() if key not in ("original_event_id", "latest_event_id", "creation_event_id")}
            row.update(original_snapshot_id=snapshot_ids[message["original_event_id"]], latest_snapshot_id=snapshot_ids[message["latest_event_id"]])
            row["data"].update(original_event_id=message["original_event_id"], latest_event_id=message["latest_event_id"])
            if row["data"].get("is_rating") and row["round_id"] is None:
                linked = connection.execute("""SELECT round_id FROM ops.rating_index
                    WHERE source_key=%s AND account_id=%s AND conversation_id=%s
                      AND invitation_message_id=%s AND round_id IS NOT NULL LIMIT 1""", (*scope, row["message_id"])).fetchone()
                if linked:
                    row["round_id"] = linked["round_id"]
            self._upsert(connection, "ops.message_index", row, ("source_key", "account_id", "message_id"))
        for agent in projection.agents:
            self._upsert(connection, "ops.round_agent_index", agent, ("round_id", "agent_id"))
        # Statistics facts follow the rows just written, in the same transaction,
        # so a report can never combine new rounds with stale timings.
        service_facts.store(connection, [round_row["round_id"] for round_row in projection.rounds])
        for rating in projection.ratings:
            row = {key: value for key, value in rating.items() if key in ("source_key", "account_id", "conversation_id", "rating_key", "round_id", "invitation_message_id", "external_round_id", "rating", "feedback", "status", "data")}
            row["snapshot_id"] = rating_ids[rating["event_id"]]
            row["data"].update(source_time=rating["source_time"], closed_at=rating["closed_at"])
            self._upsert(connection, "ops.rating_index", row, ("source_key", "account_id", "conversation_id", "rating_key"))
        if window[1] is None:
            self._upsert(connection, "ops.conversation_index", projection.conversation, ("source_key", "account_id", "conversation_id"))
        return projection

    def _customer(self, connection, customer):
        # Several conversations may belong to one contact. Serialize this small
        # row update and preserve the global first/last times and newer name.
        connection.execute("SELECT pg_advisory_xact_lock(%s)",
            (scope_lock(customer["source_key"], customer["account_id"], customer["customer_id"]),))
        old = connection.execute("SELECT * FROM ops.customer_index WHERE customer_id=%s FOR UPDATE", (customer["customer_id"],)).fetchone()
        if old:
            old_at, new_at = old["last_message_at"], customer["last_message_at"]
            if old_at and (not new_at or old_at > new_at):
                customer["last_message_at"] = old_at
            if (old["data"].get("identity_observed_at") or "") > (customer["data"].get("identity_observed_at") or ""):
                customer["display_name"] = old["display_name"] or customer["display_name"]
                customer["data"]["identity_observed_at"] = old["data"]["identity_observed_at"]
            starts = [value for value in (old["data"].get("first_message_at"), customer["data"].get("first_message_at")) if value]
            customer["data"]["first_message_at"] = min(starts) if starts else None
            if customer["business_user_id"] is None and old["business_user_id"]:
                customer["business_user_id"] = old["business_user_id"]
                customer["data"]["identity_status"] = old["data"].get("identity_status", "verified")
            for key in ("h5_user_id", "chatwoot_user_id", "phone_number", "contact_note"):
                if not customer["data"].get(key) and old["data"].get(key):
                    customer["data"][key] = old["data"][key]
        self._upsert(connection, "ops.customer_index", customer, ("customer_id",))

    @staticmethod
    def _values(row):
        return [Jsonb(json_safe(value)) if isinstance(value, (dict, list)) else value for value in row.values()]

    def _append(self, connection, table, row, conflict):
        columns = list(row)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING RETURNING id").format(
            sql.Identifier(*table.split(".")), sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns), sql.SQL(",").join(map(sql.Identifier, conflict)))
        result = connection.execute(query, self._values(row)).fetchone()
        if result:
            return result["id"]
        where = sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in conflict)
        return connection.execute(sql.SQL("SELECT id FROM {} WHERE {}").format(sql.Identifier(*table.split(".")), where), tuple(row[key] for key in conflict)).fetchone()["id"]

    def _upsert(self, connection, table, row, conflict):
        columns = list(row)
        updates = [key for key in columns if key not in conflict]
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
            sql.Identifier(*table.split(".")), sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns), sql.SQL(",").join(map(sql.Identifier, conflict)),
            sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(key), sql.Identifier(key)) for key in updates))
        connection.execute(query, self._values(row))
