"""Chatwoot 转人工请求判定。"""

from __future__ import annotations

import re
import unicodedata


_SEPARATOR_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_HANDOFF_TARGETS = (
    "转接人工客服",
    "转接人工服务",
    "转人工客服",
    "转人工服务",
    "接入人工客服",
    "接入人工服务",
    "人工客服",
    "人工服务",
    "转接人工",
    "接入人工",
    "联系人工",
    "找人工",
    "转人工",
    "人工",
)
_EXACT_REQUESTS = {
    "人工",
    "人工客服",
    "人工服务",
    "转人工",
    "转人工客服",
    "转人工服务",
    "转接人工",
    "转接人工客服",
    "转接人工服务",
    "接入人工",
    "接入人工客服",
    "接入人工服务",
    "找人工",
    "联系人工",
}
_ACTION_PREFIXES = (
    "我要",
    "我想要",
    "想要",
    "需要",
    "请",
    "请帮我",
    "帮我",
    "麻烦",
    "麻烦帮我",
    "给我",
    "帮忙",
    "劳烦",
    "转",
    "转接",
    "接入",
    "切换",
    "联系",
    "找",
    "呼叫",
    "安排",
    "能不能",
    "可不可以",
)
_NEGATION_PREFIXES = (
    "不要",
    "不用",
    "无需",
    "无须",
    "不需要",
    "不想",
    "不必",
    "别",
    "先不",
    "暂不",
    "暂时不",
    "没必要",
)
_QUESTION_PREFIXES = (
    "怎么",
    "如何",
    "为什么",
    "为啥",
    "哪里",
    "在哪",
    "是否",
    "是不是",
)
_INFORMATIONAL_SUFFIXES = (
    "时间",
    "入口",
    "方式",
    "流程",
    "规则",
    "原因",
    "在哪",
    "哪里",
    "是什么意思",
)
_POLITE_SUFFIXES = (
    "",
    "吧",
    "呗",
    "呢",
    "呀",
    "啊",
    "哈",
    "哦",
    "谢谢",
    "麻烦了",
    "处理",
    "处理下",
    "处理一下",
    "帮忙",
)


def normalize_handoff_text(value: str) -> str:
    """统一全角字符并移除空白和标点，便于识别用户的直接请求。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _SEPARATOR_RE.sub("", normalized)


def is_explicit_handoff_request(value: str | None) -> bool:
    """只识别明确的“人工/转人工”表达，避免咨询或否定语境误接管。"""
    if not value or not value.strip():
        return False

    normalized = normalize_handoff_text(value)
    if normalized in _EXACT_REQUESTS:
        return True

    for target in _HANDOFF_TARGETS:
        start = normalized.find(target)
        while start >= 0:
            prefix = normalized[:start]
            suffix = normalized[start + len(target) :]
            if _is_positive_handoff_context(prefix, suffix):
                return True
            start = normalized.find(target, start + 1)
    return False


def _is_positive_handoff_context(prefix: str, suffix: str) -> bool:
    context_prefix = prefix[-8:]
    context_suffix = suffix[:8]
    if any(negation in context_prefix for negation in _NEGATION_PREFIXES):
        return False
    if any(question in context_prefix for question in _QUESTION_PREFIXES):
        return False
    if any(context_suffix.startswith(info) for info in _INFORMATIONAL_SUFFIXES):
        return False

    has_action_prefix = any(
        context_prefix.endswith(action) for action in _ACTION_PREFIXES
    )
    if has_action_prefix:
        return not suffix.endswith(("吗", "么", "嘛"))

    if suffix in _POLITE_SUFFIXES:
        return True

    return False
