"""Bounded archive queries and restricted review saves, separate from the ICS pool."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
import uuid

from psycopg.errors import SerializationFailure
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import QuerySettings
from .responsibility import normalize_responsibility
from .service_metrics import RoundServiceMetrics


OVERVIEW_AGENT_LIMIT = 200


class ReviewConflict(RuntimeError):
    """A newer review was saved after the browser loaded this round."""


@dataclass(frozen=True)
class ArchiveFilters:
    source_key: str | None = None
    account_id: int | None = None
    channel_key: str | None = None
    inbox_id: int | None = None
    customer_query: str | None = None
    conversation_id: int | None = None
    round_no: int | None = None
    reception_type: str | None = None
    agent_id: str | None = None
    agent_scope: str = "responsible"
    time_field: str = "started_at"
    start: datetime | None = None
    end: datetime | None = None
    rating_status: str | None = None
    rating: int | None = None
    keyword: str | None = None
    message_scope: str = "all"
    has_attachment: bool | None = None
    status: str | None = None
    ai_review_status: str | None = None
    human_review_status: str | None = None


def _literal_pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _require_scoped_ids(filters: ArchiveFilters) -> None:
    # Chatwoot seat and inbox IDs are only unique within one source/account.
    if (filters.agent_id or filters.inbox_id is not None) and (
        not filters.source_key or filters.account_id is None
    ):
        raise ValueError("Seat and inbox filters require source_key and account_id")


def _message_conditions(
    filters: ArchiveFilters, relation: list[str], relation_values: list | None = None, *, require_message: bool = False,
) -> tuple[list[str], list]:
    """Apply message criteria to one logical message, including negative attachment searches."""
    eligible = list(relation)
    values: list[Any] = list(relation_values or [])
    if filters.keyword:
        # Search observed versions too, so an edited/deleted original remains findable.
        eligible.append("EXISTS (SELECT 1 FROM archive.message_snapshots s WHERE s.source_key=m.source_key AND s.account_id=m.account_id AND s.message_id=m.message_id AND s.content ILIKE %s)")
        values.append(_literal_pattern(filters.keyword))
    if filters.message_scope != "all":
        if filters.message_scope == "private":
            eligible.append("m.is_private")
        else:
            eligible.append("m.sender_role=%s AND NOT m.is_private")
            values.append(filters.message_scope)
    if filters.time_field == "message_at":
        if filters.start:
            eligible.append("m.sent_at >= %s")
            values.append(filters.start)
        if filters.end:
            eligible.append("m.sent_at < %s")
            values.append(filters.end)
    attachment = "jsonb_array_length(COALESCE(m.data -> 'attachments', '[]'::jsonb)) > 0"
    predicate = " AND ".join(eligible)
    clauses: list[str] = []
    if filters.has_attachment is True:
        clauses.append(f"EXISTS (SELECT 1 FROM ops.message_index m WHERE {predicate} AND {attachment})")
    else:
        if require_message or len(eligible) > len(relation):
            clauses.append(f"EXISTS (SELECT 1 FROM ops.message_index m WHERE {predicate})")
        if filters.has_attachment is False:
            clauses.append(f"NOT EXISTS (SELECT 1 FROM ops.message_index m WHERE {predicate} AND {attachment})")
            if require_message or len(eligible) > len(relation):
                values.extend(values.copy())
    return clauses, values


def round_conditions(filters: ArchiveFilters) -> tuple[str, list]:
    """Only SQL fragments from closed dictionaries are interpolated."""
    _require_scoped_ids(filters)
    clauses: list[str] = []
    values: list[Any] = []
    for name in ("source_key", "account_id", "inbox_id", "conversation_id", "round_no", "rating_status", "rating", "status"):
        value = getattr(filters, name)
        if value is not None:
            clauses.append(f"r.{name} = %s")
            values.append(value)
    if filters.reception_type:
        clauses.append("r.reception_type = %s")
        values.append(filters.reception_type)
    review_terms = []
    review_values = []
    for field, verdict in (("ai_status", filters.ai_review_status), ("human_status", filters.human_review_status)):
        if verdict is not None:
            review_terms.append(f"review.{field} = %s")
            review_values.append(verdict)
    if review_terms:
        # Match both verdicts on the same round's latest saved revision.
        review_match = ("EXISTS (SELECT 1 FROM quality.current_round_reviews review "
                        "WHERE review.round_id=r.round_id AND " + " AND ".join(review_terms) + ")")
        if all(verdict == "pending" for verdict in review_values):
            # Unsaved rounds display pending in the editor and exports too.
            review_match = (f"({review_match} OR NOT EXISTS (SELECT 1 FROM quality.current_round_reviews review "
                            "WHERE review.round_id=r.round_id))")
        clauses.append(review_match)
        values.extend(review_values)
    if filters.channel_key:
        clauses.append("r.channel_key IS NULL" if filters.channel_key == "unmapped" else "r.channel_key = %s")
        if filters.channel_key != "unmapped":
            values.append(filters.channel_key)
    if filters.customer_query:
        clauses.append("(c.display_name ILIKE %s OR c.contact_id ILIKE %s OR c.business_user_id ILIKE %s OR c.customer_id = %s)")
        pattern = _literal_pattern(filters.customer_query)
        values.extend([pattern, pattern, pattern, filters.customer_query])
    if filters.agent_id:
        if filters.agent_scope == "responsible":
            clauses.append("r.data ->> 'responsibility_basis' = 'ics_assignment'")
            clauses.append("r.data ->> 'responsible_agent_id' = %s")
        elif filters.agent_scope == "current":
            clauses.append("EXISTS (SELECT 1 FROM ops.conversation_index ci WHERE ci.source_key=r.source_key AND ci.account_id=r.account_id AND ci.conversation_id=r.conversation_id AND ci.current_assignee_id=%s)")
        else:
            predicate = {"replied": "a.replied", "assigned": "a.was_assigned"}[filters.agent_scope]
            clauses.append(f"EXISTS (SELECT 1 FROM ops.round_agent_index a WHERE a.round_id=r.round_id AND a.agent_id=%s AND {predicate})")
        values.append(filters.agent_id)
    time_expression = {
        "started_at": "r.started_at", "resolved_at": "NULLIF(COALESCE(r.data ->> 'resolved_at', r.data ->> 'closed_at'), '')::timestamptz",
        "rating_at": "NULLIF(r.data ->> 'rating_at', '')::timestamptz",
    }
    if filters.time_field != "message_at":
        expression = time_expression[filters.time_field]
        if filters.start:
            clauses.append(f"{expression} >= %s")
            values.append(filters.start)
        if filters.end:
            clauses.append(f"{expression} < %s")
            values.append(filters.end)
    message_clauses, message_values = _message_conditions(filters, ["m.round_id=r.round_id"])
    clauses.extend(message_clauses)
    values.extend(message_values)
    return " AND ".join(clauses) or "TRUE", values


def customer_conditions(filters: ArchiveFilters) -> tuple[str, list]:
    """Keep identity searches available for messages whose round is still unknown."""
    _require_scoped_ids(filters)
    clauses = ["EXISTS (SELECT 1 FROM ops.message_index m WHERE m.customer_id=c.customer_id)"]
    values: list[Any] = []
    for name in ("source_key", "account_id"):
        value = getattr(filters, name)
        if value is not None:
            clauses.append(f"c.{name} = %s")
            values.append(value)
    if filters.customer_query:
        pattern = _literal_pattern(filters.customer_query)
        clauses.append("(c.display_name ILIKE %s OR c.contact_id ILIKE %s OR c.business_user_id ILIKE %s OR c.customer_id = %s)")
        values.extend([pattern, pattern, pattern, filters.customer_query])
    round_specific = any(value is not None for value in (
        filters.round_no, filters.reception_type, filters.agent_id if filters.agent_scope != "current" else None, filters.rating_status,
        filters.rating, filters.status, filters.ai_review_status, filters.human_review_status,
    )) or (filters.time_field != "message_at" and bool(filters.start or filters.end))
    if round_specific:
        where, round_values = round_conditions(replace(filters, source_key=None, account_id=None, customer_query=None)
                                               if not filters.agent_id and filters.inbox_id is None else replace(filters, customer_query=None))
        clauses.append(f"EXISTS (SELECT 1 FROM ops.round_index r WHERE r.customer_id=c.customer_id AND {where})")
        values.extend(round_values)
    else:
        relation = ["m.customer_id=c.customer_id"]
        relation_values: list[Any] = []
        if filters.conversation_id is not None:
            relation.append("m.conversation_id=%s")
            relation_values.append(filters.conversation_id)
        conversation_scope = "ci.source_key=m.source_key AND ci.account_id=m.account_id AND ci.conversation_id=m.conversation_id"
        if filters.agent_id:
            relation.append(f"EXISTS (SELECT 1 FROM ops.conversation_index ci WHERE {conversation_scope} AND ci.current_assignee_id=%s)")
            relation_values.append(filters.agent_id)
        if filters.inbox_id is not None:
            relation.append(f"COALESCE(m.data ->> 'inbox_id', (SELECT ci.inbox_id::text FROM ops.conversation_index ci WHERE {conversation_scope})) = %s")
            relation_values.append(str(filters.inbox_id))
        if filters.channel_key:
            expression = f"COALESCE(m.data ->> 'channel_key', (SELECT ci.channel_key FROM ops.conversation_index ci WHERE {conversation_scope}))"
            relation.append(f"{expression} IS NULL" if filters.channel_key == "unmapped" else f"{expression} = %s")
            if filters.channel_key != "unmapped":
                relation_values.append(filters.channel_key)
        message_clauses, message_values = _message_conditions(filters, relation, relation_values, require_message=True)
        clauses.extend(message_clauses)
        values.extend(message_values)
    return " AND ".join(clauses), values


def _flatten(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row.get("data") or {})
    result.update({key: value for key, value in row.items() if key != "data"})
    if "round_id" in row and "reception_type" in row:
        result = normalize_responsibility(result)
    return result


def _contact_profile(value):
    """Extract the stable Chatwoot contact fields from an archived payload."""
    if not isinstance(value, dict):
        return {}
    additional = value.get("additional_attributes") if isinstance(value.get("additional_attributes"), dict) else {}
    custom = value.get("custom_attributes") if isinstance(value.get("custom_attributes"), dict) else {}
    phone = next((value.get(key) for source in (value, additional, custom)
                  for key in ("phone_number", "phone")
                  if isinstance(source, dict) and source.get(key) not in (None, "")), None)
    note = next((source.get(key) for source in (value, additional, custom)
                 for key in ("description", "note", "notes", "contact_note", "contact_notes")
                 if isinstance(source, dict) and isinstance(source.get(key), str) and source.get(key).strip()), None)
    return {"chatwoot_user_id": str(value.get("id") or value.get("user_id")) if value.get("id") or value.get("user_id") else None,
            "phone_number": str(phone) if phone is not None else None,
            "contact_note": note}


def _snapshot_contact(data):
    if not isinstance(data, dict):
        return {}
    candidates = [data.get("contact"), data.get("sender"),
                  (data.get("meta") or {}).get("sender") if isinstance(data.get("meta"), dict) else None]
    for candidate in candidates:
        profile = _contact_profile(candidate)
        if any(profile.values()):
            return profile
    return {}


class QueryRepository:
    def __init__(self, settings: QuerySettings):
        self.pool = ConnectionPool(
            kwargs={**settings.connection_kwargs, "row_factory": dict_row}, min_size=0,
            max_size=settings.query_pool_size, timeout=1, open=False,
        )

    def open(self):
        self.pool.open(wait=False)
        self.ping()

    def close(self):
        self.pool.close()

    def ping(self):
        with self.pool.connection() as connection:
            row = connection.execute("""
                SELECT r.rolsuper, pg_has_role(current_user, 'archive_owner', 'MEMBER') AS owner,
                    has_table_privilege(current_user, 'archive.events', 'INSERT,UPDATE,DELETE,TRUNCATE') AS writes,
                    has_table_privilege(current_user, 'ops.round_index', 'SELECT') AS reads,
                    has_table_privilege(current_user, 'ops.round_index', 'INSERT,UPDATE,DELETE,TRUNCATE') AS projection_writes,
                    has_table_privilege(current_user, 'quality.current_round_reviews', 'SELECT') AS review_reads,
                    has_function_privilege(current_user, 'quality.save_round_review(text,text,text,text,text,bigint)', 'EXECUTE') AS review_saves
                FROM pg_roles r WHERE r.rolname=current_user
            """).fetchone()
            if (not row or row["rolsuper"] or row["owner"] or row["writes"] or row["projection_writes"]
                    or not row["reads"] or not row["review_reads"] or not row["review_saves"]):
                raise RuntimeError("Archive queries require the restricted reader role and migrations through 009_dual_round_reviews")

    @staticmethod
    def _agents(connection, items):
        if not items:
            return items
        agents = connection.execute("SELECT * FROM ops.round_agent_index WHERE round_id=ANY(%s) ORDER BY agent_name,agent_id", ([item["round_id"] for item in items],)).fetchall()
        grouped: dict[str, list] = {}
        for agent in agents:
            grouped.setdefault(agent["round_id"], []).append(dict(agent))
        for item in items:
            item["agents"] = grouped.get(item["round_id"], [])
        return items

    def list_rounds(self, filters: ArchiveFilters, page: int = 1, page_size: int = 30):
        where, values = round_conditions(filters)
        join = "FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id"
        with self.pool.connection() as connection:
            total = connection.execute(f"SELECT count(*) AS total {join} WHERE {where}", values).fetchone()["total"]
            rows = connection.execute(f"""
                SELECT r.*, c.display_name AS customer_name, c.contact_id, c.business_user_id,
                    to_jsonb(review) AS review
                {join} LEFT JOIN quality.current_round_reviews review ON review.round_id=r.round_id
                WHERE {where} ORDER BY r.started_at DESC,r.round_id DESC LIMIT %s OFFSET %s
            """, [*values, page_size, (page - 1) * page_size]).fetchall()
            items = self._agents(connection, [_flatten(row) for row in rows])
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def overview(self, filters: ArchiveFilters):
        """Aggregate only matched rounds, without expanding customers' full history."""
        where, values = round_conditions(filters)
        # One bounded statement computes totals and the limited seat list from
        # one snapshot. No chat bodies or unbounded result sets leave PostgreSQL.
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '3000ms'")
                result = dict(connection.execute(f"""
                    WITH selected AS MATERIALIZED (
                        SELECT r.round_id,r.source_key,r.account_id,r.customer_id,
                            r.reception_type,r.status,r.rating_status,r.rating,r.data
                        FROM ops.round_index r
                        LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id
                        WHERE {where}
                    ), participation AS (
                        SELECT r.source_key,r.account_id,r.round_id,
                            r.data->>'responsible_agent_id' AS agent_id,
                            r.data->>'responsible_agent_name' AS agent_name,
                            TRUE AS responsible,FALSE AS replied,
                            r.rating_status='rated' AND r.rating<4 AS low_rating
                        FROM selected r
                        WHERE r.data->>'responsibility_basis'='ics_assignment'
                            AND NULLIF(r.data->>'responsible_agent_id','') IS NOT NULL
                        UNION ALL
                        SELECT r.source_key,r.account_id,r.round_id,a.agent_id,a.agent_name,
                            FALSE,TRUE,FALSE
                        FROM selected r JOIN ops.round_agent_index a ON a.round_id=r.round_id
                        WHERE a.replied AND NULLIF(a.agent_id,'') IS NOT NULL
                    ), per_round_agent AS (
                        SELECT source_key,account_id,round_id,agent_id,
                            max(NULLIF(agent_name,'')) AS agent_name,
                            bool_or(responsible) AS responsible,bool_or(replied) AS replied,
                            bool_or(low_rating) AS low_rating
                        FROM participation GROUP BY source_key,account_id,round_id,agent_id
                    ), agent_totals AS (
                        SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,
                            count(*) FILTER (WHERE responsible) AS responsible_rounds,
                            count(*) FILTER (WHERE replied) AS replied_rounds,
                            count(*) FILTER (WHERE low_rating) AS low_rating_rounds
                        FROM per_round_agent GROUP BY source_key,account_id,agent_id
                    )
                    SELECT count(*) AS total_rounds,count(DISTINCT customer_id) AS total_customers,
                        count(*) FILTER (WHERE reception_type='ai_only') AS ai_only_rounds,
                        count(*) FILTER (WHERE reception_type='human') AS human_rounds,
                        count(*) FILTER (WHERE status='active') AS active_rounds,
                        count(*) FILTER (WHERE status='resolved') AS resolved_rounds,
                        count(*) FILTER (WHERE rating_status='rated') AS rated_rounds,
                        count(*) FILTER (WHERE rating_status='rated' AND rating<4) AS low_rating_rounds,
                        (avg(rating) FILTER (WHERE rating_status='rated'))::double precision AS average_rating,
                        count(*) FILTER (WHERE reception_type='human'
                            AND data #>> '{{counts,agent}}' = '0') AS human_without_public_reply_rounds,
                        count(*) FILTER (WHERE reception_type='human'
                            AND data->>'responsibility_basis' IS DISTINCT FROM 'ics_assignment')
                            AS responsibility_unrecorded_rounds,
                        (SELECT count(*) FROM agent_totals) AS participating_agent_count,
                        COALESCE((SELECT jsonb_agg(to_jsonb(limited)
                            ORDER BY responsible_rounds DESC,replied_rounds DESC,source_key,account_id,agent_id)
                            FROM (SELECT * FROM agent_totals
                                ORDER BY responsible_rounds DESC,replied_rounds DESC,source_key,account_id,agent_id
                                LIMIT {OVERVIEW_AGENT_LIMIT}) limited),'[]'::jsonb) AS participating_agents,
                        CURRENT_TIMESTAMP AS generated_at
                    FROM selected
                """, values).fetchone())
        result["agents_truncated"] = result["participating_agent_count"] > OVERVIEW_AGENT_LIMIT
        result["generated_at"] = result["generated_at"].isoformat()
        return result

    def list_customers(self, filters: ArchiveFilters, page: int = 1, page_size: int = 30):
        condition, values = customer_conditions(filters)
        with self.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            total = connection.execute(f"SELECT count(*) AS total FROM ops.customer_index c WHERE {condition}", values).fetchone()["total"]
            rows = connection.execute(f"""
                SELECT c.*, (SELECT count(*) FROM ops.round_index r WHERE r.customer_id=c.customer_id) AS round_count,
                    (SELECT count(*) FROM ops.message_index m WHERE m.customer_id=c.customer_id) AS message_count
                FROM ops.customer_index c WHERE {condition}
                ORDER BY c.last_message_at DESC NULLS LAST,c.customer_id LIMIT %s OFFSET %s
            """, [*values, page_size, (page - 1) * page_size]).fetchall()
            items = [_flatten(row) for row in rows]
            self._customer_service_metrics(connection, items)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def _customer_service_metrics(connection, customers):
        """Read only the latest round for customers on this page, in two batches."""
        if not customers:
            return
        rows = connection.execute("""
            SELECT latest.*,to_jsonb(review) AS review FROM (
                SELECT DISTINCT ON (r.customer_id) r.* FROM ops.round_index r
                WHERE r.customer_id=ANY(%s)
                ORDER BY r.customer_id,r.started_at DESC,r.round_id DESC
            ) latest LEFT JOIN quality.current_round_reviews review ON review.round_id=latest.round_id
        """, ([item["customer_id"] for item in customers],)).fetchall()
        rounds = {row["customer_id"]: _flatten(row) for row in rows}
        metrics = {row["round_id"]: RoundServiceMetrics(row) for row in rounds.values()}
        if metrics:
            # A server cursor keeps memory proportional to the displayed customer
            # count, even when a selected round contains a long chat history.
            with connection.cursor(name="quality_service_metrics_" + uuid.uuid4().hex) as cursor:
                cursor.execute("""
                    SELECT round_id,message_id,sent_at,sender_role,direction,is_private,
                        data->'is_rating' AS is_rating
                    FROM ops.message_index WHERE round_id=ANY(%s)
                    ORDER BY round_id,sent_at NULLS LAST,message_id
                """, (list(metrics),))
                while batch := cursor.fetchmany(500):
                    for message in batch:
                        metrics[message["round_id"]].observe(message)
        for customer in customers:
            latest = rounds.get(customer["customer_id"])
            customer["latest_round"] = {**latest, "service_metrics": metrics[latest["round_id"]].result()} if latest else None

    def round_detail(self, round_id: str, page: int = 1, page_size: int = 100):
        with self.pool.connection() as connection:
            row = connection.execute("""
                SELECT r.*,c.display_name AS customer_name,c.contact_id,c.business_user_id,c.data AS customer_profile,
                    to_jsonb(review) AS review
                FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id
                LEFT JOIN quality.current_round_reviews review ON review.round_id=r.round_id
                WHERE r.round_id=%s
            """, (round_id,)).fetchone()
            if row is None:
                return None
            round_item = self._agents(connection, [_flatten(row)])[0]
            self._customer_profile(connection, round_item, round_item.pop("customer_profile", None))
            messages, total = self._messages(connection, "m.round_id=%s", [round_id], page, page_size)
        return {"round": round_item, "messages": messages, "total": total, "page": page, "page_size": page_size}

    def round_review(self, round_id: str):
        with self.pool.connection() as connection:
            row = connection.execute("""
                SELECT r.round_id,review.ai_status,review.human_status,review.remark,review.reviewer,
                    review.reviewed_at,review.revision
                FROM ops.round_index r LEFT JOIN quality.current_round_reviews review
                    ON review.round_id=r.round_id WHERE r.round_id=%s
            """, (round_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "ai_status": row["ai_status"] or "pending",
                "human_status": row["human_status"] or "pending",
                "remark": row["remark"] or "", "revision": row["revision"] or "0"}

    def save_round_review(self, round_id: str, ai_status: str, human_status: str,
                          remark: str, reviewer: str, revision: int):
        try:
            with self.pool.connection() as connection:
                # Normal queries remain read-only. Only this transaction invokes
                # the narrow append function; the reader cannot edit any table.
                connection.execute("SET TRANSACTION READ WRITE")
                row = connection.execute(
                    "SELECT quality.save_round_review(%s,%s,%s,%s,%s,%s) AS review",
                    (round_id, ai_status, human_status, remark, reviewer, revision),
                ).fetchone()
                return row["review"]
        except SerializationFailure:
            raise ReviewConflict() from None

    @staticmethod
    def _messages(connection, where: str, values: list, page: int, page_size: int):
        total = connection.execute(f"SELECT count(*) AS total FROM ops.message_index m WHERE {where}", values).fetchone()["total"]
        rows = connection.execute(f"""
            SELECT m.*,r.round_no,r.channel_key,
                CASE WHEN rating.rating_key IS NOT NULL THEN jsonb_build_object(
                    'rating',rating.rating,'feedback',rating.feedback,'status',rating.status,
                    'rating_at',rating.data->>'source_time','round_id',rating.round_id
                ) END AS visitor_rating
            FROM ops.message_index m
            LEFT JOIN ops.round_index r ON r.round_id=m.round_id
            LEFT JOIN ops.rating_index rating ON rating.source_key=m.source_key
                AND rating.account_id=m.account_id AND rating.conversation_id=m.conversation_id
                AND rating.rating_key='message:' || m.message_id::text
                AND rating.invitation_message_id=m.message_id
            WHERE {where} ORDER BY m.sent_at ASC NULLS LAST,m.message_id ASC LIMIT %s OFFSET %s
        """, [*values, page_size, (page - 1) * page_size]).fetchall()
        return [_flatten(row) for row in rows], total

    def _customer_profile(self, connection, customer: dict, customer_profile=None):
        """Fill contact fields for new and pre-profile customer projections."""
        data = customer.get("data") if isinstance(customer.get("data"), dict) else {}
        profile = {key: data.get(key) for key in ("chatwoot_user_id", "phone_number", "contact_note")}
        if not profile["chatwoot_user_id"] and customer.get("contact_id"):
            profile["chatwoot_user_id"] = str(customer["contact_id"])
        candidates = [customer_profile] if customer_profile is not None else []
        if isinstance(customer_profile, dict):
            for key in profile:
                if not profile[key] and customer_profile.get(key):
                    profile[key] = customer_profile[key]
        if not profile["phone_number"] or not profile["contact_note"]:
            rows = connection.execute("""
                SELECT s.data FROM archive.conversation_snapshots s
                JOIN (
                    SELECT DISTINCT source_key,account_id,conversation_id,customer_id FROM ops.message_index
                    UNION
                    SELECT DISTINCT source_key,account_id,conversation_id,customer_id FROM ops.round_index
                ) c ON c.source_key=s.source_key
                    AND c.account_id=s.account_id AND c.conversation_id=s.conversation_id
                WHERE c.customer_id=%s ORDER BY s.id DESC LIMIT 100
            """, (customer["customer_id"],)).fetchall()
            candidates.extend(row["data"] for row in rows)
        if not profile["phone_number"] or not profile["contact_note"]:
            rows = connection.execute("""
                SELECT e.payload FROM archive.events e
                JOIN (
                    SELECT DISTINCT source_key,account_id,conversation_id,customer_id FROM ops.message_index
                    UNION
                    SELECT DISTINCT source_key,account_id,conversation_id,customer_id FROM ops.round_index
                ) c ON c.source_key=e.source_key
                    AND c.account_id=e.account_id AND c.conversation_id=e.conversation_id
                WHERE c.customer_id=%s ORDER BY e.id DESC LIMIT 100
            """, (customer["customer_id"],)).fetchall()
            for row in rows:
                payload = row["payload"] if isinstance(row["payload"], dict) else {}
                conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
                candidates.extend((payload.get("contact"), payload.get("sender"), conversation.get("contact"),
                                   (conversation.get("meta") or {}).get("sender")
                                   if isinstance(conversation.get("meta"), dict) else None))
        for candidate in candidates:
            found = _snapshot_contact(candidate)
            for key in profile:
                if not profile[key] and found.get(key):
                    profile[key] = found[key]
            if profile["phone_number"] and profile["contact_note"]:
                break
        customer.update({key: value for key, value in profile.items() if value is not None})
        return customer

    def customer_history(self, customer_id: str, page: int = 1, page_size: int = 100):
        with self.pool.connection() as connection:
            customer = connection.execute("""
                SELECT c.*, (SELECT count(*) FROM ops.round_index r WHERE r.customer_id=c.customer_id) AS round_count,
                    (SELECT count(*) FROM ops.message_index m WHERE m.customer_id=c.customer_id) AS message_count
                FROM ops.customer_index c WHERE c.customer_id=%s
            """, (customer_id,)).fetchone()
            if customer is None:
                return None
            customer = self._customer_profile(connection, _flatten(customer))
            messages, total = self._messages(connection, "m.customer_id=%s", [customer_id], page, page_size)
        return {"customer": customer, "messages": messages, "total": total, "page": page, "page_size": page_size}

    def customer_rounds(self, customer_id: str, page: int = 1, page_size: int = 50):
        """Read a customer's round directory without loading any message bodies."""
        with self.pool.connection() as connection:
            customer = connection.execute(
                "SELECT customer_id FROM ops.customer_index WHERE customer_id=%s", (customer_id,)
            ).fetchone()
            if customer is None:
                return None
            total = connection.execute(
                "SELECT count(*) AS total FROM ops.round_index WHERE customer_id=%s", (customer_id,)
            ).fetchone()["total"]
            rows = connection.execute("""
                SELECT r.*,c.display_name AS customer_name,c.contact_id,c.business_user_id,
                    to_jsonb(review) AS review
                FROM ops.round_index r JOIN ops.customer_index c ON c.customer_id=r.customer_id
                LEFT JOIN quality.current_round_reviews review ON review.round_id=r.round_id
                WHERE r.customer_id=%s
                ORDER BY r.started_at DESC,r.round_id DESC LIMIT %s OFFSET %s
            """, (customer_id, page_size, (page - 1) * page_size)).fetchall()
            items = [_flatten(row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def message_versions(self, source_key: str, account_id: int, message_id: int, page: int = 1, page_size: int = 100):
        with self.pool.connection() as connection:
            values = (source_key, account_id, message_id)
            where = "source_key=%s AND account_id=%s AND message_id=%s"
            total = connection.execute(f"SELECT count(*) AS total FROM archive.message_snapshots WHERE {where}", values).fetchone()["total"]
            rows = connection.execute(f"""
                SELECT id,event_id,snapshot_kind,content,sender_role,sender_name,is_private,sent_at,
                    source_updated_at,recorded_at,content_attributes FROM archive.message_snapshots
                WHERE {where} ORDER BY COALESCE(source_updated_at,sent_at,recorded_at),id LIMIT %s OFFSET %s
            """, (*values, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def filter_options(self):
        from ics_agent.channels import list_channels

        with self.pool.connection() as connection:
            # The receipt index is available before projection succeeds.
            sources = connection.execute("SELECT DISTINCT source_key,account_id FROM archive.events ORDER BY source_key,account_id LIMIT 500").fetchall()
            agents = connection.execute("""
                SELECT source_key,account_id,agent_id,
                    COALESCE(max(NULLIF(agent_name,'')), '#' || agent_id) AS agent_name
                FROM (
                    SELECT r.source_key,r.account_id,a.agent_id,a.agent_name
                    FROM ops.round_agent_index a JOIN ops.round_index r ON r.round_id=a.round_id
                    UNION ALL
                    SELECT source_key,account_id,current_assignee_id,NULL::text
                    FROM ops.conversation_index WHERE NULLIF(current_assignee_id,'') IS NOT NULL
                    UNION ALL
                    SELECT source_key,account_id,data->>'responsible_agent_id',data->>'responsible_agent_name'
                    FROM ops.round_index WHERE data->>'responsibility_basis'='ics_assignment'
                        AND NULLIF(data->>'responsible_agent_id','') IS NOT NULL
                ) agents GROUP BY source_key,account_id,agent_id
                ORDER BY source_key,account_id,agent_name,agent_id LIMIT 1000
            """).fetchall()
            inboxes = connection.execute("""
                SELECT source_key,account_id,inbox_id,'#' || inbox_id::text AS name,max(channel_key) AS channel_key
                FROM (
                    SELECT source_key,account_id,inbox_id,channel_key FROM ops.conversation_index WHERE inbox_id IS NOT NULL
                    UNION ALL
                    SELECT source_key,account_id,inbox_id,channel_key FROM ops.round_index WHERE inbox_id IS NOT NULL
                ) inboxes GROUP BY source_key,account_id,inbox_id
                ORDER BY source_key,account_id,inbox_id LIMIT 1000
            """).fetchall()
        return {
            "sources": [dict(row) for row in sources],
            "channels": [{"key": channel.key, "name": channel.name} for channel in list_channels()],
            "agents": [dict(row) for row in agents],
            "inboxes": [dict(row) for row in inboxes],
        }

    def raw_event(self, event_id: int, source_key: str, account_id: int):
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT raw_body FROM archive.events WHERE id=%s AND source_key=%s AND account_id=%s",
                (event_id, source_key, account_id),
            ).fetchone()
        return None if row is None else bytes(row["raw_body"])

    def unlinked_ratings(self, page: int = 1, page_size: int = 30):
        with self.pool.connection() as connection:
            total = connection.execute("SELECT count(*) AS total FROM ops.rating_index WHERE status='unlinked'").fetchone()["total"]
            rows = connection.execute("""
                SELECT r.*,s.event_id,NULLIF(r.data ->> 'source_time', '')::timestamptz AS rating_at FROM ops.rating_index r
                JOIN archive.rating_snapshots s ON s.id=r.snapshot_id WHERE r.status='unlinked'
                ORDER BY rating_at DESC NULLS LAST,r.snapshot_id DESC LIMIT %s OFFSET %s
            """, (page_size, (page - 1) * page_size)).fetchall()
        return {"items": [_flatten(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def processing_status(self):
        with self.pool.connection() as connection:
            rows = connection.execute("SELECT status,count(*) AS count FROM ops.event_jobs GROUP BY status").fetchall()
            latest = connection.execute("SELECT max(received_at) AS last_received_at FROM archive.events").fetchone()
            unlinked = connection.execute("SELECT count(*) AS unlinked_rating_count FROM ops.rating_index WHERE status='unlinked'").fetchone()
        return {"jobs": {row["status"]: row["count"] for row in rows}, **dict(latest), **dict(unlinked)}
