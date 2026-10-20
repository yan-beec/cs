"""Persistent leasing and atomic append/replay using a dedicated ingestion pool."""

from datetime import datetime
import hashlib
import logging
import uuid

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import ProcessingSettings
from .projector import project_conversation


logger = logging.getLogger(__name__)


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
                      has_table_privilege(current_user,'ops.round_index','SELECT,INSERT,UPDATE,DELETE') AS can_project
                    FROM pg_roles r WHERE r.rolname=current_user
                """).fetchone()
                if not row or row["rolsuper"] or row["owner_member"] or row["can_mutate"] or not row["can_append"] or not row["can_project"]:
                    raise RuntimeError("Processor requires restricted ingestion credentials and query migrations")
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
            event = connection.execute("SELECT * FROM archive.events WHERE id=%s", (claim["event_id"],)).fetchone()
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
                events = self._load_events(connection, scope)
                self._write_projection(connection, events)
                self._complete(connection, [item["id"] for item in events], claim)
        if contention:
            self._retry(claim, "Conversation is being processed by another worker", contention=True)

    def _load_events(self, connection, scope):
        budget = connection.execute("""
            SELECT count(*) AS n, COALESCE(sum(octet_length(payload::text)),0) AS bytes, max(id) AS high_water
            FROM archive.events WHERE source_key=%s AND account_id=%s AND conversation_id=%s
        """, scope).fetchone()
        if budget["n"] > self.settings.processor_max_conversation_events:
            raise RuntimeError("Conversation exceeds configured replay event bound")
        if budget["bytes"] > self.settings.processor_max_conversation_bytes:
            raise RuntimeError("Conversation exceeds configured replay byte bound")
        events = connection.execute("""
            SELECT id,source_key,account_id,conversation_id,message_id,event_type,ingestion_kind,
              source_time,received_at,payload FROM archive.events
            WHERE source_key=%s AND account_id=%s AND conversation_id=%s AND id<=%s
            ORDER BY id LIMIT %s
        """, (*scope, budget["high_water"], self.settings.processor_max_conversation_events + 1)).fetchall()
        if len(events) > self.settings.processor_max_conversation_events:
            raise RuntimeError("Conversation exceeds configured replay bound")
        return events

    def _complete(self, connection, event_ids, claim=None, reason=None, include_failed=False):
        # Other active leases are deliberately left untouched, even if replay
        # has materialized their evidence. Never complete an event not loaded.
        connection.execute("""
            UPDATE ops.event_jobs SET status='completed',locked_by=NULL,locked_until=NULL,
              completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,last_error=%s
            WHERE event_id=ANY(%s) AND (status IN ('queued','retry') OR (%s AND status='failed') OR
              (event_id=%s AND status='processing' AND locked_by=%s))
        """, (reason, event_ids, include_failed, claim["event_id"] if claim else None, claim["locked_by"] if claim else None))

    def rebuild(self, source_key, account_id=None, conversation_id=None):
        """Replay selected archived evidence, without updating or deleting it."""
        if not source_key:
            raise ValueError("An explicit source key is required for rebuild")
        with self.pool.connection() as connection:
            scopes = connection.execute("""
                SELECT DISTINCT source_key,account_id,conversation_id FROM archive.events
                WHERE source_key=%s AND (%s::bigint IS NULL OR account_id=%s)
                  AND (%s::bigint IS NULL OR conversation_id=%s) AND conversation_id IS NOT NULL
                ORDER BY account_id,conversation_id
            """, (source_key, account_id, account_id, conversation_id, conversation_id)).fetchall()
        count = 0
        for scope_row in scopes:
            scope = (scope_row["source_key"], scope_row["account_id"], scope_row["conversation_id"])
            with self.pool.connection() as connection:
                if not connection.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (scope_lock(*scope),)).fetchone()["locked"]:
                    continue
                events = self._load_events(connection, scope)
                self._write_projection(connection, events)
                self._complete(connection, [item["id"] for item in events], include_failed=True)
                count += 1
        return count

    def _write_projection(self, connection, events):
        projection = project_conversation(events, self.settings.idle_timeout_seconds, self.channel_bindings)
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
        for identity in projection.identities:
            self._upsert(connection, "ops.identity_map", identity, ("source_key", "account_id", "identity_kind", "identity_value"))
        # FK dependents are replaced inside one transaction. Readers observe
        # either the complete old projection or the complete new projection.
        connection.execute("DELETE FROM ops.message_index WHERE source_key=%s AND account_id=%s AND conversation_id=%s", scope)
        connection.execute("DELETE FROM ops.rating_index WHERE source_key=%s AND account_id=%s AND conversation_id=%s", scope)
        connection.execute("""DELETE FROM ops.round_agent_index a USING ops.round_index r
            WHERE a.round_id=r.round_id AND r.source_key=%s AND r.account_id=%s AND r.conversation_id=%s""", scope)
        connection.execute("DELETE FROM ops.round_index WHERE source_key=%s AND account_id=%s AND conversation_id=%s", scope)
        for round_row in projection.rounds:
            self._upsert(connection, "ops.round_index", round_row, ("round_id",))
        for message in projection.messages:
            row = {key: value for key, value in message.items() if key not in ("original_event_id", "latest_event_id", "creation_event_id")}
            row.update(original_snapshot_id=snapshot_ids[message["original_event_id"]], latest_snapshot_id=snapshot_ids[message["latest_event_id"]])
            row["data"].update(original_event_id=message["original_event_id"], latest_event_id=message["latest_event_id"])
            self._upsert(connection, "ops.message_index", row, ("source_key", "account_id", "message_id"))
        for agent in projection.agents:
            self._upsert(connection, "ops.round_agent_index", agent, ("round_id", "agent_id"))
        for rating in projection.ratings:
            row = {key: value for key, value in rating.items() if key in ("source_key", "account_id", "conversation_id", "rating_key", "round_id", "invitation_message_id", "external_round_id", "rating", "feedback", "status", "data")}
            row["snapshot_id"] = rating_ids[rating["event_id"]]
            row["data"].update(source_time=rating["source_time"], closed_at=rating["closed_at"])
            self._upsert(connection, "ops.rating_index", row, ("source_key", "account_id", "conversation_id", "rating_key"))
        self._upsert(connection, "ops.conversation_index", projection.conversation, ("source_key", "account_id", "conversation_id"))

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
