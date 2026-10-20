"""Read-only aggregates over quality's existing pool and archive projections."""

from datetime import datetime, timedelta, timezone

from quality.presence.retention import retention_metadata
from .agent_quality import consultation_ctes, handoff_ctes, reply_quality
from .periods import BEIJING
from .quality_reports import ai_quality, percentage

# Historical ICS notice: only this observed message proves a busy refusal.
# Do not infer refusal from an unanswered/pending handoff or a muted contact.
BUSY_NOTICE = ("当前人工坐席繁忙，您可以先继续描述问题，我会尽力帮您解答。"
               "如仍需人工客服，请稍后再次尝试「转人工」。")
METRICS = ("incoming", "ai_only", "handoff", "rejected", "effective_human", "low_rating")


def scope_sql(alias, source_key=None, account_id=None, channel_key=None):
    clauses, values = [], []
    for name, value in (("source_key", source_key), ("account_id", account_id), ("channel_key", channel_key)):
        if value is None:
            continue
        if name == "channel_key" and value == "unmapped":
            clauses.append(f"{alias}.channel_key IS NULL")
        else:
            clauses.append(f"{alias}.{name}=%s")
            values.append(value)
    return " AND ".join(clauses) or "TRUE", values


def bucket_sql(column, grain):
    # grain is selected exclusively by ReportPeriod, never interpolated user text.
    if grain not in {"hour", "day", "month", "minute"}:
        raise ValueError("Unsupported reporting grain")
    return f"date_trunc('{grain}', {column} AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'"


