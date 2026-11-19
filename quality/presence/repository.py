"""Independent, bounded persistence of real Chatwoot agent observations."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .retention import CLEANUP_BATCH_SIZE, CLEANUP_MAX_BATCHES, retention_metadata


UTC = timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")
MAX_AGENTS = 200


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError("Presence observations require an explicit ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Presence observations require an explicit timezone")
    return parsed.astimezone(UTC)


def _positive_id(value):
    identifier = str(value)
    if (isinstance(value, bool) or not identifier.isascii() or not identifier.isdecimal()
            or len(identifier.lstrip("0")) > 19 or not 0<int(identifier)<=2**63-1):
        raise ValueError("Presence observations require positive Chatwoot identifiers")
    return int(identifier)


def _scope(source_key, account_id):
    if (not isinstance(source_key, str) or not 1<=len(source_key)<=128
            or source_key != source_key.strip() or "\x00" in source_key):
        raise ValueError("Presence observations require an identified source")
    return source_key, _positive_id(account_id)


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("status") != "ready":
        raise ValueError("Only successful fresh directory observations may be sampled")
    source, account = _scope(snapshot.get("source_key"), snapshot.get("account_id"))
    at = _time(snapshot.get("sampled_at"))
    agents = snapshot.get("agents")
    if not isinstance(agents, list) or len(agents)>MAX_AGENTS:
        raise ValueError("Presence snapshots must contain at most 200 agents")
    normalized, identifiers = [], set()
    for row in agents:
        if not isinstance(row, dict):
            raise ValueError("Invalid presence agent observation")
        if (row.get("source_key", source) != source
                or _positive_id(row.get("account_id", account)) != account):
            raise ValueError("Presence agent scope differs from its snapshot")
        agent_id = str(_positive_id(row.get("agent_id")))
        if agent_id in identifiers:
            raise ValueError("Duplicate agent in a presence snapshot")
        identifiers.add(agent_id)
        name = row.get("agent_name")
        if name is not None and (not isinstance(name, str) or len(name)>200 or "\x00" in name):
            raise ValueError("Invalid presence agent name")
        name = name.strip() or None if name is not None else None
        name_source = row.get("agent_name_source", "chatwoot" if name else "unobserved")
        status = row.get("availability_status", "unknown")
        if name_source not in ("chatwoot", "unobserved") or status not in ("online", "busy", "offline", "unknown"):
            raise ValueError("Invalid observed presence status or name provenance")
        normalized.append({"agent_id": agent_id, "agent_name": name,
            "agent_name_source": name_source if name else "unobserved", "availability_status": status})
    return source, account, at, sorted(normalized, key=lambda row: int(row["agent_id"]))


def _serialize(row):
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in dict(row).items()}


class PresenceRepository:
    def __init__(self, settings):
        self.pool = ConnectionPool(kwargs={**settings.connection_kwargs, "row_factory": dict_row,
            "connect_timeout": 2, "application_name": "ics-quality-presence",
            "options": "-c statement_timeout=3000 -c lock_timeout=500"},
            min_size=0, max_size=1, timeout=1, max_waiting=1, num_workers=1,
            reconnect_timeout=5, open=False)

    def open(self):
        self.pool.open(wait=False)
        with self.pool.connection() as connection:
            row = connection.execute("""SELECT current_user AS role,r.rolsuper,
                pg_has_role(current_user,'archive_owner','MEMBER') AS owner,
                has_table_privilege(current_user,'archive.agent_presence_samples','INSERT') AS appends,
                has_table_privilege(current_user,'archive.agent_presence_samples','UPDATE,DELETE,TRUNCATE') AS mutates,
                has_table_privilege(current_user,'archive.events','SELECT,INSERT,UPDATE,DELETE,TRUNCATE') AS chat_access,
                has_column_privilege(current_user,'ops.agent_presence_registry','hidden_at','UPDATE') AS manages_roster
                FROM pg_roles r WHERE r.rolname=current_user""").fetchone()
            if (not row or row["role"] != "archive_presence" or row["rolsuper"] or row["owner"]
                    or not row["appends"] or row["mutates"] or row["chat_access"] or not row["manages_roster"]):
                raise RuntimeError("Presence persistence requires its restricted role and migrations 004 through 007")

    def close(self):
        self.pool.close()

    @staticmethod
    def _roster(connection, source, account):
        return [_serialize(row) for row in connection.execute("""SELECT source_key,account_id,agent_id,
            agent_name,agent_name_source,first_seen_at,last_seen_at,removed_at
            FROM ops.agent_presence_registry WHERE source_key=%s AND account_id=%s
                AND removed_at IS NULL AND hidden_at IS NULL
            ORDER BY agent_id LIMIT 200""", (source, account)).fetchall()]

    def append(self, snapshot):
        source, account, at, agents = validate_snapshot(snapshot)
        minute = at.replace(second=0, microsecond=0)
        identifiers = [row["agent_id"] for row in agents]
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("""INSERT INTO ops.agent_presence_monitor(source_key,account_id,started_at)
                    VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""", (source, account, at))
                monitor = connection.execute("""SELECT started_at,last_sampled_minute FROM ops.agent_presence_monitor
                    WHERE source_key=%s AND account_id=%s FOR UPDATE""", (source, account)).fetchone()
                duplicate = monitor["last_sampled_minute"] is not None and minute<=monitor["last_sampled_minute"]
                inserted = 0
                if not duplicate:
                    observed = Jsonb(agents)
                    inserted = len(connection.execute("""INSERT INTO archive.agent_presence_samples
                        (source_key,account_id,agent_id,sampled_minute,sampled_at,agent_name,agent_name_source,availability_status)
                        SELECT %s,%s,a.agent_id,%s,%s,a.agent_name,a.agent_name_source,a.availability_status
                        FROM jsonb_to_recordset(%s) a(agent_id text,agent_name text,agent_name_source text,availability_status text)
                        ON CONFLICT DO NOTHING RETURNING agent_id""", (source, account, minute, at, observed)).fetchall())
                    connection.execute("""INSERT INTO ops.agent_presence_registry
                        (source_key,account_id,agent_id,first_seen_at,last_seen_at,agent_name,agent_name_source)
                        SELECT %s,%s,a.agent_id,%s,%s,a.agent_name,a.agent_name_source
                        FROM jsonb_to_recordset(%s) a(agent_id text,agent_name text,agent_name_source text)
                        ON CONFLICT(source_key,account_id,agent_id) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at,
                            agent_name=EXCLUDED.agent_name,agent_name_source=EXCLUDED.agent_name_source,removed_at=NULL,
                            hidden_at=CASE WHEN EXCLUDED.last_seen_at>ops.agent_presence_registry.hidden_at
                                THEN NULL ELSE ops.agent_presence_registry.hidden_at END""",
                        (source, account, at, at, observed))
                    connection.execute("""INSERT INTO ops.agent_presence_periods(source_key,account_id,agent_id,started_minute)
                        SELECT %s,%s,a.agent_id,%s FROM jsonb_to_recordset(%s) a(agent_id text)
                        WHERE NOT EXISTS(SELECT 1 FROM ops.agent_presence_periods p
                            WHERE p.source_key=%s AND p.account_id=%s AND p.agent_id=a.agent_id AND p.ended_minute IS NULL)
                        ON CONFLICT DO NOTHING""", (source, account, minute, observed, source, account))
                    connection.execute("""UPDATE ops.agent_presence_periods SET ended_minute=%s
                        WHERE source_key=%s AND account_id=%s AND ended_minute IS NULL AND NOT(agent_id=ANY(%s))""",
                        (minute, source, account, identifiers))
                    connection.execute("""UPDATE ops.agent_presence_registry SET removed_at=%s
                        WHERE source_key=%s AND account_id=%s AND removed_at IS NULL AND NOT(agent_id=ANY(%s))""",
                        (at, source, account, identifiers))
                    connection.execute("""UPDATE ops.agent_presence_monitor SET last_sampled_at=%s,last_sampled_minute=%s
                        WHERE source_key=%s AND account_id=%s""", (at, minute, source, account))
                roster = self._roster(connection, source, account)
        return {"source_key": source, "account_id": account, "sampled_at": at.isoformat(),
            "monitoring_started_at": monitor["started_at"].isoformat(), "inserted_samples": inserted,
            "duplicate": duplicate, "agents": roster}

    def list_agents(self, source_key, account_id, page=1, page_size=50, include_hidden=False):
        source, account = _scope(source_key, account_id)
        if type(page) is not int or page<1 or type(page_size) is not int or not 1<=page_size<=100:
            raise ValueError("Invalid presence roster page")
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                total = connection.execute("""SELECT count(*) AS total FROM ops.agent_presence_registry
                    WHERE source_key=%s AND account_id=%s AND (%s OR hidden_at IS NULL)""",
                    (source, account, include_hidden)).fetchone()["total"]
                rows = connection.execute("""SELECT source_key,account_id,agent_id,agent_name,agent_name_source,
                        first_seen_at,last_seen_at,removed_at,hidden_at
                    FROM ops.agent_presence_registry
                    WHERE source_key=%s AND account_id=%s AND (%s OR hidden_at IS NULL)
                    ORDER BY agent_name NULLS LAST,agent_id LIMIT %s OFFSET %s""",
                    (source, account, include_hidden, page_size, (page-1)*page_size)).fetchall()
        return {"status": "ready", "source_key": source, "account_id": account,
                "agents": [_serialize(row) for row in rows], "total": total,
                "page": page, "page_size": page_size}

    def remove_agent(self, source_key, account_id, agent_id, now=None):
        source, account = _scope(source_key, account_id)
        identifier = str(_positive_id(agent_id))
        at = _time(now or datetime.now(UTC))
        with self.pool.connection() as connection:
            with connection.transaction():
                # Serialize with append without closing genuine membership periods.
                connection.execute("""SELECT started_at FROM ops.agent_presence_monitor
                    WHERE source_key=%s AND account_id=%s FOR UPDATE""", (source, account))
                row = connection.execute("""UPDATE ops.agent_presence_registry
                    SET hidden_at=COALESCE(hidden_at,greatest(%s,last_seen_at))
                    WHERE source_key=%s AND account_id=%s AND agent_id=%s
                    RETURNING source_key,account_id,agent_id,hidden_at""",
                    (at, source, account, identifier)).fetchone()
        return {"status": "ready" if row else "not_found", **(_serialize(row) if row else {})}

    def daily(self, day, source_key, account_id, now=None, *, include_hidden=False):
        source, account = _scope(source_key, account_id)
        now = _time(now or datetime.now(UTC))
        day = day or now.astimezone(BEIJING).date()
        if type(day) is not date:
            raise ValueError("Presence daily statistics require a date")
        start = datetime.combine(day, time(), BEIJING).astimezone(UTC)
        end = start+timedelta(days=1)
        through = max(start, min(end, now.replace(second=0, microsecond=0)))
        retention = retention_metadata(now)
        if day.isoformat() < retention["oldest_available_day"]:
            return {**retention, "source_key": source, "account_id": account,
                    "day": day.isoformat(), "timezone": "Asia/Shanghai", "status": "expired",
                    "monitoring_started_at": None, "through_at": through.isoformat(),
                    "agents": [], "agent_count": 0, "agents_truncated": False}
        retained_start = max(start, datetime.fromisoformat(retention["retention_cutoff_at"]))
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                monitor = connection.execute("""SELECT started_at FROM ops.agent_presence_monitor
                    WHERE source_key=%s AND account_id=%s""", (source, account)).fetchone()
                rows = [] if monitor is None else connection.execute("""
                    WITH expected AS (
                        SELECT agent_id,sum(extract(epoch FROM least(COALESCE(ended_minute,%s),%s)
                            - greatest(started_minute,%s))/60)::bigint AS expected_minutes
                        FROM ops.agent_presence_periods WHERE source_key=%s AND account_id=%s
                            AND started_minute<%s AND COALESCE(ended_minute,%s)>%s GROUP BY agent_id
                    ), sampled AS (
                        SELECT agent_id,count(*) AS sampled_minutes,
                            count(*) FILTER(WHERE availability_status='online') AS online_minutes,
                            count(*) FILTER(WHERE availability_status='busy') AS busy_minutes,
                            count(*) FILTER(WHERE availability_status='offline') AS offline_minutes,
                            count(*) FILTER(WHERE availability_status='unknown') AS unknown_minutes,
                            max(sampled_at) AS last_sampled_at
                        FROM archive.agent_presence_samples WHERE source_key=%s AND account_id=%s
                            AND sampled_minute>=%s AND sampled_minute<%s GROUP BY agent_id
                    )
                    SELECT r.source_key,r.account_id,r.agent_id,r.agent_name,r.agent_name_source,
                        r.first_seen_at,r.last_seen_at,r.removed_at,r.hidden_at,
                        COALESCE(e.expected_minutes,0) AS expected_minutes,COALESCE(s.sampled_minutes,0) AS sampled_minutes,
                        COALESCE(s.online_minutes,0) AS online_minutes,COALESCE(s.busy_minutes,0) AS busy_minutes,
                        COALESCE(s.offline_minutes,0) AS offline_minutes,COALESCE(s.unknown_minutes,0) AS unknown_minutes,
                        greatest(COALESCE(e.expected_minutes,0)-COALESCE(s.sampled_minutes,0),0) AS missing_minutes,
                        s.last_sampled_at,count(*) OVER() AS agent_count
                    FROM ops.agent_presence_registry r LEFT JOIN expected e USING(agent_id)
                    LEFT JOIN sampled s USING(agent_id) WHERE r.source_key=%s AND r.account_id=%s
                        AND (%s OR r.hidden_at IS NULL)
                        AND (COALESCE(e.expected_minutes,0)>0 OR COALESCE(s.sampled_minutes,0)>0)
                    ORDER BY r.agent_name NULLS LAST,r.agent_id LIMIT 200
                """, (through, through, retained_start, source, account, through, through, retained_start,
                      source, account, retained_start, through, source, account, include_hidden)).fetchall()
        count = rows[0]["agent_count"] if rows else 0
        agents = [_serialize({key: value for key, value in row.items() if key != "agent_count"}) for row in rows]
        return {**retention, "source_key": source, "account_id": account, "day": day.isoformat(), "timezone": "Asia/Shanghai",
            "status": "ready" if monitor else "not_started",
            "monitoring_started_at": monitor["started_at"].isoformat() if monitor else None,
            "through_at": through.isoformat(), "agents": agents, "agent_count": count,
            "agents_truncated": count>MAX_AGENTS,
            "expired_minutes": int((min(end, retained_start)-start).total_seconds()//60)}

    def timeline(self, day, source_key, account_id, agent_id, now=None):
        """Read one agent and one Beijing day; never interpolate missing states."""
        from .timeline import build_timeline

        source, account = _scope(source_key, account_id)
        identifier = str(_positive_id(agent_id))
        now = _time(now or datetime.now(UTC))
        day = day or now.astimezone(BEIJING).date()
        if type(day) is not date:
            raise ValueError("Presence timeline requires a date")
        start = datetime.combine(day, time(), BEIJING).astimezone(UTC)
        end = start + timedelta(days=1)
        through = max(start, min(end, now.replace(second=0, microsecond=0)))
        base = {"source_key": source, "account_id": account, "day": day.isoformat(),
                "timezone": "Asia/Shanghai", "granularity_minutes": 1,
                "agent": {"agent_id": identifier, "agent_name": None, "agent_name_source": "unobserved"},
                "segments": [], "sample_count": 0, "monitoring_started_at": None,
                "through_at": through.isoformat(), **retention_metadata(now)}
        if day.isoformat() < base["oldest_available_day"]:
            return {**base, "status": "expired"}
        retained_start = max(start, datetime.fromisoformat(base["retention_cutoff_at"]))
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                monitor = connection.execute("""SELECT started_at FROM ops.agent_presence_monitor
                    WHERE source_key=%s AND account_id=%s""", (source, account)).fetchone()
                if monitor is None:
                    return {**base, "status": "not_started"}
                base["monitoring_started_at"] = monitor["started_at"].isoformat()
                agent = connection.execute("""SELECT source_key,account_id,agent_id,agent_name,agent_name_source
                    FROM ops.agent_presence_registry WHERE source_key=%s AND account_id=%s AND agent_id=%s""",
                    (source, account, identifier)).fetchone()
                if agent is None:
                    return {**base, "status": "not_found"}
                # The PK can seek to this day and its single preceding interval;
                # years of already-closed intervals never need to be transferred.
                periods = connection.execute("""WITH previous AS (
                        SELECT started_minute,ended_minute FROM ops.agent_presence_periods
                        WHERE source_key=%s AND account_id=%s AND agent_id=%s AND started_minute<%s
                        ORDER BY started_minute DESC LIMIT 1
                    )
                    SELECT started_minute,ended_minute FROM (
                        SELECT started_minute,ended_minute FROM ops.agent_presence_periods
                        WHERE source_key=%s AND account_id=%s AND agent_id=%s
                            AND started_minute>=%s AND started_minute<%s
                        UNION ALL SELECT started_minute,ended_minute FROM previous
                    ) p WHERE ended_minute IS NULL OR ended_minute>%s
                    ORDER BY started_minute LIMIT 1441""",
                    (source, account, identifier, start, source, account, identifier, start, through, start)).fetchall()
                samples = connection.execute("""SELECT sampled_minute,availability_status
                    FROM archive.agent_presence_samples WHERE source_key=%s AND account_id=%s AND agent_id=%s
                        AND sampled_minute>=%s AND sampled_minute<%s
                    ORDER BY sampled_minute LIMIT 1441""", (source, account, identifier, retained_start, through)).fetchall()
        if len(periods)>1440 or len(samples)>1440:
            raise ValueError("Presence timeline exceeds its single-day observation boundary")
        return {**base, **build_timeline(day, now, periods, samples,
                                       retention_cutoff=datetime.fromisoformat(base["retention_cutoff_at"])), "status": "ready",
                "agent": _serialize(agent), "sample_count": len(samples)}

    def cleanup(self, source_key, account_id):
        """Run one server-bounded batch without granting this role DELETE access."""
        source, account = _scope(source_key, account_id)
        with self.pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    "SELECT * FROM ops.run_presence_cleanup(%s,%s)",
                    (source, account),
                ).fetchone()
        if row is None or any(type(row.get(key)) is not int or not 0 <= row[key] <= CLEANUP_BATCH_SIZE
                              for key in ("deleted_samples", "deleted_periods")):
            raise RuntimeError("Presence cleanup returned an invalid bounded batch")
        if row.get("cleanup_status") not in {"waiting", "pending", "completed", "limited"} or (
            type(row.get("cleanup_day")) is not date or type(row.get("batches")) is not int
            or not 0 <= row["batches"] <= CLEANUP_MAX_BATCHES
            or any(type(row.get(key)) is not int or not 0 <= row[key] <= row["batches"] * CLEANUP_BATCH_SIZE
                   for key in ("total_deleted_samples", "total_deleted_periods"))
        ):
            raise RuntimeError("Presence cleanup returned an invalid bounded batch state")
        return {**row, "cutoff_at": row["cutoff_at"].isoformat(),
                "last_success_at": row["last_success_at"].isoformat() if row["last_success_at"] else None}
