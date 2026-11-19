"""R13 用户成功转人工排行：按 ICS 成功分配时间统计每个用户的成功转人工次数，取前 100 名。

口径与坐席细化“发配量”（stat_metrics 的 assigned_inquiries）完全相同：一轮最多一次 ICS 成功分配，
分配时间落在统计窗口内计 1 次。数据只来自归档库 ops.round_index 上已同步的 ICS 分配记录
（responsibility_basis='ics_assignment' + responsible_agent_id + responsible_assigned_at），
本模块负责归档排行；repository 层另通过 queue_counts 补充当前排队日、周计数。

用户 = 来源 + 账号 + Chatwoot 联系人（contact_id）。同一联系人多个会话的成功转人工合并；
联系人无法识别（customer_index.contact_id 为空）的轮次计入总次数，但不参与排行。

性能（R2，迁移 020）：已结束且未变动的整天读日汇总 ops.stat_user_days（处理程序空闲时与其他日汇总一起建），
其余时段（今天、未建/脏日、带时刻的首尾不足一天）用同一条 SQL（rollups.user_rows_sql）现算，两部分相加；
溯源明细按联系人 → 客户 → 轮次走索引，不扫整个区间。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quality.processing.rollups import ASSIGNED_TS, ASSIGNED_WHERE, clean_days, day_ranges, user_rows_sql
from quality.processing.stat_metrics import day_start, days_between
from .periods import BEIJING

RANK_LIMIT = 100


def _scope(source_key=None, account_id=None, channel_key=None, alias="r", unmapped="IS NULL") -> tuple[str, list]:
    clauses, values = [], []
    if source_key is not None:
        clauses.append(f"{alias}.source_key=%s")
        values.append(source_key)
    if account_id is not None:
        clauses.append(f"{alias}.account_id=%s")
        values.append(account_id)
    if channel_key is not None:
        if channel_key == "unmapped":
            clauses.append(f"{alias}.channel_key {unmapped}")
        else:
            clauses.append(f"{alias}.channel_key=%s")
            values.append(channel_key)
    return " AND ".join(clauses) or "TRUE", values


# 名次稳定：次数多在前 → 最近一次成功转人工晚者在前 → 联系人 ID（数字序）→ 来源、账号。
RANK_ORDER = ("assignments DESC, last_assigned_at DESC, length(contact_id), contact_id, source_key, account_id")


def _blacklist_status(connection, users) -> None:
    """标注当前黑名单状态：active=拉黑中，past=曾被拉黑（ops.contact_block_state，迁移 020）。"""
    for user in users:
        user["blacklist_status"] = None
    if not users:
        return
    keys = [(u["source_key"], u["account_id"], u["contact_id"]) for u in users]
    sources, accounts, contacts = (list(column) for column in zip(*keys))
    rows = connection.execute("""
        SELECT s.source_key,s.account_id,s.contact_id,
            s.action IN ('block','change') AND (s.permanent OR s.blocked_until > now()) AS active
        FROM unnest(%s::text[], %s::bigint[], %s::text[]) AS k(source_key,account_id,contact_id)
        JOIN ops.contact_block_state s ON s.source_key=k.source_key AND s.account_id=k.account_id
            AND s.contact_id=k.contact_id
    """, (sources, accounts, contacts)).fetchall()
    status = {(r["source_key"], int(r["account_id"]), r["contact_id"]): "active" if r["active"] else "past"
              for r in rows}
    for user in users:
        user["blacklist_status"] = status.get((user["source_key"], int(user["account_id"]), user["contact_id"]))


def parts_sql(connection, start, end, period_end, now, source_key=None, account_id=None, channel_key=None):
    """Rows (source,account,channel,contact,customer,assignments,conversations,first/last) of [start, end):
    clean whole days from ops.stat_user_days, the rest live with the same SQL."""
    days = days_between(start, end)
    full = [day for day in days if day_start(day) >= start and day_start(day + timedelta(days=1)) <= period_end]
    clean = sorted(clean_days(connection, full, now)) if full else []
    live = day_ranges([day for day in days if day not in clean], start, end)
    parts, params = [], []
    if clean:
        where, values = _scope(source_key, account_id, channel_key, alias="s", unmapped="= ''")
        parts.append(f"""SELECT s.source_key,s.account_id,s.channel_key,s.contact_id,s.customer_id,s.assignments,
            s.conversations,s.first_assigned_at,s.last_assigned_at
            FROM ops.stat_user_days s WHERE s.day = ANY(%s::date[]) AND {where}""")
        params += [clean, *values]
    if live:
        where, values = _scope(source_key, account_id, channel_key)
        sql, values = user_rows_sql(live, where, values)
        parts.append(sql)
        params += values
    if not parts:
        return None
    return " UNION ALL ".join(f"({part})" for part in parts), params


def user_report(connection, period, *, source_key=None, account_id=None, channel_key=None,
                page=1, page_size=20, now=None, full_ranking=False) -> dict:
    """排行（前 100 名中的一页）与概况；full_ranking=True 时返回完整前 100 名（导出用，不含逐条溯源明细）。"""
    now = now or datetime.now(timezone.utc)
    end = min(period.end, now)
    summary = {"total_assignments": 0, "unidentified_assignments": 0, "users_total": 0}
    ranked: list[dict] = []
    built = parts_sql(connection, period.start, end, period.end, now, source_key, account_id, channel_key) \
        if period.start < end else None
    if built is not None:
        union, params = built
        rows = connection.execute(f"""WITH p AS MATERIALIZED ({union}),
            totals AS (
                SELECT COALESCE(sum(assignments),0) AS total_assignments,
                    COALESCE(sum(assignments) FILTER(WHERE contact_id=''),0) AS unidentified_assignments,
                    count(DISTINCT (source_key,account_id,contact_id)) FILTER(WHERE contact_id<>'') AS users_total
                FROM p
            ), conv AS (
                SELECT p.source_key,p.account_id,p.contact_id,count(DISTINCT x) AS conversations
                FROM p CROSS JOIN LATERAL unnest(p.conversations) x WHERE p.contact_id<>''
                GROUP BY 1,2,3
            ), u AS (
                SELECT p.source_key,p.account_id,p.contact_id,sum(p.assignments) AS assignments,
                    min(p.first_assigned_at) AS first_assigned_at,max(p.last_assigned_at) AS last_assigned_at,
                    (array_agg(c.display_name ORDER BY p.last_assigned_at DESC)
                        FILTER(WHERE c.display_name IS NOT NULL))[1] AS display_name,
                    (array_agg(p.customer_id ORDER BY p.last_assigned_at DESC))[1] AS customer_id,
                    array_agg(DISTINCT COALESCE(NULLIF(p.channel_key,''),'unmapped')) AS channels
                FROM p LEFT JOIN ops.customer_index c ON c.customer_id=p.customer_id
                WHERE p.contact_id<>'' GROUP BY 1,2,3
            ), ranked AS (
                SELECT u.*,conv.conversations FROM u JOIN conv USING(source_key,account_id,contact_id)
                ORDER BY {RANK_ORDER} LIMIT {RANK_LIMIT}
            )
            SELECT t.total_assignments,t.unidentified_assignments,t.users_total,ranked.*
            FROM totals t LEFT JOIN ranked ON TRUE ORDER BY {RANK_ORDER}""", params).fetchall()
        summary = {key: int(rows[0][key] or 0) for key in summary}
        ranked = [{key: value for key, value in row.items() if key not in summary}
                  for row in rows if row["contact_id"] is not None]
    # 页面与报告统一的“最早分配记录”，用于提示同步开启之前没有数据（R13-18）。
    earliest = connection.execute(f"""SELECT min({ASSIGNED_TS}) AS earliest FROM ops.round_index r
        WHERE {ASSIGNED_WHERE}""").fetchone()["earliest"]
    for rank, user in enumerate(ranked, 1):
        user["rank"] = rank
        user["account_id"] = int(user["account_id"])
        user["assignments"] = int(user["assignments"])
        user["conversations"] = int(user["conversations"])
        user["channels"] = sorted(user["channels"] or [])
    page_items = ranked[(page - 1) * page_size: page * page_size]
    _blacklist_status(connection, ranked if full_ranking else page_items)
    ranked_sum = sum(u["assignments"] for u in ranked)
    total = summary["total_assignments"]
    result = {
        **period.metadata(now), **summary,
        "ranked_total": len(ranked), "ranked_assignments": ranked_sum,
        "ranked_share": round(ranked_sum / total * 100, 2) if total else None,
        "earliest_assigned_at": _iso(earliest),
        "before_earliest": earliest is None or period.start < earliest,
        "users": [{**u, "first_assigned_at": _iso(u["first_assigned_at"]),
                   "last_assigned_at": _iso(u["last_assigned_at"])} for u in (ranked if full_ranking else page_items)],
        "total": len(ranked), "page": page, "page_size": page_size, "as_of": now.isoformat(),
        "channel_key": channel_key,
    }
    return result


def user_detail(connection, period, *, source_key, account_id, contact_id, channel_key=None, now=None) -> dict:
    """某个用户区间内每一次成功转人工（溯源明细），条数等于排行中的次数。
    按联系人 → 客户（customer_index_contact_idx）→ 轮次（round_index_customer_idx）取数，与区间长短无关。"""
    now = now or datetime.now(timezone.utc)
    end = min(period.end, now)
    rows: list[dict] = []
    if period.start < end:
        where, values = _scope(None, None, channel_key)
        rows = [dict(row) for row in connection.execute(f"""
            SELECT r.source_key,r.account_id,NULLIF(c.contact_id,'') AS contact_id,r.round_id,r.conversation_id,
                r.round_no,r.channel_key,r.customer_id,{ASSIGNED_TS} AS assigned_at,
                NULLIF(r.data->>'responsible_agent_id','') AS agent_id,r.data->>'responsible_agent_name' AS agent_name
            FROM ops.customer_index c JOIN ops.round_index r ON r.customer_id=c.customer_id
            WHERE c.source_key=%s AND c.account_id=%s AND c.contact_id=%s AND r.source_key=%s AND r.account_id=%s
                AND {ASSIGNED_WHERE} AND {ASSIGNED_TS} >= %s AND {ASSIGNED_TS} < %s AND {where}
            ORDER BY assigned_at,r.round_id
        """, [source_key, account_id, str(contact_id), source_key, account_id, period.start, end, *values]).fetchall()]
    return {**period.metadata(now), "source_key": source_key, "account_id": account_id,
            "contact_id": str(contact_id), "as_of": now.isoformat(),
            "items": [{**d, "account_id": int(d["account_id"]), "assigned_at": _iso(d["assigned_at"])} for d in rows]}


def _iso(value):
    return value.astimezone(BEIJING).isoformat() if isinstance(value, datetime) else value
