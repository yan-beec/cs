"""归档写入：先追加证据，提交后再在 ICS 源表标记已送达；重试写入同样的字节。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import threading

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ics_agent.app.blacklist.options import ACTION_LABELS, DURATIONS, LEGACY_REASONS, SOURCE_LABELS
from ics_agent.app.blacklist.repository import BlacklistRepository
from .config import BlacklistSyncSettings

BATCH_SIZE = 20


class BlacklistEvidenceInvalid(ValueError):
    pass


class BlacklistEvidenceConflict(RuntimeError):
    pass


def _iso(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BlacklistEvidenceInvalid("timestamp requires timezone")
    return value.astimezone(timezone.utc).isoformat()


def _id(value) -> int | None:
    if value in (None, ""):
        return None
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) <= 2**63 - 1:
        raise BlacklistEvidenceInvalid("invalid id")
    return int(text)


def archive_row(settings: BlacklistSyncSettings, row: dict) -> dict:
    account = _id(row["account_id"])
    if account not in settings.account_ids:
        raise BlacklistEvidenceInvalid("account outside archive source")
    if _id(row["contact_id"]) is None or row["action"] not in ACTION_LABELS or row["source"] not in SOURCE_LABELS:
        raise BlacklistEvidenceInvalid("invalid blacklist evidence")
    if row.get("duration_code") is not None and row["duration_code"] not in DURATIONS:
        raise BlacklistEvidenceInvalid("invalid duration")
    reason = row.get("reason_code")
    payload = {
        "schema_version": 1, "origin": "ics_blacklist", "event_id": str(row["event_id"]),
        "source_seq": int(row["id"]), "account_id": account, "contact_id": str(int(row["contact_id"])),
        "conversation_id": _id(row.get("conversation_id")), "contact_name": row.get("contact_name"),
        "contact_identifier": row.get("contact_identifier"), "action": row["action"], "source": row["source"],
        "duration_code": row.get("duration_code"), "blocked_until": _iso(row.get("blocked_until")),
        "permanent": bool(row.get("permanent")), "previous_blocked_until": _iso(row.get("previous_blocked_until")),
        "previous_permanent": row.get("previous_permanent"), "reason_code": reason,
        "reason_label": LEGACY_REASONS.get(reason) if reason else None, "remark": row.get("remark") or "",
        "operator_id": row.get("operator_id"), "operator_name": row.get("operator_name"),
        "occurred_at": _iso(row["occurred_at"]),
    }
    return {"event_key": "ics_blacklist:" + payload["event_id"], "account_id": account, "payload": payload}


class BlacklistSyncRepository:
    def __init__(self, settings: BlacklistSyncSettings, source: BlacklistRepository):
        self.settings = settings
        self.source = source
        self.last_batch_delivered = 0
        self.last_batch_blocked = 0
        self.target_pool = ConnectionPool(kwargs={**settings.connection_kwargs, "row_factory": dict_row},
                                          min_size=0, max_size=1, timeout=1, max_waiting=1, num_workers=1, open=False)

    def open(self):
        self.target_pool.open(wait=False)
        with self.target_pool.connection() as connection:
            role = connection.execute("""
                SELECT r.rolsuper, pg_has_role(current_user, 'archive_owner', 'MEMBER') AS owner,
                    to_regclass('archive.contact_block_events') IS NOT NULL AS migrated
                FROM pg_roles r WHERE r.rolname=current_user
            """).fetchone()
            if not role or role["rolsuper"] or role["owner"]:
                raise RuntimeError("Blacklist sync requires the restricted archive assignment role")
            if not role["migrated"]:
                raise RuntimeError("Archive migration 012_contact_blacklist is not applied")
            rights = connection.execute("""
                SELECT has_table_privilege(current_user, 'archive.contact_block_events', 'INSERT') AS writes,
                    has_table_privilege(current_user, 'archive.contact_block_events', 'UPDATE,DELETE,TRUNCATE') AS rewrites
            """).fetchone()
            if not rights["writes"] or rights["rewrites"]:
                raise RuntimeError("Blacklist sync requires append-only archive privileges")

    def close(self):
        self.target_pool.close()

    def append(self, row: dict) -> None:
        try:
            event = archive_row(self.settings, row)
        except (BlacklistEvidenceInvalid, KeyError, TypeError, ValueError) as error:
            raise BlacklistEvidenceInvalid("Invalid blacklist evidence") from error
        payload = event["payload"]
        with self.target_pool.connection() as connection:
            inserted = connection.execute("""
                INSERT INTO archive.contact_block_events (
                    source_key, account_id, event_key, source_seq, contact_id, conversation_id, contact_name,
                    contact_identifier, action, source, duration_code, blocked_until, permanent,
                    previous_blocked_until, previous_permanent, reason_code, reason_label, remark,
                    operator_id, operator_name, occurred_at, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_key, account_id, event_key) DO NOTHING RETURNING id
            """, (self.settings.source_key, event["account_id"], event["event_key"], payload["source_seq"],
                  payload["contact_id"], payload["conversation_id"], payload["contact_name"],
                  payload["contact_identifier"], payload["action"], payload["source"], payload["duration_code"],
                  payload["blocked_until"], payload["permanent"], payload["previous_blocked_until"],
                  payload["previous_permanent"], payload["reason_code"], payload["reason_label"],
                  payload["remark"], payload["operator_id"], payload["operator_name"], payload["occurred_at"],
                  Jsonb(payload))).fetchone()
            if inserted is None:
                existing = connection.execute("""
                    SELECT payload FROM archive.contact_block_events
                    WHERE source_key=%s AND account_id=%s AND event_key=%s
                """, (self.settings.source_key, event["account_id"], event["event_key"])).fetchone()
                if existing is None:
                    raise RuntimeError("Existing blacklist evidence could not be read")
                if json.dumps(existing["payload"], sort_keys=True) != json.dumps(payload, sort_keys=True):
                    raise BlacklistEvidenceConflict("Blacklist duplicate must preserve identical evidence")

    def sync_once(self, stopping: threading.Event | None = None) -> int:
        self.last_batch_delivered = 0
        self.last_batch_blocked = 0
        for row in self.source.pending_delivery(BATCH_SIZE):
            if stopping is not None and stopping.is_set():
                break
            try:
                self.append(row)
            except (BlacklistEvidenceInvalid, BlacklistEvidenceConflict) as error:
                code = "evidence_conflict" if isinstance(error, BlacklistEvidenceConflict) else "invalid_evidence"
                self.source.quarantine(row["id"], code)
                self.last_batch_blocked += 1
                continue
            self.source.acknowledge(row["id"])
            self.last_batch_delivered += 1
        return self.last_batch_delivered
