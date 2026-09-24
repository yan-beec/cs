# 模块职责：校验 Chatwoot Webhook 签名并解析事件载荷。

import hashlib
import hmac
import json
import time
import unicodedata
from typing import Any

from .models import ChatwootWebhookEvent


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
