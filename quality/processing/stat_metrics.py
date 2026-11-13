"""Statistics metric registry: every report figure and archive drill-down from one definition.

A metric is a clock (which time puts a round into a period), a predicate on the
round's facts (alias f, see quality.processing.service_facts.FACT_COLUMNS; the
current review is alias rv), the seat column it is credited to and an
aggregation. Reports (live from L1/L2 facts or summed from L3 day rollups) and
the archive's "show me these rounds" filter are generated from the same entry,
so a report cell always equals the rounds the archive lists for it.

Clocks:
- reply: the round has a human reply in the period (L2 reply hours). Counted
  once per round per period: on its reply-hour row inside the period whose
  previous reply hour (prev_hour_at) is empty or before the period. A round
  replied on several days of a month counts once in the month.
- reply_sum: number of the assessed seat's replies inside the period (additive).
- handoff / assigned / started: the fact column of that time lies in the period.
- consultation: the assigned seat's consultation segments clipped to the period
  (seconds; union per seat and day, so overlapping rounds are not double counted).

Every family statement reads only the requested time ranges (index ranges), so
the live part of a report costs what its live days contain, not the period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

# Seat columns of the facts row. "responsible" = assessed seat (ICS assignment,
# NULL for assisted rounds), "assigned" = ICS-assigned seat.
SEAT_COLUMNS = {"responsible": ("responsible_agent_id", "responsible_agent_name"),
                "assigned": ("assigned_agent_id", "assigned_agent_name")}
CLOCK_COLUMNS = {"handoff": "f.handoff_at", "assigned": "f.assigned_at", "started": "f.started_at"}
# Current verdicts, one row per round (migration 020).
RATING_ELIGIBLE = "NOT EXISTS (SELECT 1 FROM quality.round_rating_review_current rating_review WHERE rating_review.round_id=f.round_id AND rating_review.status='invalid')"

REVIEW_JOIN = "LEFT JOIN quality.round_review_current rv ON rv.round_id=f.round_id"

@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    clock: str
    seat: str | None          # SEAT_COLUMNS key; None for round totals (time/AI reports)
    kind: str = "count"       # count | sum | seconds
    predicate: str = "TRUE"
    value: str | None = None  # summed expression for kind=sum
    review: bool = False

    def aggregate(self) -> str:
        if self.kind == "count":
            return f"count(*) FILTER(WHERE {self.predicate})"
        if self.kind == "sum":
            return f"COALESCE(sum({self.value}) FILTER(WHERE {self.predicate}),0)"
        raise ValueError(self.kind)


METRICS = {m.key: m for m in (
    # Seat table, reply clock: rounds with a human reply in the period, credited to the assessed seat.
    Metric("inquiries", "回复询盘总量", "reply", "responsible"),
    Metric("dialogues", "对话量", "reply", "responsible", predicate="f.visitor_messages > 0"),
    Metric("high_quality_dialogues", "高质量对话量", "reply", "responsible", predicate="f.visitor_messages > 5"),
    Metric("rated_inquiries", "已评分询盘", "reply", "responsible", predicate="f.rating IS NOT NULL AND " + RATING_ELIGIBLE),
    Metric("rating_sum", "评分合计", "reply", "responsible", "sum", "f.rating IS NOT NULL AND " + RATING_ELIGIBLE, "f.rating"),
    Metric("low_rating", "低分数（低于4分）", "reply", "responsible", predicate="f.rating < 4 AND " + RATING_ELIGIBLE),
    Metric("failed_inquiries", "人工质检不合格", "reply", "responsible", predicate="rv.human_status = 'failed'",
           review=True),
    # Same clock, rounds without an assessed seat (shown as separate totals).
    Metric("unassigned_inquiries", "无ICS分配轮次", "reply", "responsible",
           predicate="f.assigned_agent_id IS NULL"),
    Metric("assisted_inquiries", "协助轮次", "reply", "responsible", predicate="f.assisted"),
    Metric("replies", "回复条数", "reply_sum", "responsible", "sum", "TRUE", "h.assessed_replies"),
    # Seat table, other clocks, credited to the ICS-assigned seat.
    Metric("assigned_inquiries", "发配量", "assigned", "assigned"),
    Metric("lost_inquiries", "流单数", "handoff", "assigned", predicate="f.is_lost"),
    Metric("first_response_count", "首响对话数", "handoff", "assigned",
           predicate="f.first_response_seconds IS NOT NULL"),
    Metric("first_response_sum", "首响合计秒", "handoff", "assigned", "sum",
           "f.first_response_seconds IS NOT NULL", "f.first_response_seconds"),
    Metric("average_response_count", "平均响应对话数", "handoff", "assigned",
           predicate="f.average_response_seconds IS NOT NULL"),
    # Stored total seconds of the completed waits: the report mean is this sum /
    # response_samples, weighted by waits, not by rounds (R3).
    Metric("average_response_sum", "响应合计秒", "handoff", "assigned", "sum",
           "f.response_samples > 0", "f.response_seconds_total"),
    Metric("response_rounds", "参与响应平均的对话数", "handoff", "assigned", predicate="f.response_samples > 0"),
    Metric("response_samples", "已完成响应段数", "handoff", "assigned", "sum",
           "f.response_samples > 0", "f.response_samples"),
    Metric("consultation_seconds", "咨询状态总时长", "consultation", "assigned", "seconds"),
    # Round totals by start time (time report and AI quality), per channel.
    Metric("incoming", "进客量", "started", None),
    Metric("ai_only", "纯AI接待量", "started", None, predicate="f.is_ai_only"),
    Metric("handoff", "转人工量", "started", None, predicate="f.is_handoff"),
    Metric("rejected", "转人工拒接量", "started", None, predicate="f.busy_refused"),
    Metric("effective_human", "有效人工量", "started", None, predicate="f.human_replied"),
    Metric("low_rating_rounds", "低评分数（低于4分）", "started", None, predicate="f.rating < 4 AND " + RATING_ELIGIBLE),
    Metric("ai_replied_only", "纯AI回复量", "started", None, predicate="f.is_ai_only AND f.ai_replied"),
    Metric("ai_failed", "AI质检不合格", "started", None, predicate="rv.ai_status = 'failed'", review=True),
)}

# Report fields shown in tables that are derived from stored metrics; their
# archive drill-down lists the rounds of the underlying sample metric.
DRILL_ALIASES = {
    "first_response_seconds": "first_response_count", "first_response_samples": "first_response_count",
    "average_response_seconds": "average_response_count", "average_rating": "rated_inquiries",
    "failure_rate": "failed_inquiries", "low_rating_seat": "low_rating", "total_rounds": "incoming",
    "handoff_failed": "rejected", "failed_rounds": "ai_failed", "low_rating_time": "low_rating_rounds",
    "total_lost_inquiries": "lost_inquiries", "unassigned_lost_inquiries": "lost_inquiries",
}
SEAT_FAMILIES = ("reply", "reply_sum", "handoff", "assigned", "consultation")


def family_metrics(clock: str) -> list[Metric]:
    return [m for m in METRICS.values() if m.clock == clock]


def drill_metric(key: str) -> Metric:
    return METRICS[DRILL_ALIASES.get(key, key)]


# ---------------------------------------------------------------- calendar helpers
def day_start(day: date) -> datetime:
    return datetime.combine(day, time(), BEIJING)


def beijing_day(value: datetime) -> date:
    return value.astimezone(BEIJING).date()


def days_between(start: datetime, end: datetime) -> list[date]:
    """Beijing days overlapping [start, end)."""
    if end <= start:
        return []
    first, last = beijing_day(start), beijing_day(end - timedelta(microseconds=1))
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def bucket_sql(column: str, grain: str) -> str:
    if grain not in {"minute", "hour", "day", "month"}:
        raise ValueError("Unsupported reporting grain")
    return f"date_trunc('{grain}', {column} AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'"


def day_sql(column: str) -> str:
    return f"({column} AT TIME ZONE 'Asia/Shanghai')::date"


# ---------------------------------------------------------------- family SQL
def _scope(scope) -> tuple[str, list]:
    if scope is None:
        return "TRUE", []
    return scope[0].replace("{a}", "f"), list(scope[1])


def clip_ranges(start: datetime, end: datetime, ranges=None) -> list[tuple[datetime, datetime]]:
    """[start, end) or the parts of `ranges` inside it, merged and in order."""
    parts = sorted((max(a, start), min(b, end)) for a, b in (ranges if ranges is not None else [(start, end)]))
    merged: list[list[datetime]] = []
    for a, b in parts:
        if a >= b:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def ranges_sql(column: str, ranges) -> tuple[str, list]:
    """OR of half-open index ranges on `column`."""
    params: list = []
    for a, b in ranges:
        params.extend([a, b])
    return "(" + " OR ".join(f"({column} >= %s AND {column} < %s)" for _ in ranges) + ")", params


def family_sql(src, clock: str, start: datetime, end: datetime, *, scope=None, grain: str | None = None,
               ranges=None, by_channel: bool = False, repeats: bool = False):
    """One aggregation statement for every registered metric of `clock` in the period [start, end).

    Rows: source_key, account_id, [agent_id, agent_name], [channel_key], [bucket], [prev_hour_at], metrics.
    - grain: also group per hour/day/month bucket (a round counts once per bucket).
    - ranges: only contributions inside these parts of the period (default: all of it).
      The period still decides "once per period": a reply row counts only when the
      round had no reply earlier in the period (or bucket).
    - repeats (reply clock): instead, the rows counted for the period that follow an
      earlier reply of the round, grouped by that earlier reply hour (day rollups).
    scope: ("SQL with {a} for the facts alias", params) restricting source/account/channel.
    Returns (sql, params), or None when the ranges are empty.
    """
    parts = clip_ranges(start, end, ranges)
    if not parts:
        return None
    scope_sql, scope_params = _scope(scope)
    metrics = family_metrics(clock)
    seat = SEAT_COLUMNS[metrics[0].seat] if metrics[0].seat else None
    groups, selects = ["f.source_key", "f.account_id"], ["f.source_key", "f.account_id"]
    if seat:
        selects += [f"COALESCE(f.{seat[0]},'') AS agent_id", f"max(f.{seat[1]}) AS agent_name"]
        groups.append(f"COALESCE(f.{seat[0]},'')")
    if by_channel:
        selects.append("COALESCE(f.channel_key,'') AS channel_key")
        groups.append("COALESCE(f.channel_key,'')")
    review = REVIEW_JOIN if any(m.review for m in metrics) else ""
    if clock in ("reply", "reply_sum"):
        window, window_params = ranges_sql("h.hour_at", parts)
        columns = "h.round_id,h.hour_at,h.assessed_replies,h.prev_hour_at"
        if grain:
            columns += f",{bucket_sql('h.hour_at', grain)} AS bucket"
        if clock == "reply":
            # The period's (or bucket's) first reply row of each round.
            first = f"GREATEST(%s::timestamptz,{bucket_sql('h.hour_at', grain)})" if grain else "%s::timestamptz"
            if repeats:
                window += f" AND h.prev_hour_at IS NOT NULL AND h.prev_hour_at < {first}"
            else:
                window += f" AND (h.prev_hour_at IS NULL OR h.prev_hour_at < {first})"
            window_params.append(start)
        hours, hour_params = src.hours(window, window_params)
        rr = f"SELECT {columns} FROM {hours}"
        facts, fact_params = src.facts(
            f"({scope_sql}) AND f.last_reply_at >= %s AND f.round_id IN (SELECT round_id FROM rr)",
            [*scope_params, parts[0][0]])
        if grain:
            selects.append("rr.bucket")
            groups.append("rr.bucket")
        if repeats:
            selects.append("rr.prev_hour_at")
            groups.append("rr.prev_hour_at")
        aggregates = [m.aggregate().replace("h.", "rr.") + f" AS {m.key}" for m in metrics]
        sql = f"""WITH rr AS MATERIALIZED ({rr})
            SELECT {', '.join(selects + aggregates)}
            FROM rr JOIN {facts} ON f.round_id=rr.round_id {review}
            GROUP BY {', '.join(groups)}"""
        return sql, [*hour_params, *fact_params]
    if clock == "consultation":
        seat_id, seat_name = SEAT_COLUMNS["assigned"]
        # Segments end at most at their upper bound and start no earlier than the round.
        bounds = " OR ".join("(upper(f.consultation_spans) > %s AND f.started_at < %s)" for _ in parts)
        facts, fact_params = src.facts(
            f"({scope_sql}) AND f.{seat_id} IS NOT NULL AND ({bounds})"
            " AND f.consultation_spans && tstzrange(%s,%s,'[)')",
            [*scope_params, *[value for part in parts for value in part], start, end])
        outer_select = "source_key,account_id,agent_id,max(agent_name) AS agent_name"
        outer_group = "source_key,account_id,agent_id"
        sql = f"""WITH spans AS MATERIALIZED (
                SELECT f.source_key,f.account_id,f.{seat_id} AS agent_id,f.{seat_name} AS agent_name,
                    span * tstzrange(%s,%s,'[)') AS span
                FROM {facts} CROSS JOIN LATERAL unnest(f.consultation_spans) AS span
                WHERE span && tstzrange(%s,%s,'[)')
            ), pieces AS (
                SELECT s.source_key,s.account_id,s.agent_id,s.agent_name,d.day,
                    s.span * tstzrange(d.day::timestamp AT TIME ZONE 'Asia/Shanghai',
                        (d.day+1)::timestamp AT TIME ZONE 'Asia/Shanghai','[)')
                        * tstzrange(w.a,w.b,'[)') AS piece
                FROM spans s CROSS JOIN LATERAL (
                    SELECT generate_series({day_sql('lower(s.span)')},
                        {day_sql("(upper(s.span) - interval '1 microsecond')")},interval '1 day')::date AS day) d
                JOIN unnest(%s::timestamptz[],%s::timestamptz[]) AS w(a,b) ON s.span && tstzrange(w.a,w.b,'[)')
                WHERE NOT isempty(s.span)
            ), merged AS (
                SELECT source_key,account_id,agent_id,max(agent_name) AS agent_name,day,range_agg(piece) AS ranges
                FROM pieces WHERE NOT isempty(piece) GROUP BY source_key,account_id,agent_id,day
            ) SELECT {outer_select},
                sum((SELECT COALESCE(sum(extract(epoch FROM upper(r)-lower(r))),0) FROM unnest(ranges) r))
                    ::double precision AS consultation_seconds
            FROM merged GROUP BY {outer_group}"""
        params = [start, end, *fact_params, start, end, [a for a, _ in parts], [b for _, b in parts]]
        return sql, params
    column = CLOCK_COLUMNS[clock]
    window, window_params = ranges_sql(column, parts)
    facts, fact_params = src.facts(f"({scope_sql}) AND {window}", [*scope_params, *window_params])
    if grain:
        selects.append(f"{bucket_sql(column, grain)} AS bucket")
        groups.append(bucket_sql(column, grain))
    aggregates = [f"{m.aggregate()} AS {m.key}" for m in metrics]
    sql = f"""SELECT {', '.join(selects + aggregates)} FROM {facts} {review}
        GROUP BY {', '.join(groups)}"""
    return sql, fact_params


def run_family(connection, src, clock, start, end, **kwargs) -> list[dict]:
    if end <= start:
        return []
    statement = family_sql(src, clock, start, end, **kwargs)
    if statement is None:
        return []
    sql, params = statement
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- archive drill-down
def drill_selection(src, metric: Metric, start: datetime | None, end: datetime | None,
                    seat: str | None = None, scope=None) -> tuple[str, list]:
    """SELECT of the round_ids counted by `metric` in [start, end), from the same facts
    (and reply hours) the reports read. seat: credited seat id; '' = rounds without
    that seat; '*' = rounds credited to any seat (seat totals); None = every round.

    Report periods and buckets start on whole hours, where the reply-hour rows give
    exactly the rounds the report counted. Each selection is bounded by indexed
    fact columns first (seat, first/last reply, segment end) so a long period does
    not read every reply hour of the period.
    """
    clauses, params = [], []
    scope_sql, scope_params = _scope(scope)
    clauses.append(f"({scope_sql})")
    params.extend(scope_params)
    if metric.clock in ("reply", "reply_sum"):
        window, window_params = [], []
        if start is not None:
            window.append("h.hour_at >= %s")
            window_params.append(start)
            clauses.append("f.last_reply_at >= %s")
            params.append(start)
        if end is not None:
            window.append("h.hour_at < %s")
            window_params.append(end)
            clauses.append("f.first_reply_at < %s")
            params.append(end)
        if metric.clock == "reply_sum":
            window.append("h.assessed_replies > 0")
        hours, hour_params = src.hours(" AND ".join(window) or "TRUE", window_params)
        if metric.clock == "reply" and start is not None and end is not None:
            # A first or last reply inside the window already proves a reply hour in it;
            # only rounds replied before and after the window need their hours checked.
            clauses.append(f"(f.first_reply_at >= %s OR f.last_reply_at < %s"
                           f" OR f.round_id IN (SELECT h.round_id FROM {hours}))")
            params.extend([start, end, *hour_params])
        else:
            clauses.append(f"f.round_id IN (SELECT h.round_id FROM {hours})")
            params.extend(hour_params)
    elif metric.clock == "consultation":
        clauses.append(f"f.{SEAT_COLUMNS['assigned'][0]} IS NOT NULL")
        if start is not None:
            clauses.append("upper(f.consultation_spans) > %s")
            params.append(start)
        if end is not None:
            clauses.append("f.started_at < %s")
            params.append(end)
        clauses.append("f.consultation_spans && tstzrange(%s,%s,'[)')")
        params.extend([start, end])
    else:
        column = CLOCK_COLUMNS[metric.clock]
        if start is not None:
            clauses.append(f"{column} >= %s")
            params.append(start)
        if end is not None:
            clauses.append(f"{column} < %s")
            params.append(end)
    if metric.predicate != "TRUE":
        clauses.append(f"({metric.predicate})")
    if seat is not None and metric.seat:
        column = SEAT_COLUMNS[metric.seat][0]
        if seat == "":
            clauses.append(f"f.{column} IS NULL")
        elif seat == "*":  # credited to some seat: the report's seat totals
            clauses.append(f"f.{column} IS NOT NULL")
        else:
            clauses.append(f"f.{column} = %s")
            params.append(seat)
    facts, fact_params = src.facts()
    review = REVIEW_JOIN if metric.review else ""
    return (f"SELECT f.round_id FROM {facts} {review} WHERE {' AND '.join(clauses)}", [*fact_params, *params])
