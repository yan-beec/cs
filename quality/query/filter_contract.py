"""归档与单文件共用的版本化筛选参数契约。"""
from dataclasses import fields

from .repository import ArchiveFilters

FILTER_CONTRACT_VERSION = 1
FORMAT_VERSION = 3
OPTIONS = {
    "agent_scope": ["responsible", "replied", "assigned", "current"],
    "time_field": ["started_at", "message_at", "resolved_at", "rating_at", "handoff_at", "reply_at", "assigned_at"],
    "reception_type": ["ai_only", "human"],
    "ai_review_status": ["passed", "failed", "pending"],
    "human_review_status": ["passed", "failed", "pending"],
    "blacklist": ["ever", "active"], "rating_status": ["rated"],
    "rating": [1, 2, 3, 4, 5], "rating_op": ["eq", "gt", "lt"],
    "lost": [True], "outcome": ["ai_only", "handoff", "refused", "effective_human", "lost"],
    "dialogue": ["dialogue", "high_quality"], "message_scope": ["all", "contact", "agent", "bot", "private"],
}
FIXED_PARAMETERS = ("metric", "metric_seat", "source_key", "account_id", "channel_key", "as_of",
                    "has_attachment", "status")
API_OPTIONS = {**OPTIONS, "reception_type": ["ai_only", "human", "unknown"],
               "rating_status": ["not_invited", "pending", "rated", "unlinked"],
               "status": ["active", "resolved", "idle_boundary", "unknown"]}


def normalize_ids(value, key=""):
    """在 JSON 边界复制并规范身份字段，避免浏览器损失 bigint 精度；计数和分数保持数字。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return {name: normalize_ids(item, name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        item_key = key[:-1] if key.endswith("_ids") else ""
        return [normalize_ids(item, item_key) for item in value]
    if key == "id" or key.endswith("_id") or key in ("metric_seat", "revision"):
        return str(value)
    return value


def filter_contract(context=None, *, casefold_map=None):
    """返回新对象；空字符串、False 也是有效的固定约束。"""
    defaults = ArchiveFilters()
    fixed = (*FIXED_PARAMETERS, "start", "end") if (context or {}).get("metric") else FIXED_PARAMETERS
    parameters = {item.name: {"default": getattr(defaults, item.name),
                             "type": str(item.type), "fixed": item.name in fixed}
                  for item in fields(ArchiveFilters)}
    for name, options in OPTIONS.items():
        parameters[name]["options"] = list(options)
    for name, options in API_OPTIONS.items():
        parameters[name]["api_options"] = list(options)
    return {"version": FILTER_CONTRACT_VERSION, "parameters": parameters,
            "casefold_map": casefold_map or {}, "casefold_algorithm": "postgres_scalar_lower_v1",
            "fixed_context": normalize_ids({key: value for key, value in (context or {}).items()
                                            if key in fixed}),
            "normalization": {"trim": ["customer_query", "keyword"], "id_encoding": "decimal_string",
                              "rating_op_without_rating": "eq", "lost_false": None,
                              "scoped_ids": ["agent_id", "inbox_id"],
                              "scope_requires": ["source_key", "account_id"],
                              "customer_id_match": "exact", "message_at": "same_message",
                              "reply_at": "independent_exists", "attachment_false": "negative_exists"},
            "semantics": {"timezone": "Asia/Shanghai", "end": "exclusive", "keyword": "literal_observed_content",
                          "reset": "exported_collection", "dynamic_values": "export_snapshot"}}
