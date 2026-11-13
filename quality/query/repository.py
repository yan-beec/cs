"""Bounded archive queries and restricted review saves, separate from the ICS pool."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
import uuid

from psycopg.errors import InvalidParameterValue, SerializationFailure
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from quality.attachment_safety import safe_attachments
from quality.processing.service_facts import HUMAN_REPLY, FactSource
from quality.processing.stat_metrics import METRICS, drill_metric, drill_selection

from .budget import archive_budget, read_only_snapshot
from .config import QuerySettings
from .responsibility import normalize_responsibility
from .service_metrics import RoundServiceMetrics


OVERVIEW_AGENT_LIMIT = 200


def customer_round_seq(alias: str = "r") -> str:
    """用户累计第 N 次咨询（跨会话），返回可放入 SELECT 的 SQL 表达式。

    round_no 只在单个 Chatwoot 会话内递增；同一用户每次咨询都开新会话时，
    每条记录都会是“第 1 轮”。序号 = 该客户全部轮次按 (started_at, round_id) 排序的名次，
    由触发器预存在 ops.customer_round_seq（迁移 020），这里按主键取一行，不受筛选和分页影响。
    """
    return f"(SELECT seq.seq FROM ops.customer_round_seq seq WHERE seq.round_id={alias}.round_id)"


def customer_round_seq_cte(customer_scope: str) -> str:
    """批量导出使用的同一份预存序号；customer_scope 为返回 customer_id 的子查询。"""
    return f"""customer_round_seq AS MATERIALIZED (
        SELECT round_id,seq AS customer_round_seq FROM ops.customer_round_seq
        WHERE customer_id IN ({customer_scope}))"""


def latest_review_sql(alias: str) -> str:
    """当前质检结论（与 quality.current_round_reviews 视图列一致），按主键读 quality.round_review_current。"""
    return (f"(SELECT rr.round_id,rr.status,rr.remark,rr.reviewer,rr.reviewed_at,rr.review_id::text AS revision,"
            f"rr.ai_status,rr.human_status FROM quality.round_review_current rr WHERE rr.round_id={alias}.round_id)")


def latest_review_join(alias: str, name: str = "review") -> str:
    return f"LEFT JOIN LATERAL {latest_review_sql(alias)} {name} ON TRUE"


# ICS 已确认分配的成功分配时间（发配量口径），与 round_index_assigned_idx 表达式一致。
ASSIGNED_AT_SQL = "ops.archive_ts(r.data ->> 'responsible_assigned_at')"
ASSIGNED_ROUND_SQL = ("r.data ->> 'responsibility_basis' = 'ics_assignment'"
                      " AND NULLIF(r.data ->> 'responsible_agent_id', '') IS NOT NULL")

# 时间筛选与迁移 011 的表达式索引保持字面一致，否则用不上索引。
TIME_EXPRESSIONS = {
    "started_at": "r.started_at",
    "resolved_at": "ops.archive_ts(COALESCE(r.data ->> 'resolved_at', r.data ->> 'closed_at'))",
    "rating_at": "ops.archive_ts(r.data ->> 'rating_at')",
    # 转人工时间：与统计报表流单/首响口径同一函数，命中 round_index_handoff_idx。
    "handoff_at": "ops.round_handoff_at(r.data, r.started_at)",
    # 成功分配时间：只有 ICS 已确认分配的轮次才有。
    "assigned_at": ASSIGNED_AT_SQL,
}

RATING_OPERATORS = {"eq": "=", "gt": ">", "lt": "<"}

# 人工回复：统计与归档唯一的一份谓词（quality.processing.service_facts.HUMAN_REPLY），别名 lm。
HUMAN_REPLY_LM = HUMAN_REPLY.replace("m.", "lm.")


# 接待结果：与时间统计/AI质检同一套每轮事实（别名 f）。
OUTCOME_PREDICATES = {
    "ai_only": "f.is_ai_only", "handoff": "f.is_handoff", "refused": "f.busy_refused",
    "effective_human": "f.human_replied", "lost": "f.is_lost",
}
DIALOGUE_PREDICATES = {"dialogue": "f.visitor_messages > 0", "high_quality": "f.visitor_messages > 5"}


class ReviewConflict(RuntimeError):
    """A newer review was saved after the browser loaded this round."""


class ReviewValidationError(ValueError):
    """保存结论与轮次当前接待分类不匹配。"""

    def __init__(self, message: str, reception_type: str | None = None):
        super().__init__(message)
        self.reception_type = reception_type


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
    rating_op: str = "eq"
    lost: bool | None = None
    keyword: str | None = None
    message_scope: str = "all"
    has_attachment: bool | None = None
    status: str | None = None
    ai_review_status: str | None = None
    human_review_status: str | None = None
    # 黑名单：ever=曾被拉黑，active=当前拉黑中（数据来自 archive.contact_block_events）。
    blacklist: str | None = None
    # 锁定时刻：本次归档浏览的判定与聊天内容都截至该时刻（进入归档模块或报表生成的时刻）。
    as_of: datetime | None = None
    # 统计报表下钻：指标（stat_metrics.METRICS 或其别名）、统计窗口 start/end、记入的坐席
    # （metric_seat，""=无该坐席的轮次），命中的轮次与报表格子计入的轮次完全相同。
    metric: str | None = None
    metric_seat: str | None = None
    # 与报表同一套每轮事实的筛选：接待结果、协助轮次、无 ICS 分配、有效对话。
    outcome: str | None = None
    assisted: bool | None = None
    unassigned: bool | None = None
    dialogue: str | None = None


def needs_facts(filters: ArchiveFilters | None) -> bool:
    """筛选条件是否要读每轮统计事实（考核坐席、接待结果、报表下钻等）。"""
    return filters is not None and bool(
        filters.metric or filters.outcome or filters.assisted is not None or filters.unassigned is not None
        or filters.dialogue or filters.lost or (filters.agent_id and filters.agent_scope == "responsible"))


def load_facts(connection, filters: ArchiveFilters | None, now: datetime | None = None, *,
               customer_id: str | None = None) -> FactSource | None:
    """本次查询需要的每轮事实：已存的当前事实 + 锁定时刻后才有变化的轮次现场计算（同一规则）。"""
    if not needs_facts(filters):
        return None
    observed = filters.as_of or now or connection.execute("SELECT now() AS now").fetchone()["now"]
    scope, params = [], []
    if filters.source_key is not None:
        scope.append("r.source_key=%s")
        params.append(filters.source_key)
    if filters.account_id is not None:
        scope.append("r.account_id=%s")
        params.append(filters.account_id)
    scope.append("r.started_at < %s")
    params.append(observed)
    # 只提前应用主查询必定保留、且只读取索引列的条件，避免用旧事实排除待补算轮次。
    round_clauses, round_params = [], []
    if customer_id is not None:
        round_clauses.append("r.customer_id = %s")
        round_params.append(customer_id)
    for name in ("inbox_id", "conversation_id", "round_no"):
        value = getattr(filters, name)
        if value is not None:
            round_clauses.append(f"r.{name} = %s")
            round_params.append(value)
    if filters.channel_key:
        round_clauses.append("r.channel_key IS NULL" if filters.channel_key == "unmapped" else "r.channel_key = %s")
        if filters.channel_key != "unmapped":
            round_params.append(filters.channel_key)
    if filters.reception_type:
        round_clauses.append("r.reception_type = %s")
        round_params.append(filters.reception_type)
    # 指定客户的详情与导出会移除搜索词，补算范围也必须保留该客户。
    if customer_id is None and filters.customer_query:
        condition, customer_values = _customer_search_condition(filters.customer_query)
        round_clauses.append(f"r.customer_id IN (SELECT c.customer_id FROM ops.customer_index c WHERE {condition})")
        round_params.extend(customer_values)
    return FactSource.load(connection, observed, start=filters.start, end=filters.end,
                           scope=" AND ".join(scope), scope_params=params,
                           round_filter=" AND ".join(round_clauses) or None, round_params=round_params)


def fact_conditions(filters: ArchiveFilters, facts: FactSource | None) -> tuple[list[str], list]:
    """统计事实类条件，返回 r.round_id IN (...) 子句。"""
    if not needs_facts(filters):
        return [], []
    if facts is None:
        # Stored current facts only (rounds changed after the locked time are not
        # computed live); interactive queries and exports always pass load_facts().
        facts = FactSource(filters.as_of)
    clauses: list[str] = []
    values: list[Any] = []
    if filters.metric:
        metric = drill_metric(filters.metric)
        selection, params = drill_selection(facts, metric, filters.start, filters.end, filters.metric_seat)
        clauses.append(f"r.round_id IN ({selection})")
        values.extend(params)
    predicates, params = [], []
    if filters.outcome:
        predicates.append(OUTCOME_PREDICATES[filters.outcome])
    if filters.lost:
        # 流单：已转人工、截至锁定时刻没有任何人工回复；即统计报表“流单数”的 is_lost。
        predicates.append(OUTCOME_PREDICATES["lost"])
    if filters.assisted is not None:
        predicates.append("f.assisted" if filters.assisted else "NOT f.assisted")
    if filters.unassigned is not None:
        predicates.append("f.assigned_agent_id IS NULL" if filters.unassigned else "f.assigned_agent_id IS NOT NULL")
    if filters.dialogue:
        predicates.append(DIALOGUE_PREDICATES[filters.dialogue])
    if filters.agent_id and filters.agent_scope == "responsible":
        predicates.append("f.responsible_agent_id = %s")
        params.append(filters.agent_id)
    if predicates:
        relation, relation_params = facts.facts()
        clauses.append(f"r.round_id IN (SELECT f.round_id FROM {relation} WHERE {' AND '.join(predicates)})")
        values.extend([*relation_params, *params])
    return clauses, values


def as_of_message(filters: ArchiveFilters | None, alias: str = "m") -> tuple[str, list]:
    """锁定时刻之后的消息不出现（时间未知的消息保留）。"""
    if filters is None or filters.as_of is None:
        return "TRUE", []
    return f"({alias}.sent_at IS NULL OR {alias}.sent_at < %s)", [filters.as_of]


# 迁移 012 未执行时不能引用黑名单表，否则查询失败会把整个归档判为不可用。
# QueryRepository.ping() 每次探测时刷新；路由在使用黑名单筛选前检查。
BLACKLIST_STATE = {"ready": False}


def blacklist_contact_condition(mode: str, source: str, account: str, contact: str) -> str:
    """返回“该联系人曾被拉黑 / 当前拉黑中”的 SQL 片段；只拼接固定列名。
    读每个联系人最新一条事件的预存状态 ops.contact_block_state（迁移 020），到期按 now() 判断。"""
    scope = f"b.source_key={source} AND b.account_id={account} AND b.contact_id={contact}"
    if mode == "ever":
        return f"EXISTS (SELECT 1 FROM ops.contact_block_state b WHERE {scope})"
    if mode == "active":
        return (f"EXISTS (SELECT 1 FROM ops.contact_block_state b WHERE {scope} AND b.action IN ('block','change') "
                f"AND (b.permanent OR b.blocked_until > now()))")
    raise ValueError("Unsupported blacklist filter")


def _literal_pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _customer_search_condition(query: str) -> tuple[str, list]:
    # H5 IDs are searchable even without verified business identity. Older
    # projections kept the contact identifier only in unverified_identifier.
    pattern = _literal_pattern(query)
    return (
        "(c.display_name ILIKE %s OR c.contact_id ILIKE %s OR "
        "COALESCE(NULLIF(c.data ->> 'h5_user_id', ''), c.data ->> 'unverified_identifier') ILIKE %s OR "
        "c.business_user_id ILIKE %s OR c.customer_id = %s)",
        [pattern, pattern, pattern, pattern, query],
    )


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
        elif filters.message_scope == "agent":
            # 真人客服消息 = 统计口径的人工回复（排除评价卡、空白和已删除消息）。
            eligible.append(HUMAN_REPLY)
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
    if filters.as_of is not None and (len(eligible) > len(relation) or filters.has_attachment is not None):
        eligible.append("(m.sent_at IS NULL OR m.sent_at < %s)")
        values.append(filters.as_of)
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


def round_conditions(filters: ArchiveFilters, facts: FactSource | None = None) -> tuple[str, list]:
    """Only SQL fragments from closed dictionaries are interpolated.

    Statistics-fact conditions (assessed seat, outcome, report drill-down) need
    the FactSource of load_facts() for the same filters."""
    _require_scoped_ids(filters)
    clauses: list[str] = []
    values: list[Any] = []
    if filters.as_of is not None:
        clauses.append("r.started_at < %s")
        values.append(filters.as_of)
    fact_clauses, fact_values = fact_conditions(filters, facts)
    clauses.extend(fact_clauses)
    values.extend(fact_values)
    for name in ("source_key", "account_id", "inbox_id", "conversation_id", "round_no", "rating_status", "status"):
        value = getattr(filters, name)
        if value is not None:
            clauses.append(f"r.{name} = %s")
            values.append(value)
    if filters.rating is not None:
        # 比较符只取自固定字典；大于/小于只比较已评价轮次，未评分不当作 0 分。
        operator = RATING_OPERATORS[filters.rating_op]
        if filters.rating_op != "eq":
            clauses.append("r.rating_status = 'rated'")
        clauses.append(f"r.rating {operator} %s")
        values.append(filters.rating)
    if filters.reception_type:
        clauses.append("r.reception_type = %s")
        values.append(filters.reception_type)
    review_terms = []
    review_values = []
    for field, verdict in (("ai_status", filters.ai_review_status), ("human_status", filters.human_review_status)):
        if verdict is not None:
            # 转人工前保存的空人工结论，在当前人工场景下属于尚未质检。
            expression = "COALESCE(review.human_status, 'pending')" if field == "human_status" and verdict == "pending" else f"review.{field}"
            review_terms.append(f"{expression} = %s")
            review_values.append(verdict)
    if review_terms:
        if filters.human_review_status is not None:
            clauses.append("r.reception_type = 'human'")
        # Match both verdicts on the same round's latest saved revision.
        review_match = ("EXISTS (SELECT 1 FROM quality.round_review_current review "
                        "WHERE review.round_id=r.round_id AND " + " AND ".join(review_terms) + ")")
        if all(verdict == "pending" for verdict in review_values):
            # Unsaved rounds display pending in the editor and exports too.
            review_match = (f"({review_match} OR NOT EXISTS (SELECT 1 FROM quality.round_review_current review "
                            "WHERE review.round_id=r.round_id))")
        clauses.append(review_match)
        values.extend(review_values)
    if filters.channel_key:
        clauses.append("r.channel_key IS NULL" if filters.channel_key == "unmapped" else "r.channel_key = %s")
        if filters.channel_key != "unmapped":
            values.append(filters.channel_key)
    if filters.customer_query:
        condition, customer_values = _customer_search_condition(filters.customer_query)
        clauses.append(condition)
        values.extend(customer_values)
    if filters.agent_id and filters.agent_scope != "responsible":
        # responsible（考核坐席）在 fact_conditions 里按统计事实判断。
        if filters.agent_scope == "current":
            clauses.append("EXISTS (SELECT 1 FROM ops.conversation_index ci WHERE ci.source_key=r.source_key AND ci.account_id=r.account_id AND ci.conversation_id=r.conversation_id AND ci.current_assignee_id=%s)")
        else:
            predicate = {"replied": "a.replied", "assigned": "a.was_assigned"}[filters.agent_scope]
            clauses.append(f"EXISTS (SELECT 1 FROM ops.round_agent_index a WHERE a.round_id=r.round_id AND a.agent_id=%s AND {predicate})")
        values.append(filters.agent_id)
    if filters.metric:
        pass  # 报表下钻：start/end 是指标自己的统计窗口，已在 fact_conditions 中使用。
    elif filters.time_field in ("reply_at", "message_at"):
        # 区间内有消息的轮次：消息按时间顺序归轮，轮次开始不晚于其任何消息；人工回复是服务消息，
        # 不晚于轮次 last_message_at。先用这两个索引列圈出候选轮次，再逐轮核对消息
        # （消息时间的核对在 _message_conditions；内部备注等不更新 last_message_at，所以消息时间只用开始时间边界）。
        if filters.start and filters.time_field == "reply_at":
            clauses.append("r.last_message_at >= %s")
            values.append(filters.start)
        if filters.end:
            clauses.append("r.started_at < %s")
            values.append(filters.end)
        if filters.time_field == "reply_at" and (filters.start or filters.end):
            # 人工回复时间：区间内有人工回复的轮次（统计报表询盘类指标的时间口径）。
            window = ["lm.round_id=r.round_id", HUMAN_REPLY_LM]
            for operator, value in ((">=", filters.start), ("<", filters.end)):
                if value:
                    window.append(f"lm.sent_at {operator} %s")
                    values.append(value)
            if filters.as_of is not None:
                window.append("lm.sent_at < %s")
                values.append(filters.as_of)
            clauses.append(f"EXISTS (SELECT 1 FROM ops.message_index lm WHERE {' AND '.join(window)})")
    else:
        expression = TIME_EXPRESSIONS[filters.time_field]
        if filters.time_field == "assigned_at" and (filters.start or filters.end):
            clauses.append(ASSIGNED_ROUND_SQL)
        if filters.start:
            clauses.append(f"{expression} >= %s")
            values.append(filters.start)
        if filters.end:
            clauses.append(f"{expression} < %s")
            values.append(filters.end)
    if filters.blacklist:
        # 按客户筛选：命中后返回该客户的全部轮次。
        clauses.append("EXISTS (SELECT 1 FROM ops.customer_index bc WHERE bc.customer_id=r.customer_id AND "
                       + blacklist_contact_condition(filters.blacklist, "bc.source_key", "bc.account_id", "bc.contact_id") + ")")
    message_clauses, message_values = _message_conditions(filters, ["m.round_id=r.round_id"])
    clauses.extend(message_clauses)
    values.extend(message_values)
    return " AND ".join(clauses) or "TRUE", values


def customer_round_scoped(filters: ArchiveFilters | None) -> bool:
    """按客户查看时，是否有“轮次级”筛选条件。

    只按来源/账号/用户搜索/黑名单筛选时，条件描述的是“哪个用户”，详情照旧展示该用户完整历史；
    只要还有坐席、时间、关键词、评价、质检、会话、渠道、收件箱等条件，筛选描述的就是“哪些轮次”，
    按客户的列表、轮次目录、聊天记录与导出都只包含命中的轮次，避免把其他坐席/其他时间的轮次混进来。
    """
    if filters is None:
        return False
    return any(value is not None for value in (
        filters.channel_key, filters.inbox_id, filters.conversation_id, filters.round_no, filters.reception_type,
        filters.agent_id, filters.start, filters.end, filters.rating_status, filters.rating, filters.keyword,
        filters.has_attachment, filters.status, filters.ai_review_status, filters.human_review_status, filters.lost,
        filters.metric, filters.outcome, filters.assisted, filters.unassigned, filters.dialogue,
    )) or filters.message_scope != "all"


def customer_scope_round_conditions(filters: ArchiveFilters, facts: FactSource | None = None) -> tuple[str, list]:
    """命中轮次条件（别名 r），由调用方再加 r.customer_id 关联；用户级条件在客户表上单独判断。"""
    return round_conditions(replace(filters, customer_query=None, blacklist=None), facts)


def customer_unrounded_message_predicate(filters: ArchiveFilters, customer_ref: str) -> tuple[str, list] | None:
    """尚未整理进轮次的消息（别名 m）是否命中筛选；只有纯消息/会话类条件才可能命中。

    这类消息不属于任何轮次，按客户筛选时仍要能被搜到（证据不完整时也能找到用户），
    详情里只展示命中的这几条，而不是该用户的其他轮次。返回 None 表示条件含轮次属性，未归轮消息不可能命中。
    """
    round_specific = any(value is not None for value in (
        filters.round_no, filters.reception_type, filters.agent_id if filters.agent_scope != "current" else None,
        filters.rating_status, filters.rating, filters.status, filters.ai_review_status, filters.human_review_status,
        filters.lost, filters.metric, filters.outcome, filters.assisted, filters.unassigned, filters.dialogue,
    )) or (filters.time_field != "message_at" and bool(filters.start or filters.end))
    if round_specific:
        return None
    eligible = [f"m.customer_id={customer_ref}", "m.round_id IS NULL"]
    values: list[Any] = []
    if filters.source_key is not None:
        eligible.append("m.source_key=%s")
        values.append(filters.source_key)
    if filters.account_id is not None:
        eligible.append("m.account_id=%s")
        values.append(filters.account_id)
    if filters.conversation_id is not None:
        eligible.append("m.conversation_id=%s")
        values.append(filters.conversation_id)
    conversation_scope = "ci.source_key=m.source_key AND ci.account_id=m.account_id AND ci.conversation_id=m.conversation_id"
    if filters.agent_id:
        eligible.append(f"EXISTS (SELECT 1 FROM ops.conversation_index ci WHERE {conversation_scope} AND ci.current_assignee_id=%s)")
        values.append(filters.agent_id)
    if filters.inbox_id is not None:
        eligible.append(f"COALESCE(m.data ->> 'inbox_id', (SELECT ci.inbox_id::text FROM ops.conversation_index ci WHERE {conversation_scope})) = %s")
        values.append(str(filters.inbox_id))
    if filters.channel_key:
        expression = f"COALESCE(m.data ->> 'channel_key', (SELECT ci.channel_key FROM ops.conversation_index ci WHERE {conversation_scope}))"
        eligible.append(f"{expression} IS NULL" if filters.channel_key == "unmapped" else f"{expression} = %s")
        if filters.channel_key != "unmapped":
            values.append(filters.channel_key)
    if filters.keyword:
        eligible.append("EXISTS (SELECT 1 FROM archive.message_snapshots s WHERE s.source_key=m.source_key AND s.account_id=m.account_id AND s.message_id=m.message_id AND s.content ILIKE %s)")
        values.append(_literal_pattern(filters.keyword))
    if filters.message_scope != "all":
        if filters.message_scope == "private":
            eligible.append("m.is_private")
        elif filters.message_scope == "agent":
            eligible.append(HUMAN_REPLY)
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
    if filters.as_of is not None:
        eligible.append("(m.sent_at IS NULL OR m.sent_at < %s)")
        values.append(filters.as_of)
    if filters.has_attachment is not None:
        attachment = "jsonb_array_length(COALESCE(m.data -> 'attachments', '[]'::jsonb)) > 0"
        eligible.append(attachment if filters.has_attachment else f"NOT ({attachment})")
    return " AND ".join(eligible), values


def customer_conditions(filters: ArchiveFilters, facts: FactSource | None = None) -> tuple[str, list]:
    """Customer-level filters pick the user; round-level filters require a matching round."""
    _require_scoped_ids(filters)
    # 客户在锁定时刻前有消息：预存的消息数与最早消息时间（迁移 020），不逐个客户探查消息表。
    clauses, values = ["c.message_total > 0"], []
    if filters.as_of is not None:
        clauses.append("c.message_since < %s")
        values.append(filters.as_of)
    for name in ("source_key", "account_id"):
        value = getattr(filters, name)
        if value is not None:
            clauses.append(f"c.{name} = %s")
            values.append(value)
    if filters.customer_query:
        condition, customer_values = _customer_search_condition(filters.customer_query)
        clauses.append(condition)
        values.extend(customer_values)
    if filters.blacklist:
        clauses.append(blacklist_contact_condition(filters.blacklist, "c.source_key", "c.account_id", "c.contact_id"))
    if customer_round_scoped(filters):
        # 与轮次列表同一套条件：客户因“命中轮次”出现，展示时也只展示这些轮次；
        # 纯消息类条件还可命中尚未归轮的消息（详情只展示命中的那几条）。
        where, round_values = customer_scope_round_conditions(filters, facts)
        matched = f"EXISTS (SELECT 1 FROM ops.round_index r WHERE r.customer_id=c.customer_id AND {where})"
        unrounded = customer_unrounded_message_predicate(filters, "c.customer_id")
        if unrounded is None:
            clauses.append(matched)
            values.extend(round_values)
        else:
            clauses.append(f"({matched} OR EXISTS (SELECT 1 FROM ops.message_index m WHERE {unrounded[0]}))")
            values.extend([*round_values, *unrounded[1]])
    return " AND ".join(clauses), values


def stored_counts(filters: ArchiveFilters | None, alias: str = "c") -> tuple[str, str, list, list]:
    """(round_count SQL, message_count SQL, round params, message params) of a customer row as of the
    locked time: the precomputed totals minus what was added at/after that time (index tail only)."""
    if filters is None or filters.as_of is None:
        return f"{alias}.round_total", f"{alias}.message_total", [], []
    return (f"({alias}.round_total - (SELECT count(*) FROM ops.round_index lr WHERE lr.customer_id={alias}.customer_id"
            f" AND lr.started_at >= %s))",
            f"({alias}.message_total - (SELECT count(*) FROM ops.message_index lm WHERE lm.customer_id={alias}.customer_id"
            f" AND lm.sent_at >= %s))", [filters.as_of], [filters.as_of])


def _flatten(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row.get("data") or {})
    result.update({key: value for key, value in row.items() if key != "data"})
    if "attachments" in result:
        # 非图片/音视频附件在任何下游（后台、Excel、离线留档）都拿不到可打开的地址。
        result["attachments"] = safe_attachments(result["attachments"])
    if "round_id" in row and "reception_type" in row:
        result = normalize_responsibility(result)
    return result


def _contact_profile(value):
    """Extract the stable Chatwoot contact fields from an archived payload."""
    if not isinstance(value, dict):
        return {}
    additional = value.get("additional_attributes") if isinstance(value.get("additional_attributes"), dict) else {}
    custom = value.get("custom_attributes") if isinstance(value.get("custom_attributes"), dict) else {}
    h5_user_id = next((source.get(key) for source in (value, additional, custom)
                       for key in ("identifier", "userId", "user_id", "h5_user_id", "app_user_id")
                       if isinstance(source, dict) and source.get(key) not in (None, "")), None)
    phone = next((value.get(key) for source in (value, additional, custom)
                  for key in ("phone_number", "phone")
                  if isinstance(source, dict) and source.get(key) not in (None, "")), None)
    note = next((source.get(key) for source in (value, additional, custom)
                 for key in ("description", "note", "notes", "contact_note", "contact_notes")
                 if isinstance(source, dict) and isinstance(source.get(key), str) and source.get(key).strip()), None)
    return {"h5_user_id": str(h5_user_id) if h5_user_id is not None else None,
            "chatwoot_user_id": str(value.get("id") or value.get("user_id")) if value.get("id") or value.get("user_id") else None,
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


def statistics_metrics(card: dict, fact: dict | None) -> dict:
    """Card timings from the statistics facts, so a card shows the figures the reports count.

    Display-only fields (round duration, message counts, last speaker) stay as
    read from the messages. A round is timed only for its ICS-assigned seat
    when no colleague replied (statistics rule); otherwise the card says why."""
    result = dict(card)
    if fact is None:
        return result
    first = fact.get("first_response_seconds")
    result.update(first_response_seconds=first, average_response_seconds=fact.get("average_response_seconds"),
                  response_sample_count=int(fact.get("response_samples") or 0),
                  responding_seat_count=int(fact.get("responding_seats") or 0),
                  seat_timing_excluded=bool(fact.get("assisted")),
                  assessed_agent_id=fact.get("responsible_agent_id"),
                  assigned_agent_id=fact.get("assigned_agent_id"),
                  consultation_seconds=fact.get("consultation_seconds"))
    if first is not None:
        result["first_response_status"] = "recorded"
    elif fact.get("handoff_at") is None:
        result["first_response_status"] = card.get("first_response_status") if card.get(
            "first_response_status") in ("not_handed_off", "missing_handoff_time") else "not_handed_off"
    elif fact.get("assigned_agent_id") is None:
        result["first_response_status"] = "unassigned"
    elif fact.get("assisted"):
        result["first_response_status"] = "assisted"
    elif not fact.get("human_replied"):
        result["first_response_status"] = "awaiting_agent"
    elif card.get("first_response_status") in ("unknown_time", "over_limit"):
        pass
    else:
        result["first_response_status"] = "not_counted"
    return result


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
                    has_function_privilege(current_user, 'quality.save_round_review(text,text,text,text,text,bigint)', 'EXECUTE') AS review_saves,
                    to_regclass('ops.round_service_facts') IS NOT NULL AS scaling,
                    to_regclass('ops.customer_round_seq') IS NOT NULL
                        AND to_regprocedure('quality.get_rating_review(text)') IS NOT NULL
                        AND to_regprocedure('quality.save_rating_review(text,text,text,bigint,text)') IS NOT NULL AS precomputed
                FROM pg_roles r WHERE r.rolname=current_user
            """).fetchone()
            if (not row or row["rolsuper"] or row["owner"] or row["writes"] or row["projection_writes"]
                    or not row["reads"] or not row["review_reads"] or not row["review_saves"] or not row["scaling"]
                    or not row["precomputed"]):
                raise RuntimeError("Archive queries require the restricted reader role and migrations through "
                                   "021_rating_reviews")
            # 黑名单历史是可选功能（迁移 012），缺失时其他归档查询照常可用。
            blacklist = connection.execute("""
                SELECT COALESCE(has_table_privilege(current_user, to_regclass('archive.contact_block_events'), 'SELECT'),
                    FALSE) AND COALESCE(has_table_privilege(current_user, to_regclass('ops.contact_block_state'),
                    'SELECT'), FALSE) AS ready
            """).fetchone()
            BLACKLIST_STATE["ready"] = bool(blacklist and blacklist["ready"])

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

    @staticmethod
    def _blacklist_flags(connection, items):
        """给当页客户/轮次标注黑名单状态：active=拉黑中，past=曾被拉黑。"""
        if not BLACKLIST_STATE["ready"]:
            return items
        keys = {(item.get("source_key"), item.get("account_id"), str(item.get("contact_id")))
                for item in items if item.get("contact_id") and item.get("source_key") and item.get("account_id")}
        if not keys:
            return items
        sources, accounts, contacts = (list(column) for column in zip(*keys))
        rows = connection.execute("""
            SELECT k.source_key,k.account_id,k.contact_id,
                last.action IN ('block','change') AND (last.permanent OR last.blocked_until > now()) AS active
            FROM unnest(%s::text[], %s::bigint[], %s::text[]) AS k(source_key,account_id,contact_id)
            JOIN ops.contact_block_state last ON last.source_key=k.source_key AND last.account_id=k.account_id
                AND last.contact_id=k.contact_id
        """, (sources, accounts, contacts)).fetchall()
        status = {(row["source_key"], row["account_id"], row["contact_id"]): "active" if row["active"] else "past" for row in rows}
        for item in items:
            item["blacklist_status"] = status.get((item.get("source_key"), item.get("account_id"), str(item.get("contact_id"))))
        return items

    @staticmethod
    def _blacklist_history(connection, item, limit: int = 50):
        """客户详情里的拉黑记录（最新在前）。迁移 012 未执行时返回 None。"""
        if not BLACKLIST_STATE["ready"] or not item.get("contact_id"):
            return None
        rows = connection.execute("""
            SELECT action,source,duration_code,blocked_until,permanent,reason_code,reason_label,remark,
                operator_id,operator_name,conversation_id,occurred_at,previous_blocked_until,previous_permanent,
                action IN ('block','change') AND (permanent OR blocked_until > now()) AS open_now
            FROM archive.contact_block_events
            WHERE source_key=%s AND account_id=%s AND contact_id=%s
            ORDER BY source_seq DESC LIMIT %s
        """, (item.get("source_key"), item.get("account_id"), str(item["contact_id"]), limit)).fetchall()
        events = [dict(row) for row in rows]
        latest = events[0] if events else None
        return {"active": bool(latest and latest["open_now"]), "events": events}

    @contextmanager
    def _read(self, budget):
        budget = budget or archive_budget()
        with budget.connection(self.pool, setup=read_only_snapshot) as connection:
            yield connection

    def list_rounds(self, filters: ArchiveFilters, page: int = 1, page_size: int = 30, budget=None):
        join = "FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id"
        with self._read(budget) as connection:
            facts = load_facts(connection, filters)
            where, values = round_conditions(filters, facts)
            total = connection.execute(f"SELECT count(*) AS total {join} WHERE {where}", values).fetchone()["total"]
            rows = connection.execute(f"""
                SELECT r.*, c.display_name AS customer_name, c.contact_id, c.business_user_id,
                    {customer_round_seq('r')} AS customer_round_seq,
                    quality.get_rating_review(r.round_id) AS rating_review, to_jsonb(review) AS review
                {join} {latest_review_join('r')}
                WHERE {where} ORDER BY r.started_at DESC,r.round_id DESC LIMIT %s OFFSET %s
            """, [*values, page_size, (page - 1) * page_size]).fetchall()
            items = self._blacklist_flags(connection, self._agents(connection, [_flatten(row) for row in rows]))
            self._round_facts(connection, items, filters)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def _round_facts(connection, items, filters: ArchiveFilters | None = None):
        """Statistics facts of these rounds (as of the locked time) for cards: the same values the reports use."""
        if not items:
            return {}
        observed = (filters.as_of if filters is not None else None) or connection.execute(
            "SELECT now() AS now").fetchone()["now"]
        ids = [item["round_id"] for item in items]
        source = FactSource.load(connection, observed, round_filter="r.round_id = ANY(%s)", round_params=[ids])
        relation, params = source.facts("f.round_id = ANY(%s)", [ids])
        rows = {row["round_id"]: dict(row) for row in connection.execute(f"""
            SELECT f.round_id,f.assigned_agent_id,f.assigned_agent_name,f.responsible_agent_id,f.assisted,
                f.responding_seats,f.is_lost,f.visitor_messages,f.first_response_seconds,f.average_response_seconds,
                f.response_samples,f.handoff_at,f.human_replied,
                (SELECT COALESCE(sum(extract(epoch FROM upper(s)-lower(s))),0) FROM unnest(f.consultation_spans) s)
                    ::double precision AS consultation_seconds
            FROM {relation}""", params).fetchall()}
        for item in items:
            item["statistics"] = rows.get(item["round_id"])
        return rows

    def overview(self, filters: ArchiveFilters, budget=None):
        """Aggregate only matched rounds, without expanding customers' full history.

        Seat participation uses the statistics facts: "responsible" is the
        assessed (ICS-assigned, not assisted) seat, exactly as in the reports."""
        # One bounded statement computes totals and the limited seat list from
        # one snapshot. No chat bodies or unbounded result sets leave PostgreSQL.
        with self._read(budget) as connection:
            facts = load_facts(connection, filters)
            where, values = round_conditions(filters, facts)
            # Facts of the matched rounds (stored, or live for rounds changed after the locked time).
            observed = filters.as_of or connection.execute("SELECT now() AS now").fetchone()["now"]
            matched = (f"r.round_id IN (SELECT r.round_id FROM ops.round_index r LEFT JOIN ops.customer_index c"
                       f" ON c.customer_id=r.customer_id WHERE {where})")
            source = FactSource.load(connection, observed, round_filter=matched, round_params=values)
            relation, relation_values = source.facts()
            result = dict(connection.execute(f"""
                WITH selected AS MATERIALIZED (
                    -- Only the scalar facts: materializing the whole JSON
                    -- projection per round spilled to disk on long ranges.
                    SELECT r.round_id,r.source_key,r.account_id,r.customer_id,
                        r.reception_type,r.status,r.rating_status,r.rating,
                        r.data->>'responsibility_basis' AS responsibility_basis,
                        f.responsible_agent_id,f.responsible_agent_name,f.assisted,f.assigned_agent_id,
                        CASE WHEN NOT EXISTS (SELECT 1 FROM quality.round_rating_review_current rating_review
                            WHERE rating_review.round_id=f.round_id AND rating_review.status='invalid')
                            THEN f.rating END AS fact_rating,
                        r.data #>> '{{counts,agent}}' AS public_agent_replies
                    FROM ops.round_index r
                    LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id
                    LEFT JOIN {relation} ON f.round_id=r.round_id
                    WHERE {where}
                ), participation AS (
                    SELECT r.source_key,r.account_id,r.round_id,
                        r.responsible_agent_id AS agent_id,
                        r.responsible_agent_name AS agent_name,
                        TRUE AS responsible,FALSE AS replied,
                        r.fact_rating < 4 AS low_rating
                    FROM selected r
                    WHERE r.responsible_agent_id IS NOT NULL
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
                    count(*) FILTER (WHERE fact_rating IS NOT NULL) AS rated_rounds,
                    count(*) FILTER (WHERE fact_rating < 4) AS low_rating_rounds,
                    (avg(fact_rating))::double precision AS average_rating,
                    count(*) FILTER (WHERE assisted) AS assisted_rounds,
                    count(*) FILTER (WHERE reception_type='human'
                        AND public_agent_replies = '0') AS human_without_public_reply_rounds,
                    count(*) FILTER (WHERE reception_type='human' AND assigned_agent_id IS NULL)
                        AS responsibility_unrecorded_rounds,
                    (SELECT count(*) FROM agent_totals) AS participating_agent_count,
                    COALESCE((SELECT jsonb_agg(to_jsonb(limited)
                        ORDER BY responsible_rounds DESC,replied_rounds DESC,source_key,account_id,agent_id)
                        FROM (SELECT * FROM agent_totals
                            ORDER BY responsible_rounds DESC,replied_rounds DESC,source_key,account_id,agent_id
                            LIMIT {OVERVIEW_AGENT_LIMIT}) limited),'[]'::jsonb) AS participating_agents,
                    CURRENT_TIMESTAMP AS generated_at
                FROM selected
            """, [*relation_values, *values]).fetchone())
        result["agents_truncated"] = result["participating_agent_count"] > OVERVIEW_AGENT_LIMIT
        result["generated_at"] = result["generated_at"].isoformat()
        return result

    def list_customers(self, filters: ArchiveFilters, page: int = 1, page_size: int = 30, budget=None):
        scoped = customer_round_scoped(filters)
        locked, locked_values = as_of_message(filters)
        stored_rounds, stored_messages, stored_round_values, stored_message_values = stored_counts(filters)
        with self._read(budget) as connection:
            facts = load_facts(connection, filters)
            condition, values = customer_conditions(filters, facts)
            total = connection.execute(f"SELECT count(*) AS total FROM ops.customer_index c WHERE {condition}", values).fetchone()["total"]
            if scoped:
                # 有轮次级筛选：卡片上的轮次/消息数、最近一轮只统计命中的轮次，另给出该用户总轮次供对照。
                where, round_values = customer_scope_round_conditions(filters, facts)
                matched = f"SELECT r.round_id FROM ops.round_index r WHERE r.customer_id=c.customer_id AND {where}"
                message_scope, message_values = f"m.round_id IN ({matched})", list(round_values)
                unrounded = customer_unrounded_message_predicate(filters, "c.customer_id")
                if unrounded is not None:
                    message_scope = f"({message_scope} OR ({unrounded[0]}))"
                    message_values.extend(unrounded[1])
                counts = f"""(SELECT count(*) FROM ({matched}) hit) AS round_count,
                    (SELECT count(*) FROM ops.message_index m WHERE m.customer_id=c.customer_id AND {message_scope}
                        AND {locked}) AS message_count,
                    {stored_rounds} AS total_round_count"""
                count_values = [*round_values, *message_values, *locked_values, *stored_round_values]
            else:
                counts = f"""{stored_rounds} AS round_count, {stored_messages} AS message_count"""
                count_values = [*stored_round_values, *stored_message_values]
            rows = connection.execute(f"""
                SELECT c.*, {counts}
                FROM ops.customer_index c WHERE {condition}
                ORDER BY c.last_message_at DESC NULLS LAST,c.customer_id LIMIT %s OFFSET %s
            """, [*count_values, *values, page_size, (page - 1) * page_size]).fetchall()
            items = [_flatten(row) for row in rows]
            for item in items:
                item["filter_scoped"] = scoped
            self._customer_service_metrics(connection, items, filters if scoped else None, facts,
                                           as_of=filters.as_of)
            self._blacklist_flags(connection, items)
        return {"items": items, "total": total, "page": page, "page_size": page_size, "filter_scoped": scoped}

    @classmethod
    def _customer_service_metrics(cls, connection, customers, scope: ArchiveFilters | None = None,
                                  facts: FactSource | None = None, as_of: datetime | None = None):
        """Read only the latest (matched) round for customers on this page, in two batches.

        Response timings and the assisted flag come from the statistics facts
        (same values as the reports); the card's display-only figures (round
        duration, message counts, last speaker) are read from the messages."""
        if not customers:
            return
        scope_sql, scope_values = "", []
        if scope is not None:
            where, scope_values = customer_scope_round_conditions(scope, facts)
            scope_sql = f" AND {where}"
        elif as_of is not None:
            scope_sql, scope_values = " AND r.started_at < %s", [as_of]
        rows = connection.execute(f"""
            SELECT latest.*,{customer_round_seq('latest')} AS customer_round_seq,
                quality.get_rating_review(latest.round_id) AS rating_review, to_jsonb(review) AS review FROM (
                SELECT DISTINCT ON (r.customer_id) r.* FROM ops.round_index r
                WHERE r.customer_id=ANY(%s){scope_sql}
                ORDER BY r.customer_id,r.started_at DESC,r.round_id DESC
            ) latest {latest_review_join('latest')}
        """, ([item["customer_id"] for item in customers], *scope_values)).fetchall()
        rounds = {row["customer_id"]: _flatten(row) for row in rows}
        metrics = {row["round_id"]: RoundServiceMetrics(row) for row in rounds.values()}
        if metrics:
            # A server cursor keeps memory proportional to the displayed customer
            # count, even when a selected round contains a long chat history.
            with connection.cursor(name="quality_service_metrics_" + uuid.uuid4().hex) as cursor:
                cursor.execute("""
                    SELECT round_id,message_id,sent_at,sender_role,direction,is_private,
                        data->'is_rating' AS is_rating,sender_id,
                        NOT is_deleted AND (COALESCE(content,'') ~ '[^[:space:]]'
                            OR jsonb_array_length(CASE WHEN jsonb_typeof(data->'attachments')='array'
                                THEN data->'attachments' ELSE '[]'::jsonb END) > 0) AS valid_content
                    FROM ops.message_index WHERE round_id=ANY(%s) AND (%s::timestamptz IS NULL OR sent_at IS NULL OR sent_at < %s)
                    ORDER BY round_id,sent_at NULLS LAST,message_id
                """, (list(metrics), as_of, as_of))
                while batch := cursor.fetchmany(500):
                    for message in batch:
                        metrics[message["round_id"]].observe(message)
        latest_rounds = list(rounds.values())
        stats = cls._round_facts(connection, latest_rounds, ArchiveFilters(as_of=as_of))
        for customer in customers:
            latest = rounds.get(customer["customer_id"])
            customer["latest_round"] = ({**latest, "service_metrics": statistics_metrics(
                metrics[latest["round_id"]].result(), stats.get(latest["round_id"]))} if latest else None)

    def round_detail(self, round_id: str, page: int = 1, page_size: int = 100, as_of: datetime | None = None, budget=None):
        locked, locked_values = as_of_message(ArchiveFilters(as_of=as_of))
        with self._read(budget) as connection:
            row = connection.execute(f"""
                SELECT r.*,c.display_name AS customer_name,c.contact_id,c.business_user_id,c.data AS customer_profile,
                    {customer_round_seq('r')} AS customer_round_seq,
                    quality.get_rating_review(r.round_id) AS rating_review, to_jsonb(review) AS review
                FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id
                {latest_review_join('r')}
                WHERE r.round_id=%s
            """, (round_id,)).fetchone()
            if row is None:
                return None
            round_item = self._agents(connection, [_flatten(row)])[0]
            self._customer_profile(connection, round_item, round_item.pop("customer_profile", None))
            round_item["blacklist"] = self._blacklist_history(connection, round_item)
            self._round_facts(connection, [round_item], ArchiveFilters(as_of=as_of))
            messages, total = self._messages(connection, f"m.round_id=%s AND {locked}", [round_id, *locked_values],
                                             page, page_size)
        return {"round": round_item, "messages": messages, "total": total, "page": page, "page_size": page_size}

    def rating_review(self, round_id: str, budget=None):
        """同一快照读取完整评分内容和复核版本。"""
        with self._read(budget) as connection:
            return connection.execute("SELECT quality.get_rating_review(%s) AS review", (round_id,)).fetchone()["review"]

    def save_rating_review(self, round_id: str, status: str, reviewer: str, revision: int, content_revision: str):
        """仅调用受限函数，原子保存结论与统计失效日期。"""
        try:
            with self.pool.connection() as connection:
                connection.execute("SET TRANSACTION READ WRITE")
                return connection.execute("SELECT quality.save_rating_review(%s,%s,%s,%s,%s) AS review",
                    (round_id, status, reviewer, revision, content_revision)).fetchone()["review"]
        except SerializationFailure:
            raise ReviewConflict() from None
        except InvalidParameterValue as error:
            raise ReviewValidationError(str(error).splitlines()[0]) from None

    def round_review(self, round_id: str, budget=None):
        with self._read(budget) as connection:
            row = connection.execute("""
                SELECT r.round_id,r.reception_type,review.ai_status,review.human_status,review.remark,review.reviewer,
                    review.reviewed_at,review.review_id::text AS revision
                FROM ops.round_index r LEFT JOIN quality.round_review_current review
                    ON review.round_id=r.round_id WHERE r.round_id=%s
            """, (round_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "ai_status": row["ai_status"] or "pending",
                "human_status": row["human_status"] if row["revision"] else (
                    None if row["reception_type"] == "ai_only" else "pending"),
                "remark": row["remark"] or "", "revision": row["revision"] or "0"}

    def save_round_review(self, round_id: str, ai_status: str, human_status: str | None,
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
        except InvalidParameterValue as error:
            scene = error.diag.message_detail
            scene = scene if scene in {"ai_only", "human"} else None
            message = ("纯 AI 轮次不适用人工质检，请重新选择 AI 结论。" if scene == "ai_only" else
                       "接待分类已变更为人工介入，请选择完整结论组合。" if scene == "human" else
                       "质检结论或备注无效。")
            raise ReviewValidationError(message, scene) from None

    @staticmethod
    def _messages(connection, where: str, values: list, page: int, page_size: int):
        total = connection.execute(f"SELECT count(*) AS total FROM ops.message_index m WHERE {where}", values).fetchone()["total"]
        rows = connection.execute(f"""
            SELECT m.*,r.round_no,r.channel_key,r.reception_type,
                CASE WHEN r.round_id IS NOT NULL THEN {customer_round_seq('r')} END AS customer_round_seq,
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
        """从原始客户资料补全字段，空缺资料仍可从历史记录恢复。"""
        data = customer_profile if isinstance(customer_profile, dict) else {}
        profile = {key: data.get(key) for key in ("h5_user_id", "chatwoot_user_id", "phone_number", "contact_note")}
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

    def customer_history(self, customer_id: str, page: int = 1, page_size: int = 100,
                         filters: ArchiveFilters | None = None, budget=None):
        """用户聊天记录。带轮次级筛选时只返回命中轮次内的消息，以及命中纯消息条件的未归轮消息。"""
        scoped = customer_round_scoped(filters)
        locked, locked_values = as_of_message(filters)
        with self._read(budget) as connection:
            if scoped:
                where, round_values = customer_scope_round_conditions(
                    filters, load_facts(connection, filters, customer_id=customer_id))
                matched = f"SELECT r.round_id FROM ops.round_index r WHERE r.customer_id=%s AND {where}"
                message_scope = f"m.customer_id=%s AND (m.round_id IN ({matched})"
                message_values = [customer_id, customer_id, *round_values]
                unrounded = customer_unrounded_message_predicate(filters, "%s")
                if unrounded is not None:
                    message_scope += f" OR ({unrounded[0]})"
                    message_values.extend([customer_id, *unrounded[1]])
                message_scope += f") AND {locked}"
                message_values.extend(locked_values)
                customer = connection.execute(f"""
                    SELECT c.*, (SELECT count(*) FROM ({matched}) hit) AS round_count,
                        (SELECT count(*) FROM ops.message_index m WHERE {message_scope}) AS message_count,
                        c.round_total AS total_round_count
                    FROM ops.customer_index c WHERE c.customer_id=%s
                """, (customer_id, *round_values, *message_values, customer_id)).fetchone()
            else:
                stored_rounds, stored_messages, round_params, message_params = stored_counts(filters)
                customer = connection.execute(f"""
                    SELECT c.*, {stored_rounds} AS round_count, {stored_messages} AS message_count
                    FROM ops.customer_index c WHERE c.customer_id=%s
                """, [*round_params, *message_params, customer_id]).fetchone()
            if customer is None:
                return None
            customer = self._customer_profile(connection, _flatten(customer), customer.get("data"))
            customer["blacklist"] = self._blacklist_history(connection, customer)
            customer["filter_scoped"] = scoped
            if scoped:
                messages, total = self._messages(connection, message_scope, message_values, page, page_size)
            else:
                messages, total = self._messages(connection, f"m.customer_id=%s AND {locked}",
                                                 [customer_id, *locked_values], page, page_size)
        return {"customer": customer, "messages": messages, "total": total, "page": page, "page_size": page_size,
                "filter_scoped": scoped}

    def customer_rounds(self, customer_id: str, page: int = 1, page_size: int = 50,
                        filters: ArchiveFilters | None = None, budget=None):
        """Read a customer's round directory without loading any message bodies.

        带轮次级筛选时只列出命中的轮次，质检标记只能落在这些轮次上。"""
        scoped = customer_round_scoped(filters)
        scope_sql, scope_values = "", []
        with self._read(budget) as connection:
            if scoped:
                where, scope_values = customer_scope_round_conditions(
                    filters, load_facts(connection, filters, customer_id=customer_id))
                scope_sql = f" AND {where}"
            elif filters is not None and filters.as_of is not None:
                scope_sql, scope_values = " AND r.started_at < %s", [filters.as_of]
            customer = connection.execute(
                "SELECT customer_id FROM ops.customer_index WHERE customer_id=%s", (customer_id,)
            ).fetchone()
            if customer is None:
                return None
            total = connection.execute(
                f"SELECT count(*) AS total FROM ops.round_index r WHERE r.customer_id=%s{scope_sql}",
                (customer_id, *scope_values),
            ).fetchone()["total"]
            rows = connection.execute(f"""
                SELECT r.*,c.display_name AS customer_name,c.contact_id,c.business_user_id,
                    {customer_round_seq('r')} AS customer_round_seq,
                    quality.get_rating_review(r.round_id) AS rating_review, to_jsonb(review) AS review
                FROM ops.round_index r JOIN ops.customer_index c ON c.customer_id=r.customer_id
                {latest_review_join('r')}
                WHERE r.customer_id=%s{scope_sql}
                ORDER BY r.started_at DESC,r.round_id DESC LIMIT %s OFFSET %s
            """, (customer_id, *scope_values, page_size, (page - 1) * page_size)).fetchall()
            items = [_flatten(row) for row in rows]
            self._round_facts(connection, items, filters)
        return {"items": items, "total": total, "page": page, "page_size": page_size, "filter_scoped": scoped}

    def message_versions(self, source_key: str, account_id: int, message_id: int, page: int = 1, page_size: int = 100, budget=None):
        with self._read(budget) as connection:
            values = (source_key, account_id, message_id)
            where = "source_key=%s AND account_id=%s AND message_id=%s"
            total = connection.execute(f"SELECT count(*) AS total FROM archive.message_snapshots WHERE {where}", values).fetchone()["total"]
            rows = connection.execute(f"""
                SELECT id,event_id,snapshot_kind,content,sender_role,sender_name,is_private,sent_at,
                    source_updated_at,recorded_at,content_attributes FROM archive.message_snapshots
                WHERE {where} ORDER BY COALESCE(source_updated_at,sent_at,recorded_at),id LIMIT %s OFFSET %s
            """, (*values, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def filter_options(self, budget=None):
        """筛选下拉项。每类只按索引跳跃读取不同取值（loose index scan），代价与取值个数相关，
        不随归档消息/轮次总量增长；坐席显示名取该来源下最近一条记录的名称。"""
        from ics_agent.channels import list_channels

        with self._read(budget) as connection:
            # The receipt index is available before projection succeeds.
            sources = connection.execute("""
                WITH RECURSIVE found AS (
                    (SELECT source_key,account_id FROM archive.events ORDER BY source_key,account_id LIMIT 1)
                    UNION ALL
                    SELECT next.source_key,next.account_id FROM found CROSS JOIN LATERAL (
                        SELECT e.source_key,e.account_id FROM archive.events e
                        WHERE (e.source_key,e.account_id) > (found.source_key,found.account_id)
                        ORDER BY e.source_key,e.account_id LIMIT 1) next
                ) SELECT source_key,account_id FROM found ORDER BY source_key,account_id LIMIT 500
            """).fetchall()
            agents = connection.execute("""
                WITH RECURSIVE scopes AS (
                    (SELECT source_key,account_id FROM archive.events ORDER BY source_key,account_id LIMIT 1)
                    UNION ALL
                    SELECT next.source_key,next.account_id FROM scopes CROSS JOIN LATERAL (
                        SELECT e.source_key,e.account_id FROM archive.events e
                        WHERE (e.source_key,e.account_id) > (scopes.source_key,scopes.account_id)
                        ORDER BY e.source_key,e.account_id LIMIT 1) next
                ), responsible AS (
                    (SELECT source_key,account_id,data->>'responsible_agent_id' AS agent_id FROM ops.round_index
                     WHERE data->>'responsibility_basis'='ics_assignment'
                     ORDER BY source_key,account_id,data->>'responsible_agent_id' LIMIT 1)
                    UNION ALL
                    SELECT next.* FROM responsible CROSS JOIN LATERAL (
                        SELECT r.source_key,r.account_id,r.data->>'responsible_agent_id' AS agent_id FROM ops.round_index r
                        WHERE r.data->>'responsibility_basis'='ics_assignment'
                            AND (r.source_key,r.account_id,r.data->>'responsible_agent_id')
                                > (responsible.source_key,responsible.account_id,responsible.agent_id)
                        ORDER BY r.source_key,r.account_id,r.data->>'responsible_agent_id' LIMIT 1) next
                ), replied_ids AS (
                    (SELECT agent_id FROM ops.round_agent_index ORDER BY agent_id LIMIT 1)
                    UNION ALL
                    SELECT next.agent_id FROM replied_ids CROSS JOIN LATERAL (
                        SELECT a.agent_id FROM ops.round_agent_index a WHERE a.agent_id > replied_ids.agent_id
                        ORDER BY a.agent_id LIMIT 1) next
                ), assignee_ids AS (
                    (SELECT current_assignee_id AS agent_id FROM ops.conversation_index
                     WHERE current_assignee_id IS NOT NULL ORDER BY current_assignee_id LIMIT 1)
                    UNION ALL
                    SELECT next.agent_id FROM assignee_ids CROSS JOIN LATERAL (
                        SELECT ci.current_assignee_id AS agent_id FROM ops.conversation_index ci
                        WHERE ci.current_assignee_id > assignee_ids.agent_id
                        ORDER BY ci.current_assignee_id LIMIT 1) next
                ), named AS (
                    SELECT p.source_key,p.account_id,p.agent_id,latest.agent_name FROM responsible p
                    LEFT JOIN LATERAL (SELECT r.data->>'responsible_agent_name' AS agent_name FROM ops.round_index r
                        WHERE r.data->>'responsibility_basis'='ics_assignment' AND r.source_key=p.source_key
                            AND r.account_id=p.account_id AND r.data->>'responsible_agent_id'=p.agent_id
                        ORDER BY r.started_at DESC,r.round_id DESC LIMIT 1) latest ON TRUE
                    WHERE NULLIF(p.agent_id,'') IS NOT NULL
                    UNION ALL
                    SELECT s.source_key,s.account_id,i.agent_id,seen.agent_name FROM replied_ids i CROSS JOIN scopes s
                    CROSS JOIN LATERAL (SELECT a.agent_name FROM ops.round_agent_index a
                        JOIN ops.round_index r ON r.round_id=a.round_id
                        WHERE a.agent_id=i.agent_id AND r.source_key=s.source_key AND r.account_id=s.account_id
                        ORDER BY a.round_id DESC LIMIT 1) seen
                    UNION ALL
                    SELECT s.source_key,s.account_id,i.agent_id,NULL::text FROM assignee_ids i CROSS JOIN scopes s
                    WHERE NULLIF(i.agent_id,'') IS NOT NULL AND EXISTS(SELECT 1 FROM ops.conversation_index c
                        WHERE c.current_assignee_id=i.agent_id AND c.source_key=s.source_key AND c.account_id=s.account_id)
                )
                SELECT source_key,account_id,agent_id,
                    COALESCE(max(NULLIF(agent_name,'')), '#' || agent_id) AS agent_name
                FROM named GROUP BY source_key,account_id,agent_id
                ORDER BY source_key,account_id,agent_name,agent_id LIMIT 1000
            """).fetchall()
            inboxes = connection.execute("""
                WITH RECURSIVE round_inboxes AS (
                    (SELECT source_key,account_id,inbox_id FROM ops.round_index WHERE inbox_id IS NOT NULL
                     ORDER BY source_key,account_id,inbox_id LIMIT 1)
                    UNION ALL
                    SELECT next.* FROM round_inboxes CROSS JOIN LATERAL (
                        SELECT r.source_key,r.account_id,r.inbox_id FROM ops.round_index r
                        WHERE r.inbox_id IS NOT NULL AND (r.source_key,r.account_id,r.inbox_id)
                            > (round_inboxes.source_key,round_inboxes.account_id,round_inboxes.inbox_id)
                        ORDER BY r.source_key,r.account_id,r.inbox_id LIMIT 1) next
                ), conversation_inboxes AS (
                    (SELECT source_key,account_id,inbox_id FROM ops.conversation_index WHERE inbox_id IS NOT NULL
                     ORDER BY source_key,account_id,inbox_id LIMIT 1)
                    UNION ALL
                    SELECT next.* FROM conversation_inboxes CROSS JOIN LATERAL (
                        SELECT c.source_key,c.account_id,c.inbox_id FROM ops.conversation_index c
                        WHERE c.inbox_id IS NOT NULL AND (c.source_key,c.account_id,c.inbox_id)
                            > (conversation_inboxes.source_key,conversation_inboxes.account_id,conversation_inboxes.inbox_id)
                        ORDER BY c.source_key,c.account_id,c.inbox_id LIMIT 1) next
                ), inboxes AS (
                    SELECT source_key,account_id,inbox_id FROM round_inboxes
                    UNION SELECT source_key,account_id,inbox_id FROM conversation_inboxes
                )
                SELECT i.source_key,i.account_id,i.inbox_id,'#' || i.inbox_id::text AS name,
                    (SELECT max(channel_key) FROM (
                        (SELECT r.channel_key FROM ops.round_index r WHERE r.source_key=i.source_key
                            AND r.account_id=i.account_id AND r.inbox_id=i.inbox_id AND r.channel_key IS NOT NULL
                            ORDER BY r.channel_key DESC LIMIT 1)
                        UNION ALL
                        (SELECT c.channel_key FROM ops.conversation_index c WHERE c.source_key=i.source_key
                            AND c.account_id=i.account_id AND c.inbox_id=i.inbox_id AND c.channel_key IS NOT NULL
                            ORDER BY c.channel_key DESC LIMIT 1)) channels) AS channel_key
                FROM inboxes i ORDER BY i.source_key,i.account_id,i.inbox_id LIMIT 1000
            """).fetchall()
        return {
            "sources": [dict(row) for row in sources],
            "channels": [{"key": channel.key, "name": channel.name} for channel in list_channels()],
            "agents": [dict(row) for row in agents],
            "inboxes": [dict(row) for row in inboxes],
        }

    def raw_event(self, event_id: int, source_key: str, account_id: int, budget=None):
        with self._read(budget) as connection:
            row = connection.execute(
                "SELECT raw_body FROM archive.events WHERE id=%s AND source_key=%s AND account_id=%s",
                (event_id, source_key, account_id),
            ).fetchone()
        return None if row is None else bytes(row["raw_body"])

    def unlinked_ratings(self, page: int = 1, page_size: int = 30, budget=None):
        with self._read(budget) as connection:
            total = connection.execute("SELECT count(*) AS total FROM ops.rating_index WHERE status='unlinked'").fetchone()["total"]
            rows = connection.execute("""
                SELECT r.*,s.event_id,NULLIF(r.data ->> 'source_time', '')::timestamptz AS rating_at FROM ops.rating_index r
                JOIN archive.rating_snapshots s ON s.id=r.snapshot_id WHERE r.status='unlinked'
                ORDER BY rating_at DESC NULLS LAST,r.snapshot_id DESC LIMIT %s OFFSET %s
            """, (page_size, (page - 1) * page_size)).fetchall()
        return {"items": [_flatten(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def processing_status(self, budget=None):
        # 只统计未完成状态（页面只用这些），走 event_jobs_unfinished_idx；
        # 已完成任务随事件只增不减，按状态全表分组会随归档总量线性变慢。
        with self._read(budget) as connection:
            rows = connection.execute("""SELECT status,count(*) AS count FROM ops.event_jobs
                WHERE status IN ('queued','retry','processing','failed') GROUP BY status""").fetchall()
            latest = connection.execute("SELECT max(received_at) AS last_received_at FROM archive.events").fetchone()
            unlinked = connection.execute("SELECT count(*) AS unlinked_rating_count FROM ops.rating_index WHERE status='unlinked'").fetchone()
        return {"jobs": {row["status"]: row["count"] for row in rows}, **dict(latest), **dict(unlinked)}
