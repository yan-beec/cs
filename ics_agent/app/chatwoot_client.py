"""Chatwoot API 客户端。"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ics_agent.app.config import AppSettings, get_settings


class ChatwootAPIError(RuntimeError):
    """Chatwoot API 调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChatwootClient:
    """通过 Chatwoot Application API 发送会话消息。"""

    def __init__(self, settings: AppSettings | None = None) -> None:
        self._settings = settings or get_settings()

    def send_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        content: str,
    ) -> dict[str, Any]:
        """向指定会话发送一条公开的 outgoing 文本消息。"""
        token = self._settings.chatwoot_api_token.strip()
        if not token:
            raise ChatwootAPIError(
                "CHATWOOT_API_TOKEN is not configured."
            )
        if not account_id.strip():
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if not conversation_id.strip():
            raise ChatwootAPIError("Chatwoot conversation_id is missing.")
        if not content.strip():
            raise ChatwootAPIError("Chatwoot message content is empty.")

        base_url = self._settings.chatwoot_base_url.rstrip("/")
        url = (
            f"{base_url}/api/v1/accounts/{quote(account_id, safe='')}"
            f"/conversations/{quote(conversation_id, safe='')}/messages"
        )
        payload = json.dumps(
            {
                "content": content,
                "message_type": "outgoing",
                "private": False,
                "content_type": "text",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            url,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "api_access_token": token,
            },
            method="POST",
        )

        try:
            with urlopen(
                request,
                timeout=max(1.0, self._settings.chatwoot_api_timeout),
            ) as response:
                raw_response = response.read()
                status_code = int(response.status)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ChatwootAPIError(
                f"Chatwoot API returned HTTP {exc.code}: {body}",
                status_code=exc.code,
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ChatwootAPIError(
                f"Chatwoot API request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not 200 <= status_code < 300:
            body = raw_response.decode("utf-8", errors="replace")[:1000]
            raise ChatwootAPIError(
                f"Chatwoot API returned HTTP {status_code}: {body}",
                status_code=status_code,
            )

        if not raw_response:
            return {}
        try:
            result = json.loads(raw_response.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw_response": raw_response.decode("utf-8", errors="replace")}
        return result if isinstance(result, dict) else {"data": result}


_CLIENT = ChatwootClient()


def get_chatwoot_client() -> ChatwootClient:
    """返回进程级 Chatwoot API 客户端。"""
    return _CLIENT
