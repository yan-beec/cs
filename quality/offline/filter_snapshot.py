"""在导出已有只读事务内投影筛选事实及分块的观察版本正文。"""
from quality.query.repository import HUMAN_REPLY, TIME_EXPRESSIONS, blacklist_contact_condition
from quality.query.filter_contract import normalize_ids

CUSTOMER_FIELDS = ("source_key", "account_id", "business_user_id", "blacklist_ever", "blacklist_active")
ROUND_FIELDS = ("source_key", "account_id", "inbox_id", "channel_key", "rating_at", "filter_handoff_at",
                "assigned_at", "replied_agent_ids", "assigned_agent_ids", "current_assignee_id",
                "visitor_messages", "is_ai_only", "is_handoff", "busy_refused")
MESSAGE_FIELDS = ("is_human_reply", "has_original_attachment", "inbox_id", "channel_key", "current_assignee_id")
SNAPSHOT_FIELDS = tuple(dict.fromkeys((*CUSTOMER_FIELDS, *ROUND_FIELDS, *MESSAGE_FIELDS)))
VERSION_FIELDS = ("source_key", "account_id", "message_id", "version", "offset", "content", "last")
VERSION_CHARS = 65536


def casefold_mapping(connection):
    """读取本数据库区域规则的完整标量小写变化表，不借用浏览器 Unicode 版本。"""
    rows = connection.execute("""SELECT code,lower(chr(code)) AS folded FROM generate_series(1,1114111) code
        WHERE CASE WHEN code BETWEEN 55296 AND 57343 THEN false ELSE lower(chr(code))<>chr(code) END""")
    return {str(row["code"]): row["folded"] for row in rows}


def validate_projection(row):
    """缺列是导出契约错误，不能伪装成字段均为空的有效快照。"""
    missing = [key for key in SNAPSHOT_FIELDS if key not in row]
    if missing:
        raise ValueError("离线筛选快照缺少字段：" + ", ".join(missing))


def snapshot_columns():
    """SQL 表达式只引用固定别名；不导出原始 JSON。"""
    blacklist = [f"{blacklist_contact_condition(mode, 'c.source_key', 'c.account_id', 'c.contact_id')} AS blacklist_{mode}"
                 for mode in ("ever", "active")]
    return ",\n".join([
        "c.business_user_id", *blacklist,
        "CASE WHEN r.round_id IS NOT NULL THEN r.inbox_id::text ELSE COALESCE(m.data->>'inbox_id',ci.inbox_id::text) END AS inbox_id",
        "CASE WHEN r.round_id IS NOT NULL THEN r.channel_key ELSE COALESCE(m.data->>'channel_key',ci.channel_key) END AS channel_key",
        f"{TIME_EXPRESSIONS['rating_at']} AS rating_at",
        f"{TIME_EXPRESSIONS['handoff_at']} AS filter_handoff_at",
        "CASE WHEN r.data->>'responsibility_basis'='ics_assignment' AND NULLIF(r.data->>'responsible_agent_id','') IS NOT NULL "
        f"THEN {TIME_EXPRESSIONS['assigned_at']} END AS assigned_at",
        "ARRAY(SELECT a.agent_id FROM ops.round_agent_index a WHERE a.round_id=r.round_id AND a.replied ORDER BY a.agent_id) AS replied_agent_ids",
        "ARRAY(SELECT a.agent_id FROM ops.round_agent_index a WHERE a.round_id=r.round_id AND a.was_assigned ORDER BY a.agent_id) AS assigned_agent_ids",
        "ci.current_assignee_id", "f.visitor_messages", "f.is_ai_only", "f.is_handoff", "f.busy_refused",
        f"COALESCE(({HUMAN_REPLY}),false) AS is_human_reply",
        "jsonb_array_length(COALESCE(m.data->'attachments','[]'::jsonb))>0 AS has_original_attachment",
    ])


def version_statement(where):
    """先按逻辑消息正文去重，再由数据库分块，避免单消息多版本无界驻留内存。"""
    return f"""WITH versions AS (
        SELECT s.source_key,s.account_id,s.message_id,s.content,min(s.id) AS version
        FROM ops.message_index m JOIN archive.message_snapshots s
          ON s.source_key=m.source_key AND s.account_id=m.account_id AND s.message_id=m.message_id
        WHERE {where} AND s.content IS NOT NULL
        GROUP BY s.source_key,s.account_id,s.message_id,s.content
    ) SELECT v.source_key,v.account_id,v.message_id,v.version,part-1 AS offset,
        substring(v.content FROM part FOR {VERSION_CHARS}) AS content,
        part+{VERSION_CHARS}>length(v.content) AS last
    FROM versions v CROSS JOIN LATERAL generate_series(1,GREATEST(length(v.content),1),{VERSION_CHARS}) part
    ORDER BY v.source_key,v.account_id,v.message_id,v.version,part"""


def version_rows(connection, where, parameters, check):
    """同连接游标逐批读取，调用方负责预算与最终清理。"""
    with connection.cursor(name="offline_archive_versions") as cursor:
        cursor.execute(version_statement(where), parameters)
        while True:
            check()
            batch = cursor.fetchmany(32)
            if not batch:
                break
            for row in batch:
                check()
                yield {"_type": "search_version", **{key: str(row[key]) if key.endswith("_id") or key == "version" else row[key]
                       for key in VERSION_FIELDS}}


def safe_rating_review(value):
    """评价复核仅保留展示必需字段，嵌套对象也使用白名单。"""
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in ("round_id", "status", "reviewer", "reviewed_at", "revision", "content_revision") if key in value}
    selected = value.get("selected_rating")
    if isinstance(selected, dict):
        result["selected_rating"] = {key: selected[key] for key in
            ("invitation_message_id", "external_round_id", "rating_key", "rating", "feedback", "status", "rating_at") if key in selected}
    return normalize_ids(result)
