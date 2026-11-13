"""通过一致性快照流式提取消息；关闭迭代器后才开始 HTTP 下载。"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from psycopg import errors as pg_errors

from quality.query.config import QuerySettings
from quality.query.agents import agent_name_index, archived_agent_name
from quality.query.responsibility import normalize_responsibility
from quality.query.export import ExportRepository
from quality.processing.service_facts import FactSource
from quality.query.repository import (ArchiveFilters, TIME_EXPRESSIONS, _flatten, customer_conditions,
                                      customer_round_seq_cte, customer_unrounded_message_predicate,
                                      load_facts, needs_facts)
from .bundle import BundleLimit
from .filter_snapshot import SNAPSHOT_FIELDS, snapshot_columns, version_rows, safe_rating_review, validate_projection, casefold_mapping
from quality.query.filter_contract import FILTER_CONTRACT_VERSION, normalize_ids
from quality.processing.service_facts import FACT_VERSION

# 筛选导出保留命中的整轮；按客户视图另保留命中的未归轮消息，旧任务默认按轮次视图处理。
FILTERED_MODES = ("rounds", "customers")
QUERY_TIMEOUT_MESSAGE = "归档库查询超时：导出范围内数据过多，请缩小时间范围或增加筛选条件后重试。"
logger = logging.getLogger(__name__)


# 仅导出查看留档所需字段，避免夹带原始回调中的凭据等元数据。
FIELDS = ("source_key", "account_id", "customer_id", "customer_name", "contact_id", "h5_user_id",
          "conversation_id", "message_id", "round_id", "round_no", "customer_round_seq", "round_started_at",
          "round_status", "sent_at", "sender_id", "sender_name", "sender_role", "direction", "is_private",
          "is_deleted", "is_rating", "content", "reception_type", "ai_review_status", "human_review_status",
          "review_remark", "visitor_rating", "round_rating_status", "round_rating", "round_rating_feedback", "rating_review",
          "resolved_at", "closed_at", "last_message_at", "responsibility_basis", "responsible_assigned_at",
          "handoff_requested_at", "takeover_inherited", "responsibility_trigger_message_id", "valid_content",
          "assigned_agent_id", "assigned_agent_name", "responsible_agent_id", "responsible_agent_name",
          "assisted", "responding_seats", "is_lost", "handoff_at", "human_replied", "first_response_seconds",
          "average_response_seconds", "response_samples", "snapshot_at", *SNAPSHOT_FIELDS)


class _ScopedFacts:
    """把来源与账号带入事实筛选，使用坐席复合索引的前导列，保持原查询语义。"""

    def __init__(self, source, filters):
        self.source = source
        self.scope = []
        self.params = []
        for name in ("source_key", "account_id"):
            value = getattr(filters, name)
            if value is not None:
                self.scope.append(f"f.{name}=%s")
                self.params.append(value)

    def facts(self, where="TRUE", params=(), alias="f"):
        return self.source.facts(" AND ".join([*self.scope, f"({where})"]), [*self.params, *params], alias)

    def __getattr__(self, name):
        return getattr(self.source, name)


def _scope(request, connection=None):
    """统一返回消息范围与事实所需的轮次范围，避免选择条件和参数顺序漂移。"""
    mode = request["mode"]
    if mode == "all":
        return "TRUE", [], None, [], None
    if mode == "month":
        year, month = map(int, request["month"].split("-"))
        zone = timezone(timedelta(hours=8))
        start = datetime(year, month, 1, tzinfo=zone)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
        ended = TIME_EXPRESSIONS["resolved_at"]
        selection = ("SELECT r.round_id FROM ops.round_index r WHERE "
                     f"(COALESCE(r.data ->> 'resolved_at', r.data ->> 'closed_at') IS NOT NULL "
                     f"AND ({ended} >= %s AND {ended} < %s) IS TRUE) "
                     f"OR ({ended} IS NULL AND (r.last_message_at >= %s AND r.last_message_at < %s) IS TRUE)")
        parameters = [start, end, start, end]
        where = (f"(m.round_id IN ({selection}) OR "
                 "(m.round_id IS NULL AND (m.sent_at >= %s AND m.sent_at < %s) IS TRUE))")
        return where, [*parameters, start, end], selection, parameters, None
    if mode not in FILTERED_MODES:
        raise ValueError("Unsupported offline archive scope")
    values = dict(request.get("filters") or {})
    for field in ("start", "end", "as_of"):
        if isinstance(values.get(field), str):
            values[field] = datetime.fromisoformat(values[field])
    filters = ArchiveFilters(**values)
    # 统计口径条件（考核坐席、报表下钻等）读每轮统计事实，需要在导出快照的连接里准备。
    facts = load_facts(connection, filters) if needs_facts(filters) else None
    if facts is not None:
        facts = _ScopedFacts(facts, filters)
    # 只取命中筛选的轮次（时间、评价、质检、关键词等都在轮次上判断），导出这些轮次截至锁定时刻的全部消息。
    selection, parameters = ExportRepository._selection("rounds", filters, facts=facts)
    where, values = f"m.round_id IN ({selection})", list(parameters)
    if request.get("view", "rounds") == "customers":
        unrounded = customer_unrounded_message_predicate(filters, "m.customer_id")
        if unrounded is not None:
            # 用户级条件复用页面函数；移除消息与轮次条件，避免额外展开用户的其他记录。
            customer_filters = ArchiveFilters(source_key=filters.source_key, account_id=filters.account_id,
                                              customer_query=filters.customer_query, blacklist=filters.blacklist)
            customer_where, customer_values = customer_conditions(customer_filters)
            where = (f"({where} OR ({unrounded[0]} AND m.customer_id IN "
                     f"(SELECT c.customer_id FROM ops.customer_index c WHERE {customer_where})))")
            values.extend([*unrounded[1], *customer_values])
    if filters.as_of is not None:
        where += " AND (m.sent_at IS NULL OR m.sent_at < %s)"
        values.append(filters.as_of)
    return where, values, selection, parameters, filters


def scope_predicate(request, connection=None):
    """预估与正式提取共用同一消息范围。"""
    where, parameters, *_ = _scope(request, connection)
    return where, parameters


def count_statement(where, detailed=True):
    """导出范围的数量统计；只扫描 message_index，不排序，比正式导出便宜得多。"""
    if not detailed:
        return f"SELECT count(*) AS messages, count(DISTINCT m.round_id) AS rounds FROM ops.message_index m WHERE {where}"
    return f"""
        SELECT count(*) AS messages, count(DISTINCT m.round_id) AS rounds,
            count(DISTINCT m.customer_id) AS customers,
            count(*) FILTER (WHERE m.round_id IS NULL) AS unrounded_messages,
            COALESCE(sum(octet_length(m.content)), 0) AS text_bytes,
            COALESCE(sum(att.n), 0) AS attachments, COALESCE(sum(att.bytes), 0) AS attachment_bytes,
            min(m.sent_at) AS first_at, max(m.sent_at) AS last_at
        FROM ops.message_index m
        LEFT JOIN LATERAL (
            SELECT count(*) AS n, COALESCE(sum(CASE WHEN a->>'expected_byte_count' ~ '^[0-9]{{1,15}}$'
                THEN (a->>'expected_byte_count')::bigint END), 0) AS bytes
            FROM jsonb_array_elements(CASE WHEN jsonb_typeof(m.data->'attachments') = 'array'
                THEN m.data->'attachments' ELSE '[]'::jsonb END) a
        ) att ON TRUE
        WHERE {where}
    """


def limit_message(counts, settings):
    """超过硬上限时返回拒绝原因；未超限返回 None。"""
    if counts["messages"] > settings.max_messages:
        return (f"导出范围内有 {counts['messages']:,} 条消息，超过单个离线页面上限 {settings.max_messages:,} 条"
                "（ARCHIVE_OFFLINE_MAX_MESSAGES），浏览器很可能无法打开。请缩小时间范围或增加筛选条件后分批导出。")
    if counts["rounds"] > settings.max_rounds:
        return (f"导出范围内有 {counts['rounds']:,} 轮对话，超过单个离线页面上限 {settings.max_rounds:,} 轮"
                "（ARCHIVE_OFFLINE_MAX_ROUNDS）。请缩小时间范围或增加筛选条件后分批导出。")
    return None


def assess(counts, settings):
    """把数量统计换算成提醒级别：empty / ok / warn / block。"""
    text = int(counts.get("text_bytes") or 0)
    media = min(int(counts.get("attachment_bytes") or 0), settings.max_embedded_total_bytes)
    # 粗估：正文及元数据约为正文字节的 2 倍再加每条约 600 字节结构；内嵌附件 base64 约 ×1.34。
    estimated = int(text * 2 + counts["messages"] * 600 + media * 1.34)
    warnings = []
    blocked = limit_message(counts, settings)
    if counts["messages"] > settings.warn_messages:
        warnings.append(f"消息 {counts['messages']:,} 条，超过提醒线 {settings.warn_messages:,} 条")
    if counts["rounds"] > settings.warn_rounds:
        warnings.append(f"对话 {counts['rounds']:,} 轮，超过提醒线 {settings.warn_rounds:,} 轮")
    if estimated > 500 * 1024**2:
        warnings.append(f"预计页面约 {estimated / 1024**2:,.0f} MB，打开时浏览器会占用较多内存")
    if int(counts.get("attachment_bytes") or 0) > settings.max_embedded_total_bytes:
        warnings.append("附件总量超过页面内嵌上限，超出部分只保留原始链接")
    level = "block" if blocked else "empty" if not counts["messages"] else "warn" if warnings else "ok"
    return {"level": level, "reason": blocked, "warnings": warnings, "estimated_bytes": estimated,
            "limits": {"warn_messages": settings.warn_messages, "max_messages": settings.max_messages,
                       "warn_rounds": settings.warn_rounds, "max_rounds": settings.max_rounds}}


def estimate(request, settings):
    """导出前预估。只读、关闭并行、短超时；超时按“超限”返回，不抛到接口层。"""
    repository = ExportRepository(QuerySettings())
    try:
        repository.open()
        with repository.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute(f"SET LOCAL statement_timeout = '{int(settings.estimate_timeout_seconds)}s'")
            connection.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            where, parameters = scope_predicate(request, connection)
            try:
                row = connection.execute(count_statement(where), parameters).fetchone()
            except pg_errors.QueryCanceled:
                connection.rollback()
                return {"messages": None, "rounds": None, "customers": None, "level": "block", "warnings": [],
                        "reason": (f"预估查询超过 {settings.estimate_timeout_seconds} 秒仍未完成，说明范围内数据过多，"
                                   "正式导出大概率会超时失败。请缩小时间范围或增加筛选条件后重试。")}
        # sum() 返回 Decimal，统一转成整数便于 JSON 输出。
        counts = {key: int(row[key] or 0) for key in row.keys() if key not in ("first_at", "last_at")}
        counts.update(first_at=iso(row["first_at"]), last_at=iso(row["last_at"]))
        return {**counts, **assess(counts, settings)}
    finally:
        repository.close()


def iso(value):
    """统一输出带时区的 ISO 8601，保证 Safari 等浏览器都能解析。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def message_statement(where, facts_relation="ops.round_service_facts f"):
    """消息按客户、会话、轮次、发送时间排序，保证同一轮（及同一会话的未归轮消息）连续，
    查看页目录中每轮只出现一次。"""
    return f"""
        WITH {customer_round_seq_cte(f"SELECT m.customer_id FROM ops.message_index m WHERE {where}")}
        SELECT m.*, {snapshot_columns()}, c.display_name AS customer_name, c.contact_id,
            COALESCE(NULLIF(c.data->>'h5_user_id',''),c.data->>'unverified_identifier') AS h5_user_id,
            r.round_no, seq.customer_round_seq, r.started_at AS round_started_at, r.status AS round_status,
            r.reception_type, quality.get_rating_review(r.round_id) AS rating_review, r.rating_status AS round_rating_status,
            CASE WHEN r.rating_status='rated' THEN r.rating END AS round_rating,
            CASE WHEN r.rating_status='rated' THEN NULLIF(btrim(r.data->>'rating_feedback'),'') END AS round_rating_feedback,
            r.data->>'resolved_at' AS resolved_at, r.data->>'closed_at' AS closed_at, r.last_message_at,
            r.data->>'responsibility_basis' AS responsibility_basis,
            r.data->>'responsible_assigned_at' AS responsible_assigned_at,
            r.data->>'handoff_requested_at' AS handoff_requested_at,
            r.data->'takeover_inherited' AS takeover_inherited,
            r.data->>'responsibility_trigger_message_id' AS responsibility_trigger_message_id,
            f.assigned_agent_id, f.assigned_agent_name, f.responsible_agent_id, f.responsible_agent_name,
            f.assisted, f.responding_seats, f.is_lost, f.handoff_at, f.human_replied,
            f.first_response_seconds, f.average_response_seconds, f.response_samples,
            NOT m.is_deleted AND (COALESCE(m.content,'') ~ '[^[:space:]]'
                OR jsonb_array_length(CASE WHEN jsonb_typeof(m.data->'attachments')='array'
                    THEN m.data->'attachments' ELSE '[]'::jsonb END) > 0) AS valid_content,
            review.ai_status AS ai_review_status, review.human_status AS human_review_status,
            review.remark AS review_remark,
            CASE WHEN rating.rating_key IS NOT NULL THEN jsonb_build_object(
                'rating',rating.rating,'feedback',rating.feedback,'status',rating.status,
                'rating_at',rating.data->>'source_time'
            ) END AS visitor_rating
        FROM (SELECT m.* FROM ops.message_index m WHERE {where} OFFSET 0) m
        LEFT JOIN ops.customer_index c ON c.customer_id=m.customer_id
        LEFT JOIN ops.round_index r ON r.round_id=m.round_id
        LEFT JOIN ops.conversation_index ci ON ci.source_key=m.source_key
            AND ci.account_id=m.account_id AND ci.conversation_id=m.conversation_id
        LEFT JOIN customer_round_seq seq ON seq.round_id=m.round_id
        LEFT JOIN {facts_relation} ON f.round_id=m.round_id
        LEFT JOIN quality.round_review_current review ON review.round_id=m.round_id
        LEFT JOIN ops.rating_index rating ON rating.source_key=m.source_key
            AND rating.account_id=m.account_id AND rating.conversation_id=m.conversation_id
            AND rating.rating_key='message:' || m.message_id::text
            AND rating.invitation_message_id=m.message_id
            AND rating.round_id=m.round_id
        ORDER BY m.source_key, m.account_id, m.customer_id, m.conversation_id,
            r.started_at NULLS LAST, m.round_id NULLS LAST, m.sent_at NULLS LAST, m.message_id
    """


