"""Day rollups (L3) of the statistics metrics: building by the processor, reading by reports.

Every number stored here comes from quality.processing.stat_metrics.family_sql
over the per-round facts, the same SQL the reports run live, so a report gives
identical results whether a day is read from its rollup or computed live:

- reply clock metrics (counted once per round per period) are stored per day for
  the rounds first replied on that day (stat_seat_days, variant d), plus the
  "repeats" among them: rounds that had replied on an earlier day, keyed by that
  earlier reply hour (stat_seat_repeats). Any period [A, B) of whole days is the
  sum of its day rows minus the repeats whose earlier reply is at or after A;
  a month bucket subtracts the repeats since the bucket start.
- additive clocks (reply count, assignment, handoff, consultation, start time)
  are stored once per day; any period is the sum of its days.
- R13 successful assignments per day and user (stat_user_days, statistics/users.py).

Reports read a day from the rollup only while it is built under the current
rule versions, after the day ended, not later than the report's "now", and not
marked dirty; all other days (today, dirty or unbuilt days, partial edge days of
a window with hours) are computed live from those days' facts only.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import time

from .budget import BudgetExceeded, MaintenanceBudget
from .service_facts import FACT_VERSION, FactSource
from .stat_metrics import BEIJING, beijing_day, day_start, days_between, family_metrics, run_family

logger = logging.getLogger(__name__)

# Bump when the rollup SQL or its tables change; days built earlier are rebuilt.
# 2 (2026-11-12, migration 020): day rows + repeats replace the month/year variants;
#    user (R13) day rollups.
ROLLUP_VERSION = 3
DAY_LOCK = 175608
REPLY_KEYS = tuple(m.key for m in family_metrics("reply"))
SEAT_ADDITIVE_KEYS = tuple(m.key for clock in ("reply_sum", "handoff", "assigned", "consultation")
                           for m in family_metrics(clock))
SEAT_KEYS = REPLY_KEYS + SEAT_ADDITIVE_KEYS
ROUND_KEYS = tuple(m.key for m in family_metrics("started"))


# R13 successful ICS assignments (same rule as the seat table's assignment count):
# ICS-confirmed assignment with a seat, counted at its assignment time. The time
# expression is literally the one of the partial index round_index_assigned_idx.
ASSIGNED_TS = "ops.archive_ts(r.data->>'responsible_assigned_at')"
ASSIGNED_WHERE = ("r.data->>'responsibility_basis' = 'ics_assignment'"
                  " AND NULLIF(r.data->>'responsible_agent_id','') IS NOT NULL")


def user_rows_sql(ranges, scope: str = "TRUE", scope_params=()) -> tuple[str, list]:
    """Assignments in the time ranges per source, account, channel ('' unmapped), contact
    ('' unknown) and customer row: the day rollup rows (stat_user_days) and the live part
    of statistics/users.py use this one statement. scope is SQL on alias r."""
    window = " OR ".join(f"({ASSIGNED_TS} >= %s AND {ASSIGNED_TS} < %s)" for _ in ranges) or "FALSE"
    return (f"""
        SELECT r.source_key,r.account_id,COALESCE(r.channel_key,'') AS channel_key,
            COALESCE(NULLIF(c.contact_id,''),'') AS contact_id,r.customer_id,
            count(*) AS assignments,
            array_agg(DISTINCT r.conversation_id ORDER BY r.conversation_id) AS conversations,
            min({ASSIGNED_TS}) AS first_assigned_at,max({ASSIGNED_TS}) AS last_assigned_at
        FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id
        WHERE {ASSIGNED_WHERE} AND ({window}) AND ({scope})
        GROUP BY 1,2,3,4,5""", [*[value for part in ranges for value in part], *scope_params])


def user_rows(connection, ranges, scope: str = "TRUE", scope_params=()) -> list[dict]:
    if not ranges:
        return []
    sql, params = user_rows_sql(ranges, scope, scope_params)
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _merge(target: dict, rows, keys, key_columns, sign: int = 1):
    for row in rows:
        key = tuple(str(row[column]) if column == "agent_id" else row[column] for column in key_columns)
        item = target.setdefault(key, {column: row[column] for column in key_columns})
        if row.get("agent_name") and not item.get("agent_name"):
            item["agent_name"] = row["agent_name"]
        for name in keys:
            if name in row and row[name] is not None:
                item[name] = (item.get(name) or 0) + sign * row[name]
    return target


def _seat_scope(source_key, account_id):
    clauses, params = [], []
    if source_key is not None:
        clauses.append("{a}.source_key=%s")
        params.append(source_key)
    if account_id is not None:
        clauses.append("{a}.account_id=%s")
        params.append(account_id)
    return " AND ".join(clauses) or "TRUE", params


def _selected_seats(alias, seats):
    """按来源、账号、坐席三元组筛选，避免同名坐席跨来源或账号串入。"""
    return (f"({alias}.source_key,{alias}.account_id,{alias}.agent_id) IN "
            "(SELECT * FROM unnest(%s::text[],%s::bigint[],%s::text[]))",
            [[seat[index] for seat in seats] for index in range(3)])


def round_scope(source_key=None, account_id=None, channel_key=None):
    sql, params = _seat_scope(source_key, account_id)
    if channel_key is not None:
        if channel_key == "unmapped":
            sql += " AND {a}.channel_key IS NULL"
        else:
            sql += " AND {a}.channel_key=%s"
            params.append(channel_key)
    return sql, params


def clean_days(connection, days, now) -> set[date]:
    """Days of `days` whose rollups are valid for a report evaluated at `now`."""
    if not days:
        return set()
    rows = connection.execute("""
        SELECT d.day FROM ops.stat_rollup_days d
        WHERE d.day = ANY(%s::date[]) AND d.state='built' AND d.fact_version=%s AND d.rollup_version=%s
            AND d.built_at <= %s AND d.built_at >= ((d.day+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND NOT EXISTS (SELECT 1 FROM ops.stat_dirty_days x WHERE x.day=d.day)
    """, (list(days), FACT_VERSION, ROLLUP_VERSION, now)).fetchall()
    return {row["day"] for row in rows}


def day_ranges(days, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Beijing days clipped to [start, end), adjacent days merged."""
    ranges: list[list[datetime]] = []
    for day in sorted(days):
        a, b = max(day_start(day), start), min(day_start(day + timedelta(days=1)), end)
        if a >= b:
            continue
        if ranges and ranges[-1][1] == a:
            ranges[-1][1] = b
        else:
            ranges.append([a, b])
    return [(a, b) for a, b in ranges]


class Composer:
    """Period figures from clean day rollups plus live computation for the remaining days."""

    def __init__(self, connection, start: datetime, end: datetime, now: datetime, *,
                 source_key=None, account_id=None, channel_key=None, period_variant=None):
        # period_variant is accepted for older callers; rollups no longer depend on it.
        self.connection = connection
        self.start, self.end, self.now = start, min(end, now), now
        self.days = days_between(self.start, self.end)
        # A day rollup covers the whole Beijing day; days only partly inside the window
        # (R10 hour refinement) are always computed live.
        full_days = [day for day in self.days if day_start(day) >= start and day_start(day + timedelta(days=1)) <= end]
        self.clean = clean_days(connection, full_days, now) if full_days else set()
        self.live_days = [day for day in self.days if day not in self.clean]
        self.live_ranges = day_ranges(self.live_days, self.start, self.end)
        self.seat_scope = _seat_scope(source_key, account_id)
        self.round_scope = round_scope(source_key, account_id, channel_key)
        self.channel_key = channel_key
        self.source_key, self.account_id = source_key, account_id
        self._source = None

    @property
    def source(self) -> FactSource:
        """Facts for live computation, loaded once (only rounds touching live days need live facts)."""
        if self._source is None:
            live_start = self.live_ranges[0][0] if self.live_ranges else self.start
            scope = self.round_scope[0].replace("{a}", "r")
            self._source = FactSource.load(self.connection, self.now, start=live_start,
                                           end=self.end, scope=scope, scope_params=self.round_scope[1])
        return self._source

    def _rollup_seats(self, keys, days, by_day=False, seats=None):
        if not days or not keys:
            return []
        scope, params = self.seat_scope
        scope = scope.replace("{a}", "s")
        if seats is not None:
            selected, selected_params = _selected_seats("s", seats)
            scope += f" AND {selected}"
            params = [*params, *selected_params]
        group = "s.source_key,s.account_id,s.agent_id" + (",s.day" if by_day else "")
        select = "s.source_key,s.account_id,s.agent_id" + (",s.day AS bucket" if by_day else "")
        sums = ",".join(f"sum(s.{key}) AS {key}" for key in keys)
        return [dict(row) for row in self.connection.execute(f"""
            SELECT {select},max(s.agent_name) AS agent_name,{sums}
            FROM ops.stat_seat_days s WHERE s.day = ANY(%s::date[]) AND s.variant='d' AND {scope}
            GROUP BY {group}""", [list(days), *params]).fetchall()]

    def _rollup_repeats(self, keys, days, grain=None, seats=None):
        """Repeats of clean days already counted earlier in the period (or month bucket)."""
        if not days:
            return []
        scope, params = self.seat_scope
        scope = scope.replace("{a}", "s")
        if seats is not None:
            selected, selected_params = _selected_seats("s", seats)
            scope += f" AND {selected}"
            params = [*params, *selected_params]
        since = "%s::timestamptz"
        if grain == "month":
            since = ("GREATEST(%s::timestamptz,"
                     "date_trunc('month',s.day::timestamp) AT TIME ZONE 'Asia/Shanghai')")
        bucket = ",date_trunc('month',s.day::timestamp)::date AS bucket" if grain == "month" else ""
        group = "s.source_key,s.account_id,s.agent_id" + (",date_trunc('month',s.day::timestamp)" if grain else "")
        sums = ",".join(f"sum(s.{key}) AS {key}" for key in keys)
        return [dict(row) for row in self.connection.execute(f"""
            SELECT s.source_key,s.account_id,s.agent_id{bucket},max(s.agent_name) AS agent_name,{sums}
            FROM ops.stat_seat_repeats s WHERE s.day = ANY(%s::date[]) AND {scope} AND s.prev_hour_at >= {since}
            GROUP BY {group}""", [list(days), *params, self.start]).fetchall()]

    def _live(self, clock, **kwargs):
        if not self.live_ranges:
            return []
        return run_family(self.connection, self.source, clock, self.start, self.end, ranges=self.live_ranges,
                          **kwargs)

    # ------------------------------------------------------------- seat table
    def seat_totals(self) -> dict:
        """{(source_key, account_id, agent_id): {metric: value}} for the whole period ('' = no seat)."""
        result: dict = {}
        key_columns = ("source_key", "account_id", "agent_id")
        clean = sorted(self.clean)
        _merge(result, self._rollup_seats(SEAT_KEYS, clean), SEAT_KEYS, key_columns)
        _merge(result, self._rollup_repeats(REPLY_KEYS, clean), REPLY_KEYS, key_columns, sign=-1)
        _merge(result, self._live("reply", scope=self.seat_scope), REPLY_KEYS, key_columns)
        for clock in ("reply_sum", "handoff", "assigned", "consultation"):
            _merge(result, self._live(clock, scope=self.seat_scope), [m.key for m in family_metrics(clock)],
                   key_columns)
        return result

    def seat_buckets(self, grain: str, period_buckets, seats=None) -> dict:
        """本页坐席的分桶询盘数；seats=None 保留全量路径，每轮在同桶只计一次。"""
        if seats is not None and not seats:
            return {}
        result: dict = {}
        key_columns = ("source_key", "account_id", "agent_id", "bucket")
        if grain == "hour":
            # One day at most (day reports): computed from that day's reply hours.
            merged = _merge({}, run_family(self.connection, self.source, "reply", self.start, self.end,
                                           scope=self.seat_scope, grain="hour"), ("inquiries",), key_columns)
        else:
            clean = sorted(self.clean)
            merged: dict = {}
            for row in self._rollup_seats(("inquiries",), clean, by_day=True, seats=seats):
                day = row["bucket"]
                row["bucket"] = day_start(day if grain == "day" else day.replace(day=1))
                _merge(merged, [row], ("inquiries",), key_columns)
            if grain == "month":
                for row in self._rollup_repeats(("inquiries",), clean, grain="month", seats=seats):
                    row["bucket"] = day_start(row["bucket"])
                    _merge(merged, [row], ("inquiries",), key_columns, sign=-1)
            _merge(merged, self._live("reply", scope=self.seat_scope, grain=grain), ("inquiries",), key_columns)
        # 现场 SQL 的坐席预过滤未通过三组 EXPLAIN 门槛，沿用原查询，只收集本页结果。
        selected = set(seats) if seats is not None else None
        for key, item in merged.items():
            seat = key[:3]
            if selected is not None and seat not in selected:
                continue
            bucket = item["bucket"].astimezone(BEIJING).isoformat()
            result.setdefault(seat, {})
            result[seat][bucket] = result[seat].get(bucket, 0) + int(item.get("inquiries") or 0)
        return result

    # ------------------------------------------------------------- round totals (time / AI)
    def round_rows(self, grain: str | None) -> list[dict]:
        """Started-clock metrics per channel (and bucket for grain)."""
        key_columns = ("source_key", "account_id", "channel_key") + (("bucket",) if grain else ())
        result: dict = {}
        clean = sorted(self.clean)
        if grain == "hour":
            rows = run_family(self.connection, self.source, "started", self.start, self.end,
                              scope=self.round_scope, grain="hour", by_channel=True)
            return list(_merge(result, rows, ROUND_KEYS, key_columns).values())
        if clean:
            scope, params = self.round_scope
            scope = scope.replace("{a}.channel_key IS NULL", "{a}.channel_key=''").replace("{a}", "s")
            bucket = ""
            if grain:
                bucket = ",s.day AS bucket"
            group = "s.source_key,s.account_id,s.channel_key" + (",s.day" if grain else "")
            sums = ",".join(f"sum(s.{key}) AS {key}" for key in ROUND_KEYS)
            rows = [dict(row) for row in self.connection.execute(f"""
                SELECT s.source_key,s.account_id,s.channel_key{bucket},{sums} FROM ops.stat_round_days s
                WHERE s.day = ANY(%s::date[]) AND {scope} GROUP BY {group}""", [clean, *params]).fetchall()]
            for row in rows:
                if grain:
                    day = row["bucket"]
                    row["bucket"] = day_start(day if grain == "day" else day.replace(day=1))
            _merge(result, rows, ROUND_KEYS, key_columns)
        _merge(result, self._live("started", scope=self.round_scope, grain=grain, by_channel=True),
               ROUND_KEYS, key_columns)
        return list(result.values())


class RollupBudgetExceeded(BudgetExceeded):
    """本次日汇总构建超过总预算。"""


class RollupBuilder:
    """回填完成后逐日构建，失败日期退避以免阻塞更早日期。"""

    def __init__(self, pool, statement_timeout="60s", build_budget_seconds=90):
        self.pool = pool
        self.statement_timeout = statement_timeout
        self.build_budget_seconds = build_budget_seconds
        self.failures = {}

    def next_day(self, connection, now: datetime, exclude=()) -> date | None:
        first = connection.execute("SELECT min(started_at) AS first FROM ops.round_index").fetchone()["first"]
        if first is None:
            return None
        last = beijing_day(now) - timedelta(days=1)
        row = connection.execute("""
            SELECT g.day::date AS day FROM generate_series(%s::date, %s::date, interval '1 day') AS g(day)
            LEFT JOIN ops.stat_rollup_days d ON d.day=g.day::date
            WHERE (d.day IS NULL OR d.state <> 'built' OR d.fact_version <> %s OR d.rollup_version <> %s
                OR EXISTS (SELECT 1 FROM ops.stat_dirty_days x WHERE x.day=g.day::date))
                AND g.day::date <> ALL(%s::date[])
            ORDER BY g.day DESC LIMIT 1
        """, (beijing_day(first), last, FACT_VERSION, ROLLUP_VERSION, list(exclude))).fetchone()
        return row["day"] if row and beijing_day(first) <= last else None

    def step(self, now: datetime | None = None) -> bool:
        with self.pool.connection() as connection:
            # 选日不计入 build() 的总预算，但同样限制单条查询的执行时间。
            connection.execute("SELECT set_config('statement_timeout', %s, true)", (self.statement_timeout,))
            at = now or connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            exclude = [day for day, (_, until) in self.failures.items() if until > time.monotonic()]
            day = self.next_day(connection, at, exclude=exclude)
        if day is None:
            return False
        try:
            self.build(day)
        except Exception as error:
            count = self.failures.get(day, (0, 0))[0] + 1
            delay = min(300 * 2 ** min(count - 1, 7), 21600)
            self.failures[day] = (count, time.monotonic() + delay)
            log = logger.error if count >= 3 else logger.warning
            log('Archive statistics rollup day=%s failures=%s error=%s', day, count, type(error).__name__)
        else:
            self.failures.pop(day, None)
        return True

    def build(self, day: date) -> None:
        # 连接等待、准备事务和计算写入共用入口时刻开始的预算。
        timeout = self.statement_timeout
        seconds = float(timeout[:-2]) / 1000 if timeout.endswith('ms') else float(timeout.rstrip('s'))
        budget = MaintenanceBudget(self.build_budget_seconds, statement_seconds=min(60, seconds),
                                   error_type=RollupBudgetExceeded)
        with budget.connection(self.pool) as connection:
            # 与写入者互斥，后续变更通过脏标记使报表回退到现场计算。
            connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (DAY_LOCK, (day - date(2000, 1, 1)).days))
            connection.execute("""
                INSERT INTO ops.stat_rollup_days(day,state,fact_version,rollup_version,built_at)
                VALUES(%s,'building',%s,%s,NULL)
                ON CONFLICT(day) DO UPDATE SET state='building',fact_version=EXCLUDED.fact_version,
                    rollup_version=EXCLUDED.rollup_version,built_at=NULL
            """, (day, FACT_VERSION, ROLLUP_VERSION))
            connection.execute("DELETE FROM ops.stat_dirty_days WHERE day=%s", (day,))
        with budget.connection(self.pool) as connection:
            connection.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            observed = connection.execute("SELECT now() AS now").fetchone()["now"]
            start, end = day_start(day), day_start(day + timedelta(days=1))
            source = FactSource.load(connection, observed, start=start, end=end)
            seats: dict = {}
            # 汇总当天首次回复以及这些轮次在更早日期的重复回复。
            for key, item in _merge({}, run_family(connection, source, "reply", start, end), REPLY_KEYS,
                                    ("source_key", "account_id", "agent_id")).items():
                seats[key] = {**item}
            repeats = run_family(connection, source, "reply", start, end, repeats=True)
            for clock in ("reply_sum", "handoff", "assigned", "consultation"):
                _merge(seats, run_family(connection, source, clock, start, end),
                       [m.key for m in family_metrics(clock)], ("source_key", "account_id", "agent_id"))
            rounds = run_family(connection, source, "started", start, end, by_channel=True)
            users = user_rows(connection, [(start, end)])
            for table in ("stat_seat_days", "stat_seat_repeats", "stat_round_days", "stat_user_days"):
                connection.execute(f"DELETE FROM ops.{table} WHERE day=%s", (day,))
            columns = ("day", "variant", "source_key", "account_id", "agent_id", "agent_name", *SEAT_KEYS)
            repeat_columns = ("day", "prev_hour_at", "source_key", "account_id", "agent_id", "agent_name",
                              *REPLY_KEYS)
            round_columns = ("day", "source_key", "account_id", "channel_key", *ROUND_KEYS)
            user_columns = ("day", "source_key", "account_id", "channel_key", "contact_id", "customer_id",
                            "assignments", "conversations", "first_assigned_at", "last_assigned_at")
            with connection.cursor() as cursor:
                for table, names, rows in (
                    ("stat_seat_days", columns,
                     [(day, "d", key[0], key[1], key[2], item.get("agent_name"),
                       *[item.get(name) or 0 for name in SEAT_KEYS]) for key, item in seats.items()]),
                    ("stat_seat_repeats", repeat_columns,
                     [(day, row["prev_hour_at"], row["source_key"], row["account_id"], row["agent_id"],
                       row.get("agent_name"), *[row.get(name) or 0 for name in REPLY_KEYS]) for row in repeats]),
                    ("stat_round_days", round_columns,
                     [(day, row["source_key"], row["account_id"], row["channel_key"],
                       *[row.get(name) or 0 for name in ROUND_KEYS]) for row in rounds]),
                    ("stat_user_days", user_columns, [(day, *[row[name] for name in user_columns[1:]])
                                                      for row in users]),
                ):
                    if rows:
                        cursor.executemany(f"""INSERT INTO ops.{table}({','.join(names)})
                            VALUES({','.join(['%s'] * len(names))})""", rows)
            connection.execute("""UPDATE ops.stat_rollup_days SET state='built',built_at=%s
                WHERE day=%s AND state='building'""", (observed, day))