class StatisticsRepository:
    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def transaction(connection):
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '8000ms'")

    def time_report(self, period, *, source_key=None, account_id=None, channel_key=None, now=None):
        now = now or datetime.now(timezone.utc)
        where, values = scope_sql("r", source_key, account_id, channel_key)
        bucket = bucket_sql("r.started_at", period.grain)
        with self.pool.connection() as connection:
            with connection.transaction():
                self.transaction(connection)
                rows = connection.execute(f"""
                    WITH selected AS MATERIALIZED (
                        SELECT r.round_id,r.channel_key,r.reception_type,r.rating,{bucket} AS bucket
                        FROM ops.round_index r
                        WHERE {where} AND r.started_at >= %s AND r.started_at < %s
                    ), flags AS (
                        SELECT s.*,
                            EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=s.round_id
                                AND m.sender_role='bot' AND m.direction='outgoing' AND NOT m.is_private
                                AND m.content=%s) AS refused,
                            EXISTS(SELECT 1 FROM ops.message_index m WHERE m.round_id=s.round_id
                                AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                                AND COALESCE(m.data->>'is_rating','false') <> 'true') AS replied
                        FROM selected s
                    )
                    SELECT bucket,COALESCE(channel_key,'unmapped') AS channel_key,
                        count(*) AS incoming,
                        count(*) FILTER(WHERE reception_type='ai_only' AND NOT refused AND NOT replied) AS ai_only,
                        count(*) FILTER(WHERE reception_type='human' OR refused OR replied) AS handoff,
                        count(*) FILTER(WHERE refused) AS rejected,
                        count(*) FILTER(WHERE replied) AS effective_human,
                        count(*) FILTER(WHERE rating < 4) AS low_rating
                    FROM flags GROUP BY bucket,channel_key ORDER BY bucket,channel_key
                """, [*values, period.start, min(period.end, now), BUSY_NOTICE]).fetchall()
                processing = connection.execute("""SELECT count(*) FILTER(WHERE status IN ('queued','retry','processing','failed')) AS pending_jobs,
                    max(completed_at) AS last_processed_at FROM ops.event_jobs""").fetchone()
        indexed = {b["key"]: {"key": b["key"], **dict.fromkeys(METRICS, 0), "channels": {}} for b in period.buckets}
        totals = dict.fromkeys(METRICS, 0)
        channels = {}
        for row in rows:
            item = indexed[row["bucket"].astimezone(BEIJING).isoformat()]
            channel = row["channel_key"]
            item["channels"][channel] = row["incoming"]
            channels[channel] = channels.get(channel, 0) + row["incoming"]
            for metric in METRICS:
                item[metric] += row[metric]
                totals[metric] += row[metric]
        return {**period.metadata(now), "rows": list(indexed.values()), "totals": totals,
                "channels": channels, "processing": dict(processing)}

    def agent_report(self, period, *, source_key=None, account_id=None, page=1, page_size=20, now=None):
        now = now or datetime.now(timezone.utc)
        where, values = scope_sql("s", source_key, account_id)
        retention = retention_metadata(now)
        retained_start = max(period.start, datetime.fromisoformat(retention["retention_cutoff_at"]))
        through = min(period.end, now.replace(second=0, microsecond=0))
        grain = "minute" if period.mode == "day" else period.grain
        bucket = bucket_sql("s.sampled_minute", grain)
        with self.pool.connection() as connection:
            with connection.transaction():
                self.transaction(connection)
                # Missing minutes remain absent; no last-known state carry-forward.
                observed = connection.execute(f"""
                    WITH minutes AS (
                        SELECT {bucket} AS bucket,s.sampled_minute,
                            count(*) FILTER(WHERE availability_status='online') AS online,
                            count(*) FILTER(WHERE availability_status='busy') AS busy,
                            count(*) FILTER(WHERE availability_status='offline') AS offline,
                            count(*) FILTER(WHERE availability_status='unknown') AS unknown,
                            count(*) AS samples
                        FROM archive.agent_presence_samples s WHERE {where}
                            AND sampled_minute >= %s AND sampled_minute < %s
                        GROUP BY bucket,s.sampled_minute
                    ) SELECT bucket,avg(online) AS online,avg(busy) AS busy,avg(offline) AS offline,
                        avg(unknown) AS unknown,max(online) AS peak_online,count(*) AS observed_minutes,
                        sum(samples) AS samples FROM minutes GROUP BY bucket ORDER BY bucket
                """, [*values, retained_start, through]).fetchall() if retained_start < through else []
                # Expected seat-minutes let the page distinguish a complete zero from missing sampling.
                periods = connection.execute(f"""SELECT s.started_minute,s.ended_minute
                    FROM ops.agent_presence_periods s WHERE {where}
                    AND s.started_minute < %s AND (s.ended_minute IS NULL OR s.ended_minute > %s)
                """, [*values, through, retained_start]).fetchall() if retained_start < through else []
                agents = self._agents(connection, period, retained_start, through, source_key, account_id, page, page_size, now)
        return {**period.metadata(now), **retention,
                **self._presence(period, observed, periods, retained_start, through, now), **agents}

    @staticmethod
    def _presence(period, observed, periods, retained_start, through, now):
        statuses = ("online", "busy", "offline", "unknown")
        observed_map = {row["bucket"].astimezone(BEIJING).isoformat(): row for row in observed}
        rows, points = [], []
        for b in period.buckets:
            start, end = max(b["start"], retained_start), min(b["end"], through)
            expected_minutes = max(0, int((end - start).total_seconds() // 60))
            expected_samples = sum(max(0, int((min(end, p["ended_minute"] or end) - max(start, p["started_minute"])).total_seconds() // 60)) for p in periods)
            if period.mode == "day":
                samples = []
                for offset in range(60):
                    at = b["start"] + timedelta(minutes=offset)
                    row = observed_map.get(at.isoformat())
                    if row:
                        samples.append(row)
                    points.append({"key": at.isoformat(), "label": at.strftime("%H:%M"),
                                   "online": int(row["online"]) if row else None,
                                   "future": at >= through and at >= now.replace(second=0, microsecond=0)})
            else:
                row = observed_map.get(b["key"])
                samples = [row] if row else []
            observed_minutes = sum(int(r["observed_minutes"]) for r in samples)
            sample_count = sum(int(r["samples"]) for r in samples)
            state = "future" if b["start"] >= now else "expired" if b["end"] <= retained_start and b["end"] <= period.end else "not_monitored" if not expected_samples and not sample_count else "missing" if not sample_count else "partial" if sample_count < expected_samples or b["start"] < retained_start else "observed"
            item = {"key": b["key"], "expected_minutes": expected_minutes,
                    "observed_minutes": observed_minutes, "expected_samples": expected_samples,
                    "samples": sample_count, "coverage": round(min(1, sample_count / expected_samples) * 100, 1) if expected_samples else None,
                    "peak_online": max((int(r["peak_online"]) for r in samples), default=None), "status": state}
            for status in statuses:
                item[status] = round(sum(float(r[status]) * int(r["observed_minutes"]) for r in samples) / observed_minutes, 2) if observed_minutes else None
            rows.append(item)
        if period.mode != "day":
            points = [{"key": r["key"], "label": b["label"], "online": r["online"], "future": b["start"] >= now}
                      for r, b in zip(rows, period.buckets)]
        return {"presence_rows": rows, "presence_points": points,
                "presence_summary": {"observed_minutes": sum(r["observed_minutes"] for r in rows),
                    "samples": sum(r["samples"] for r in rows),
                    "expected_samples": sum(r["expected_samples"] for r in rows),
                    "peak_online": max((r["peak_online"] for r in rows if r["peak_online"] is not None), default=None)}}

    def quality_report(self, period, *, source_key=None, account_id=None, page=1, page_size=20, now=None):
        now = now or datetime.now(timezone.utc)
        where, values = scope_sql("r", source_key, account_id)
        retained_start = max(period.start, datetime.fromisoformat(retention_metadata(now)["retention_cutoff_at"]))
        through = min(period.end, now.replace(second=0, microsecond=0))
        with self.pool.connection() as connection:
            with connection.transaction():
                self.transaction(connection)
                human = self._agents(connection, period, retained_start, through, source_key, account_id,
                    page, page_size, now, include_reviews=True)
                ai = ai_quality(connection, period, where, values, now, BUSY_NOTICE)
        return {**period.metadata(now), **human, "ai": ai,
            "human_failure_rate": percentage(human['total_failed_inquiries'], human['total_inquiries'])}

    def _agents(self, connection, period, retained_start, through, source, account, page, page_size, now,
                include_reviews=False):
        m_where, m_values = scope_sql("m", source, account)
        s_where, s_values = scope_sql("s", source, account)
        r_where, r_values = scope_sql("r", source, account)
        bucket = bucket_sql("m.sent_at", period.grain)
        review_join = "LEFT JOIN quality.current_round_reviews review USING(round_id)" if include_reviews else ""
        review_count = ",count(DISTINCT round_id) FILTER(WHERE review.human_status='failed') AS failed_inquiries" if include_reviews else ""
        review_seat = ",COALESCE(r.failed_inquiries,0) AS failed_inquiries" if include_reviews else ""
        review_total = "(SELECT COALESCE(sum(failed_inquiries),0) FROM seats) AS total_failed_inquiries," if include_reviews else ""
        # Query the complete seat set, then paginate in PostgreSQL. Identity always includes source/account.
        rows = connection.execute(f"""
            WITH bounds AS (SELECT %s::timestamptz AS starts_at,%s::timestamptz AS ends_at,
                %s::timestamptz AS observed_at),
            {handoff_ctes(r_where)}, {consultation_ctes(r_where)}, replies AS MATERIALIZED (
                SELECT m.source_key,m.account_id,m.sender_id AS agent_id,m.sender_name,m.round_id,m.sent_at
                FROM ops.message_index m WHERE {m_where} AND m.sent_at >= %s AND m.sent_at < %s
                    AND m.sender_role='agent' AND m.direction='outgoing' AND NOT m.is_private
                    AND COALESCE(m.data->>'is_rating','false') <> 'true' AND m.round_id IS NOT NULL
                    AND m.sender_id IS NOT NULL
            ), reply_totals AS (
                SELECT source_key,account_id,agent_id,max(sender_name) AS agent_name,
                    count(DISTINCT round_id) AS inquiries,count(*) AS replies {review_count}
                FROM replies {review_join} GROUP BY 1,2,3
            ), presence AS (
                SELECT s.source_key,s.account_id,s.agent_id,max(s.agent_name) AS agent_name,
                    count(*) FILTER(WHERE availability_status='online') AS online_minutes,
                    count(*) FILTER(WHERE availability_status='busy') AS busy_minutes,
                    count(*) FILTER(WHERE availability_status='offline') AS offline_minutes,
                    count(*) FILTER(WHERE availability_status='unknown') AS unknown_minutes
                FROM archive.agent_presence_samples s WHERE {s_where}
                    AND s.sampled_minute >= %s AND s.sampled_minute < %s GROUP BY 1,2,3
            ), roster AS (
                SELECT s.source_key,s.account_id,s.agent_id,s.agent_name FROM ops.agent_presence_registry s
                WHERE {s_where} AND s.first_seen_at < %s
                    AND (s.removed_at IS NULL OR s.removed_at >= %s)
            ), identities AS (
                SELECT source_key,account_id,agent_id FROM reply_totals UNION
                SELECT source_key,account_id,agent_id FROM presence UNION
                SELECT source_key,account_id,agent_id FROM roster UNION
                SELECT source_key,account_id,agent_id FROM consultation_totals UNION
                SELECT source_key,account_id,agent_id FROM assignment_totals UNION
                SELECT source_key,account_id,agent_id FROM loss_totals
            ), seats AS (
                SELECT i.*,COALESCE(roster.agent_name,p.agent_name,r.agent_name,l.agent_name,c.agent_name,a.agent_name) AS agent_name,
                    COALESCE(r.inquiries,0) AS inquiries,COALESCE(r.replies,0) AS replies,
                    COALESCE(a.assigned_inquiries,0) AS assigned_inquiries,
                    COALESCE(l.lost_inquiries,0) AS lost_inquiries,
                    COALESCE(c.consultation_seconds,0) AS consultation_seconds,
                    COALESCE(p.online_minutes,0) AS online_minutes,COALESCE(p.busy_minutes,0) AS busy_minutes,
                    COALESCE(p.offline_minutes,0) AS offline_minutes,COALESCE(p.unknown_minutes,0) AS unknown_minutes
                    {review_seat}
                FROM identities i LEFT JOIN reply_totals r USING(source_key,account_id,agent_id)
                    LEFT JOIN presence p USING(source_key,account_id,agent_id)
                    LEFT JOIN roster USING(source_key,account_id,agent_id)
                    LEFT JOIN loss_totals l USING(source_key,account_id,agent_id)
                    LEFT JOIN consultation_totals c USING(source_key,account_id,agent_id)
                    LEFT JOIN assignment_totals a USING(source_key,account_id,agent_id)
            ), selected AS (
                SELECT * FROM seats ORDER BY inquiries DESC,agent_name NULLS LAST,source_key,account_id,agent_id LIMIT %s OFFSET %s
            ), bins AS (
                SELECT m.source_key,m.account_id,m.agent_id,{bucket} AS bucket,count(DISTINCT m.round_id) AS inquiries
                FROM replies m JOIN selected s USING(source_key,account_id,agent_id) GROUP BY 1,2,3,4
            ) SELECT (SELECT count(*) FROM seats) AS total,
                {review_total}
                (SELECT COALESCE(sum(inquiries),0) FROM seats) AS total_inquiries,
                (SELECT COALESCE(sum(replies),0) FROM seats) AS total_replies,
                (SELECT count(*) FROM lost) AS total_lost_inquiries,
                (SELECT count(*) FROM lost WHERE agent_id IS NULL) AS unassigned_lost_inquiries,
                COALESCE((SELECT jsonb_agg(to_jsonb(s) || jsonb_build_object('buckets',
                    COALESCE((SELECT jsonb_object_agg(to_char(b.bucket AT TIME ZONE 'Asia/Shanghai',
                        'YYYY-MM-DD"T"HH24:MI:SS') || '+08:00',b.inquiries) FROM bins b WHERE b.source_key=s.source_key
                        AND b.account_id=s.account_id AND b.agent_id=s.agent_id),'{{}}'::jsonb))
                    ORDER BY s.inquiries DESC,s.agent_name NULLS LAST,s.source_key,s.account_id,s.agent_id) FROM selected s),'[]'::jsonb) AS agents
        """, [period.start, min(period.end, now), now, *r_values, *r_values,
              *m_values, period.start, min(period.end, now), *s_values, retained_start, through,
              *s_values, period.end, period.start, page_size, (page - 1) * page_size]).fetchone()
        quality = reply_quality(connection, period, rows["agents"], now)
        for agent in rows["agents"]:
            agent.update(quality[(agent["source_key"], agent["account_id"], agent["agent_id"])])
            if include_reviews:
                agent['failure_rate'] = percentage(agent['failed_inquiries'], agent['inquiries'])
        return {**dict(rows), "page": page, "page_size": page_size}