def _agent_names():
    """提取阶段一次性读取有界目录；目录不可用时回退事实名，不影响生成。"""
    async def read():
        from ics_agent.app.core.config import get_settings
        from quality.query.directory import ChatwootDirectory

        directory = ChatwootDirectory(QuerySettings().source_key, get_settings())
        try:
            options = await directory.enrich({})
            return agent_name_index(options.get("agents", []))
        finally:
            await directory.close()

    try:
        return asyncio.run(read())
    except Exception as error:
        logger.warning("离线留档坐席目录暂不可用：%s", type(error).__name__)
        return {}


def export_row(row, agent_names=None):
    result = _flatten(row)
    # 提取列优先于消息 data；轮次责任按页面规范化，统计事实的归属保持原值。
    result.update({key: row[key] for key in FIELDS if key in row})
    result = normalize_responsibility(result)
    result.update({key: row[key] for key in ("responsible_agent_id", "responsible_agent_name") if key in row})
    scope = str(result.get("source_key")), str(result.get("account_id"))
    for prefix in ("assigned_agent", "responsible_agent"):
        agent_id = result.get(prefix + "_id")
        if agent_id is not None and str(agent_id) != "":
            result[prefix + "_name"] = ((agent_names or {}).get((*scope, str(agent_id)))
                or archived_agent_name(result.get(prefix + "_name"), agent_id)
                or f"Chatwoot 坐席 ID {agent_id} · 名称未同步")
    exported = {key: str(result[key]) if key.endswith("_id") and result.get(key) is not None
                else iso(result.get(key)) for key in FIELDS}
    exported["is_rating"] = result.get("is_rating") is True
    exported["rating_review"] = safe_rating_review(exported.get("rating_review"))
    exported["attachments"] = [{key: item.get(key) for key in (
        "attachment_key", "source_attachment_id", "filename", "media_type", "source_url", "expected_byte_count",
        "kind", "blocked")}
        for item in (result.get("attachments") or [])]
    return normalize_ids(exported)


