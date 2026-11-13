"""Read-only aggregates over quality's existing pool and archive projections.

Every figure comes from the metric registry (quality.processing.stat_metrics)
over the per-round facts, summed from day rollups where they are valid
(quality.processing.rollups.Composer). The archive drill-down of any cell uses
the same registry entry, clock window and seat, so it lists exactly the rounds
counted here.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from quality.presence.retention import retention_metadata
from quality.processing.budget import BudgetExceeded, MaintenanceBudget, classify
from quality.processing.rollups import Composer, day_ranges
from quality.processing.stat_metrics import day_start, days_between
from quality.processing.service_facts import BUSY_NOTICE  # noqa: F401  (kept for callers/tests)
from quality.processing.stat_metrics import bucket_sql
from .periods import BEIJING
from .quality_reports import percentage
from . import users as user_ranking
from .queue_counts import attach_queue_counts

REPORT_BUDGET_SECONDS = 20


class ReportBudgetExceeded(BudgetExceeded):
    """统计数据库读取阶段已用完预算。"""


def report_budget(seconds=REPORT_BUDGET_SECONDS, *, abort_event=None, statement_seconds=8, **kwargs):
    """交互读取默认 20 秒；导出由调用方传入剩余时间，清理时间另计。"""
    return MaintenanceBudget(seconds, statement_seconds=statement_seconds, connect_seconds=1,
                             abort_event=abort_event, error_type=ReportBudgetExceeded, **kwargs)


def read_only_snapshot(connection):
    """必须先设置事务隔离级别，再由包装连接设置逐语句时限。"""
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

TIME_METRICS = {"incoming": "incoming", "ai_only": "ai_only", "handoff": "handoff", "rejected": "rejected",
                "effective_human": "effective_human", "low_rating": "low_rating_rounds"}
METRICS = tuple(TIME_METRICS)
SEAT_VALUE_KEYS = ("inquiries", "replies", "dialogues", "high_quality_dialogues", "rated_inquiries", "rating_sum",
                   "low_rating", "failed_inquiries", "assigned_inquiries", "lost_inquiries", "first_response_count",
                   "first_response_sum", "average_response_count", "average_response_sum", "response_rounds",
                   "response_samples", "consultation_seconds")


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


def _number(value):
    if value is None:
        return 0
    return int(value) if float(value).is_integer() else float(value)


def seat_figures(item: dict) -> dict:
    """Displayed seat fields from stored sums and counts (exact means)."""
    first_count, samples = int(item.get("first_response_count") or 0), int(item.get("response_samples") or 0)
    rated = int(item.get("rated_inquiries") or 0)
    return {
        "inquiries": int(item.get("inquiries") or 0), "replies": int(item.get("replies") or 0),
        "assigned_inquiries": int(item.get("assigned_inquiries") or 0),
        "lost_inquiries": int(item.get("lost_inquiries") or 0),
        "consultation_seconds": float(item.get("consultation_seconds") or 0),
        "first_response_seconds": float(item["first_response_sum"]) / first_count if first_count else None,
        "first_response_samples": first_count,
        # Weighted by waits: total seconds of all completed waits / their number (R3).
        "average_response_seconds": float(item["average_response_sum"]) / samples if samples else None,
        "response_samples": samples,
        "response_rounds": int(item.get("response_rounds") or 0),
        "average_rating": float(item["rating_sum"]) / rated if rated else None,
        "rated_inquiries": rated, "low_rating": int(item.get("low_rating") or 0),
        "dialogues": int(item.get("dialogues") or 0),
        "high_quality_dialogues": int(item.get("high_quality_dialogues") or 0),
        "failed_inquiries": int(item.get("failed_inquiries") or 0),
    }


class StatisticsRepository:
    def __init__(self, pool):
        self.pool = pool

    @contextmanager
    def _read(self, budget):
        with budget.connection(self.pool, setup=read_only_snapshot) as connection:
            yield connection

    @staticmethod
    def composer(connection, period, now, **scope):
        # 任意区间（含自定义、R10 带时刻）：整天读日汇总，其余（今天、未建/脏日、首尾不足一天）只算这些时段。
        return Composer(connection, period.start, period.end, now, **scope)

    def time_report(self, period, *, source_key=None, account_id=None, channel_key=None, now=None, budget=None):
        budget = budget or report_budget()
        now = now or datetime.now(timezone.utc)
        with self._read(budget) as connection:
            rows = self.composer(connection, period, now, source_key=source_key, account_id=account_id,
                                 channel_key=channel_key).round_rows(period.grain) if period.start < now else []
            # Both values come from small partial indexes (migration 011); the
            # completed history grows with every archived event.
            processing = connection.execute("""SELECT
                (SELECT count(*) FROM ops.event_jobs WHERE status IN ('queued','retry','processing','failed')) AS pending_jobs,
                (SELECT max(completed_at) FROM ops.event_jobs WHERE completed_at IS NOT NULL) AS last_processed_at""").fetchone()
        indexed = {b["key"]: {"key": b["key"], **dict.fromkeys(METRICS, 0), "channels": {}} for b in period.buckets}
        totals = dict.fromkeys(METRICS, 0)
        channels = {}
        for row in rows:
            item = indexed[row["bucket"].astimezone(BEIJING).isoformat()]
            channel = row["channel_key"] or "unmapped"
            incoming = int(row.get("incoming") or 0)
            item["channels"][channel] = item["channels"].get(channel, 0) + incoming
            channels[channel] = channels.get(channel, 0) + incoming
            for metric, key in TIME_METRICS.items():
                value = int(row.get(key) or 0)
                item[metric] += value
                totals[metric] += value
        return {**period.metadata(now), "rows": list(indexed.values()), "totals": totals,
                "channels": channels, "processing": dict(processing), "as_of": now.isoformat()}

    def agent_report(self, period, *, source_key=None, account_id=None, page=1, page_size=20, now=None, budget=None):
        budget = budget or report_budget()
        now = now or datetime.now(timezone.utc)
        where, values = scope_sql("s", source_key, account_id)
        retention = retention_metadata(now)
        retained_start = max(period.start, datetime.fromisoformat(retention["retention_cutoff_at"]))
        through = min(period.end, now.replace(second=0, microsecond=0))
        grain = "minute" if period.mode == "day" else period.grain
        bucket = bucket_sql("s.sampled_minute", grain)
        with self._read(budget) as connection:
            # Missing minutes remain absent; no last-known state carry-forward.
            # Per-minute counts of all seats are precomputed (ops.presence_minutes, migration 020).
            observed = connection.execute(f"""
                WITH minutes AS (
                    SELECT {bucket} AS bucket,s.sampled_minute,
                        sum(s.online) AS online,sum(s.busy) AS busy,sum(s.offline) AS offline,
                        sum(s.unknown) AS unknown,sum(s.samples) AS samples
                    FROM ops.presence_minutes s WHERE {where}
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
        budget.check()
        presence = self._presence(period, observed, periods, retained_start, through, now)
        budget.check()
        return {**period.metadata(now), **retention, **presence, **agents}

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
                for offset in range(int((b["end"] - b["start"]).total_seconds() // 60)):
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

    def quality_report(self, period, *, source_key=None, account_id=None, page=1, page_size=20, now=None, budget=None):
        budget = budget or report_budget()
        now = now or datetime.now(timezone.utc)
        retained_start = max(period.start, datetime.fromisoformat(retention_metadata(now)["retention_cutoff_at"]))
        through = min(period.end, now.replace(second=0, microsecond=0))
        with self._read(budget) as connection:
            composer = self.composer(connection, period, now, source_key=source_key, account_id=account_id)
            human = self._agents(connection, period, retained_start, through, source_key, account_id,
                page, page_size, now, include_reviews=True, composer=composer, include_buckets=False)
            rows = composer.round_rows(None) if period.start < now else []
        total = lambda key: sum(int(row.get(key) or 0) for row in rows)
        ai = {"total_rounds": total("incoming"), "handoff": total("handoff"), "ai_only": total("ai_replied_only"),
              "handoff_failed": total("rejected"), "failed_rounds": total("ai_failed")}
        ai["failure_rate"] = percentage(ai["failed_rounds"], ai["total_rounds"])
        return {**period.metadata(now), **human, "ai": ai,
            "human_failure_rate": percentage(human['total_failed_inquiries'], human['total_inquiries'])}

    def user_report(self, period, *, source_key=None, account_id=None, channel_key=None, page=1, page_size=20,
                    now=None, full_ranking=False, budget=None):
        """R13 用户成功转人工排行（前 100 名），口径同发配量，见 statistics/users.py。"""
        budget = budget or report_budget()
        with self._read(budget) as connection:
            report = user_ranking.user_report(connection, period, source_key=source_key, account_id=account_id,
                                             channel_key=channel_key, page=page, page_size=page_size, now=now,
                                             full_ranking=full_ranking)
        return attach_queue_counts(report, budget)

    def user_detail(self, period, *, source_key, account_id, contact_id, channel_key=None, now=None, budget=None):
        """R13 某个用户区间内每一次成功转人工的明细。"""
        budget = budget or report_budget()
        with self._read(budget) as connection:
            return user_ranking.user_detail(connection, period, source_key=source_key, account_id=account_id,
                                            contact_id=contact_id, channel_key=channel_key, now=now)

    @staticmethod
    def _presence_minutes(connection, s_where, s_values, start, end):
        """Minutes per seat and status in [start, end): whole Beijing days from ops.presence_agent_days
        (migration 020), the partial first/last day from the samples themselves."""
        days = [day for day in days_between(start, end)
                if day_start(day) >= start and day_start(day + timedelta(days=1)) <= end]
        raw = day_ranges([day for day in days_between(start, end) if day not in days], start, end)
        parts, params = [], []
        if days:
            parts.append(f"""SELECT s.source_key,s.account_id,s.agent_id,s.agent_name,
                    s.online_minutes,s.busy_minutes,s.offline_minutes,s.unknown_minutes
                FROM ops.presence_agent_days s WHERE {s_where} AND s.day = ANY(%s::date[])""")
            params += [*s_values, days]
        for a, b in raw:
            parts.append(f"""SELECT s.source_key,s.account_id,s.agent_id,max(s.agent_name),
                    count(*) FILTER(WHERE availability_status='online'),
                    count(*) FILTER(WHERE availability_status='busy'),
                    count(*) FILTER(WHERE availability_status='offline'),
                    count(*) FILTER(WHERE availability_status='unknown')
                FROM archive.agent_presence_samples s WHERE {s_where}
                    AND s.sampled_minute >= %s AND s.sampled_minute < %s GROUP BY 1,2,3""")
            params += [*s_values, a, b]
        if not parts:
            return []
        return connection.execute(f"""
            SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,
                sum(online_minutes) AS online_minutes,sum(busy_minutes) AS busy_minutes,
                sum(offline_minutes) AS offline_minutes,sum(unknown_minutes) AS unknown_minutes
            FROM ({' UNION ALL '.join(parts)}) p(source_key,account_id,agent_id,agent_name,online_minutes,busy_minutes,
                offline_minutes,unknown_minutes)
            GROUP BY 1,2,3""", params).fetchall()

    def _agents(self, connection, period, retained_start, through, source, account, page, page_size, now,
                include_reviews=False, *, composer=None, include_buckets=True):
        """Seat table of the selected period: registry metrics per seat, presence and roster.

        Rounds are credited only to their ICS-assigned seat (assessed seat for
        reply-clock metrics, which excludes assisted rounds). Rounds without such
        a seat are returned as totals (unassigned_inquiries, assisted_inquiries,
        unassigned_lost_inquiries) instead of being attributed to anyone.
        """
        s_where, s_values = scope_sql("s", source, account)
        if composer is None:
            composer = self.composer(connection, period, now, source_key=source, account_id=account)
        totals = composer.seat_totals() if period.start < now else {}
        presence = self._presence_minutes(connection, s_where, s_values, retained_start, through) \
            if retained_start < through else []
        roster = connection.execute(f"""
            SELECT s.source_key,s.account_id,s.agent_id,s.agent_name FROM ops.agent_presence_registry s
            WHERE {s_where} AND s.first_seen_at < %s AND (s.removed_at IS NULL OR s.removed_at >= %s)
        """, [*s_values, period.end, period.start]).fetchall()
        key = lambda row: (row["source_key"], int(row["account_id"]), str(row["agent_id"]))
        seats: dict = {}
        unseated = {"unassigned_inquiries": 0, "assisted_inquiries": 0, "unassigned_lost_inquiries": 0}
        total_lost = 0
        for (source_key, account_id, agent_id), item in totals.items():
            total_lost += int(item.get("lost_inquiries") or 0)
            unseated["unassigned_inquiries"] += int(item.get("unassigned_inquiries") or 0)
            unseated["assisted_inquiries"] += int(item.get("assisted_inquiries") or 0)
            if agent_id == "":
                unseated["unassigned_lost_inquiries"] += int(item.get("lost_inquiries") or 0)
                continue
            if not any(item.get(name) for name in SEAT_VALUE_KEYS):
                continue
            seats[(source_key, int(account_id), agent_id)] = {"agent_name": item.get("agent_name"), **seat_figures(item)}
        for row in presence:
            seat = seats.setdefault(key(row), {"agent_name": row["agent_name"], **seat_figures({})})
            seat.update({name: int(row[name]) for name in ("online_minutes", "busy_minutes", "offline_minutes",
                                                            "unknown_minutes")})
            seat["agent_name"] = row["agent_name"] or seat.get("agent_name")
        for row in roster:
            seat = seats.setdefault(key(row), {"agent_name": row["agent_name"], **seat_figures({})})
            seat["agent_name"] = row["agent_name"] or seat.get("agent_name")
        agents = []
        for (source_key, account_id, agent_id), seat in seats.items():
            item = {"source_key": source_key, "account_id": account_id, "agent_id": agent_id,
                    "online_minutes": 0, "busy_minutes": 0, "offline_minutes": 0, "unknown_minutes": 0, **seat}
            if not include_reviews:
                item.pop("failed_inquiries", None)
            agents.append(item)
        agents.sort(key=lambda a: (-a["inquiries"], a["agent_name"] is None, a["agent_name"] or "",
                                   a["source_key"], a["account_id"], a["agent_id"]))
        selected = agents[(page - 1) * page_size: page * page_size]
        selected_seats = None if len(selected) == len(agents) else [
            (a["source_key"], a["account_id"], a["agent_id"]) for a in selected]
        buckets = composer.seat_buckets(period.grain, period.buckets, seats=selected_seats) \
            if include_buckets and selected and period.start < now else {}
        for agent in selected:
            if include_buckets:
                agent["buckets"] = buckets.get((agent["source_key"], agent["account_id"], agent["agent_id"]), {})
            if include_reviews:
                agent["failure_rate"] = percentage(agent["failed_inquiries"], agent["inquiries"])
        result = {"total": len(agents), "agents": selected,
                  "total_inquiries": sum(a["inquiries"] for a in agents),
                  "total_replies": sum(a["replies"] for a in agents),
                  "total_lost_inquiries": total_lost, **unseated,
                  "page": page, "page_size": page_size, "as_of": now.isoformat()}
        if include_reviews:
            result["total_failed_inquiries"] = sum(a["failed_inquiries"] for a in agents)
        return result
