# 模块职责：校验 Chatwoot Webhook 签名并解析事件载荷。

import hashlib
import hmac
import json
from typing import Any

from .models import ChatwootWebhookEvent


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Verify Chatwoot's HMAC-SHA256 webhook signature when configured."""
    if not secret:
        return True
    if not signature:
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def parse_event(raw_body: bytes) -> ChatwootWebhookEvent:
    payload: dict[str, Any] = json.loads(raw_body)
    return ChatwootWebhookEvent(
        event=str(payload.get("event", "")),
        id=payload.get("id") or payload.get("message", {}).get("id"),
        account=payload.get("account"),
        conversation=payload.get("conversation"),
        message_type=payload.get("message_type"),
        content=payload.get("content"),
        sender=payload.get("sender"),
        raw=payload,
    )