def messages(request, check, settings=None, metadata=None):
    repository = ExportRepository(QuerySettings())
    try:
        repository.open()
        with repository.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '120s'")
            # 留档查询不使用并行工作进程，排序/扫描最多占归档库一个核，给在线查询和入库留出余量。
            connection.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            try:
                snapshot_at = connection.execute("SELECT now() AS now").fetchone()["now"]
                if metadata is not None:
                    metadata["snapshot_at"] = iso(snapshot_at)
                    metadata["filter_contract_version"] = FILTER_CONTRACT_VERSION
                    metadata["fact_version"] = FACT_VERSION
                    metadata["casefold_map"] = casefold_mapping(connection)
                where, parameters, selection, round_parameters, filters = _scope(request, connection)
                if settings is not None:
                    # 在同一快照内先数一遍，超过硬上限直接拒绝，不做昂贵的排序和附件下载。
                    # 前端预估之外再在工作进程里兜底：绕过页面直接调接口、或排队期间数据增长都能拦住。
                    check()
                    reason = limit_message(connection.execute(count_statement(where, detailed=False), parameters).fetchone(),
                                           settings)
                    if reason:
                        raise BundleLimit(reason)
                observed = (filters.as_of if filters is not None else None) or snapshot_at
                round_filter = f"r.round_id IN ({selection})" if selection else None
                source = FactSource.load(connection, observed, round_filter=round_filter, round_params=round_parameters)
                relation, fact_parameters = source.facts(f"f.round_id IN ({selection})", round_parameters) if selection else source.facts()
                agent_names = _agent_names()
                with connection.cursor(name="offline_archive_messages") as cursor:
                    # 先选择消息再连接展示字段，避免优化器先连接全库消息与轮次再筛选。
                    cursor.execute(message_statement(where, relation), [*parameters, *parameters, *fact_parameters])
                    while True:
                        check()
                        batch = cursor.fetchmany(500)
                        if not batch:
                            break
                        for row in batch:
                            check()
                            validate_projection(row)
                            row["snapshot_at"] = snapshot_at
                            yield export_row(row, agent_names)
                yield from version_rows(connection, where, parameters, check)
            except pg_errors.QueryCanceled:
                # statement_timeout：给出可操作的提示，而不是笼统的“生成失败”。
                raise BundleLimit(QUERY_TIMEOUT_MESSAGE) from None
    finally:
        repository.close()
