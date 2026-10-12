"""服务端和桌面端共用的轻量规则，不依赖 ICS 或界面库。"""

from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit


def handoff_key(message_id: str | None, handoff_at: str | None) -> str:
    if message_id:
        return "message:" + str(message_id)
    if handoff_at:
        return "at:" + datetime.fromisoformat(handoff_at).astimezone(timezone.utc).isoformat()
    raise ValueError("人工轮次缺少触发消息和接管时间。")


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请输入有效的 HTTP/HTTPS 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("地址不能包含凭据、查询参数或片段。")
    return urlunsplit(parsed).rstrip("/")


def conversation_url(base: str, account_id: str, conversation_id: str) -> str:
    if not base:
        return ""
    return (
        validate_base_url(base)
        + "/app/accounts/" + quote(account_id, safe="")
        + "/conversations/" + quote(conversation_id, safe="")
    )
