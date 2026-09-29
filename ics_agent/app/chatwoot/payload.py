"""Chatwoot Webhook payload 解析、签名校验、文本判定工具。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import unicodedata
from collections.abc import Mapping
from typing import Any

from ics_agent.app.core.models import ChatwootWebhookEvent


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
_IMAGE_ATTACHMENT_TYPES = {"image"}
_IMAGE_MIME_PREFIX = "image/"
_IMAGE_URL_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
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



_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x1FC00, 0x1FFFD),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x2199),
    (0x21A9, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x2604),
    (0x260E, 0x260E),
    (0x2611, 0x2611),
    (0x2614, 0x2615),
    (0x2618, 0x2618),
    (0x261D, 0x261D),
    (0x2620, 0x2620),
    (0x2622, 0x2623),
    (0x2626, 0x2626),
    (0x262A, 0x262A),
    (0x262E, 0x262F),
    (0x2638, 0x263A),
    (0x2640, 0x2642),
    (0x2648, 0x2653),
    (0x265F, 0x2660),
    (0x2663, 0x2663),
    (0x2665, 0x2666),
    (0x2668, 0x2668),
    (0x267B, 0x267B),
    (0x267E, 0x267F),
    (0x2692, 0x2697),
    (0x2699, 0x2699),
    (0x269B, 0x269C),
    (0x26A0, 0x26A1),
    (0x26A7, 0x26A7),
    (0x26AA, 0x26AB),
    (0x26B0, 0x26B1),
    (0x26BD, 0x26BE),
    (0x26C4, 0x26C5),
    (0x26C8, 0x26C8),
    (0x26CE, 0x26CF),
    (0x26D1, 0x26D1),
    (0x26D3, 0x26D4),
    (0x26E9, 0x26EA),
    (0x26F0, 0x26F5),
    (0x26F7, 0x26FA),
    (0x26FD, 0x26FD),
    (0x2702, 0x2702),
    (0x2705, 0x2705),
    (0x2708, 0x270D),
    (0x270F, 0x270F),
    (0x2712, 0x2712),
    (0x2714, 0x2714),
    (0x2716, 0x2716),
    (0x271D, 0x271D),
    (0x2721, 0x2721),
    (0x2728, 0x2728),
    (0x2733, 0x2734),
    (0x2744, 0x2744),
    (0x2747, 0x2747),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2763, 0x2764),
    (0x2795, 0x2797),
    (0x27A1, 0x27A1),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
)
_EMOJI_SINGLETONS = {0x00A9, 0x00AE}
_EMOJI_IGNORABLES = {
    0x200D,  # zero-width joiner
    0xFE0E,  # text presentation selector
    0xFE0F,  # emoji presentation selector
    0x20E3,  # combining enclosing keycap
}


def _is_emoji_codepoint(codepoint: int) -> bool:
    return codepoint in _EMOJI_SINGLETONS or any(
        start <= codepoint <= end for start, end in _EMOJI_RANGES
    )


def is_emoji_only_text(value: str) -> bool:
    """判断消息是否只包含 emoji、变体选择符和连接符。"""
    text = value.strip()
    if not text:
        return False

    has_emoji = False
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        if codepoint in _EMOJI_IGNORABLES:
            index += 1
            continue

        # Keycap emoji are encoded as an ASCII digit/#/* plus U+20E3.
        if text[index] in "0123456789#*":
            keycap_end = index + 1
            if (
                keycap_end < len(text)
                and ord(text[keycap_end]) == 0xFE0F
            ):
                keycap_end += 1
            if (
                keycap_end < len(text)
                and ord(text[keycap_end]) == 0x20E3
            ):
                has_emoji = True
                index = keycap_end + 1
                continue

        if _is_emoji_codepoint(codepoint):
            has_emoji = True
            index += 1
            continue

        if unicodedata.category(text[index]).startswith("M"):
            index += 1
            continue

        return False

    return has_emoji


def has_image_attachment(event: ChatwootWebhookEvent) -> bool:
    """判断 Chatwoot 消息是否包含图片附件。"""
    payload = event.raw if isinstance(event.raw, Mapping) else {}
    message = payload.get("message")
    message = message if isinstance(message, Mapping) else {}
    candidates = (
        payload.get("attachments"),
        message.get("attachments"),
    )
    for attachments in candidates:
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if _is_image_attachment(attachment):
                return True
    return False


def _is_image_attachment(attachment: Any) -> bool:
    if not isinstance(attachment, Mapping):
        return False

    file_type = str(attachment.get("file_type") or "").strip().lower()
    if file_type in _IMAGE_ATTACHMENT_TYPES:
        return True

    content_type = str(
        attachment.get("content_type")
        or attachment.get("mime_type")
        or attachment.get("mimetype")
        or ""
    ).strip().lower()
    if content_type.startswith(_IMAGE_MIME_PREFIX):
        return True

    for key in ("data_url", "thumb_url", "download_url", "url"):
        value = str(attachment.get(key) or "").strip().lower()
        if value.startswith(_IMAGE_MIME_PREFIX):
            return True
        path = value.split("?", 1)[0]
        if path.endswith(_IMAGE_URL_EXTENSIONS):
            return True

    return False


def verify_signature(
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """校验 Chatwoot 的带时间戳 HMAC-SHA256 Webhook 签名。

    Chatwoot signs the exact raw request body with ``"{timestamp}.{body}"``.
    """
    if not secret:
        return True
    if not signature or not timestamp:
        return False

    timestamp_text = timestamp.strip()
    if not timestamp_text.isdigit():
        return False

    try:
        timestamp_value = int(timestamp_text)
    except ValueError:
        return False

    current_time = time.time() if now is None else now
    if abs(current_time - timestamp_value) > tolerance_seconds:
        return False

    signed_payload = timestamp_text.encode("ascii") + b"." + raw_body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    provided = signature.strip()
    if provided.startswith("sha256="):
        provided = provided.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def parse_event(raw_body: bytes) -> ChatwootWebhookEvent:
    payload: dict[str, Any] = json.loads(raw_body)
    conversation = payload.get("conversation") or {}
    custom_attributes = conversation.get("custom_attributes") or {}
    if not isinstance(custom_attributes, dict):
        custom_attributes = {}
    thread_id = (
        payload.get("thread_id")
        or conversation.get("thread_id")
        or custom_attributes.get("thread_id")
    )
    return ChatwootWebhookEvent(
        event=str(payload.get("event", "")),
        id=payload.get("id") or payload.get("message", {}).get("id"),
        thread_id=None if thread_id is None else str(thread_id),
        account=payload.get("account"),
        conversation=conversation,
        message_type=payload.get("message_type"),
        content=payload.get("content"),
        sender=payload.get("sender"),
        raw=payload,
    )


def contact_blocked_from_payload(
    payload: Mapping[str, Any],
) -> bool | None:
    """从 Chatwoot 事件或 API 响应中提取联系人 blocked 状态。

    Chatwoot 的会话数据通常把联系人放在 ``meta.sender`` 中，
    联系人事件则可能直接把 ``blocked`` 放在顶层。
    ``None`` 表示当前载荷没有携带该字段。
    """
    conversation = payload.get("conversation")
    conversation = conversation if isinstance(conversation, Mapping) else {}
    meta = conversation.get("meta")
    meta = meta if isinstance(meta, Mapping) else {}
    payload_meta = payload.get("meta")
    payload_meta = (
        payload_meta if isinstance(payload_meta, Mapping) else {}
    )

    candidates: list[Any] = [
        payload.get("contact"),
        payload.get("sender"),
        conversation.get("contact"),
        conversation.get("sender"),
        meta.get("contact"),
        meta.get("sender"),
        payload_meta.get("contact"),
        payload_meta.get("sender"),
    ]
    observed = False
    direct_blocked = payload.get("blocked")
    if direct_blocked is not None:
        candidates.insert(0, {"blocked": direct_blocked})

    changed_attributes = payload.get("changed_attributes")
    if isinstance(changed_attributes, list):
        for changed_attribute in changed_attributes:
            if not isinstance(changed_attribute, Mapping):
                continue
            blocked_change = changed_attribute.get("blocked")
            if not isinstance(blocked_change, Mapping):
                continue
            current_value = blocked_change.get("current_value")
            if current_value is not None:
                candidates.insert(0, {"blocked": current_value})

    for candidate in candidates:
        if not isinstance(candidate, Mapping) or "blocked" not in candidate:
            continue
        observed = True
        value = candidate.get("blocked")
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, (int, float)):
            if bool(value):
                return True
            continue
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes"}:
                return True

    return False if observed else None


def contact_block_change_from_payload(
    payload: Mapping[str, Any],
) -> bool | None:
    """只读取明确的 blocked 变更；普通联系人快照不算解除动作。"""
    changed_attributes = payload.get("changed_attributes")
    if not isinstance(changed_attributes, list):
        return None

    for changed_attribute in changed_attributes:
        if not isinstance(changed_attribute, Mapping):
            continue
        blocked_change = changed_attribute.get("blocked")
        if not isinstance(blocked_change, Mapping):
            continue
        current_value = blocked_change.get("current_value")
        if isinstance(current_value, bool):
            return current_value
        if isinstance(current_value, (int, float)):
            return bool(current_value)
        if isinstance(current_value, str):
            return current_value.strip().lower() in {"1", "true", "yes"}
    return None
