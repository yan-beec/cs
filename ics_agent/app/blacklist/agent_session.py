"""用坐席在客户端里的 Chatwoot 登录会话核对操作人身份。

客户端转发当前登录会话的 devise 认证头（access-token / client / uid），ICS 用它
向 Chatwoot 读取 /api/v1/profile 和会话详情：操作人身份、账号归属和会话访问权限
都以 Chatwoot 为准，ICS 不保存这些认证头，也不写入日志。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

PROFILE_CACHE_SECONDS = 60
PROFILE_CACHE_LIMIT = 500
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AgentSessionInvalid(PermissionError):
    """登录会话无效或已过期。"""


class AgentForbidden(PermissionError):
    """坐席无权访问该账号或会话。"""


class ChatwootUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentSession:
    access_token: str
    client: str
    uid: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256("\0".join((self.access_token, self.client, self.uid)).encode()).hexdigest()

    def headers(self) -> dict[str, str]:
        return {"access-token": self.access_token, "client": self.client, "uid": self.uid,
                "token-type": "Bearer", "Accept": "application/json"}


class ChatwootAgentGateway:
    def __init__(self, settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _get(self, session: AgentSession, path: str) -> dict[str, Any]:
        request = Request(self.settings.chatwoot_base_url.rstrip("/") + path, headers=session.headers(), method="GET")
        try:
            with urlopen(request, timeout=max(1.0, min(10.0, self.settings.chatwoot_api_timeout))) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if error.code == 401:
                raise AgentSessionInvalid("Chatwoot 登录已失效，请在客户端重新登录。") from None
            if error.code in (403, 404):
                raise AgentForbidden("当前坐席无权查看该会话。") from None
            raise ChatwootUnavailable(f"Chatwoot HTTP {error.code}") from None
        except (TimeoutError, URLError, OSError) as error:
            raise ChatwootUnavailable(type(error).__name__) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise ChatwootUnavailable("response too large")
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ChatwootUnavailable("invalid json") from None
        return result if isinstance(result, dict) else {}

    def profile(self, session: AgentSession) -> dict:
        key = session.fingerprint
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        profile = self._get(session, "/api/v1/profile")
        if not str(profile.get("id") or "").isdigit() or not isinstance(profile.get("accounts"), list):
            raise AgentSessionInvalid("无法核对坐席身份，请在客户端重新登录。")
        result = {
            "id": str(profile["id"]),
            "name": str(profile.get("available_name") or profile.get("name") or profile.get("email") or profile["id"])[:100],
            "accounts": {str(account.get("id")) for account in profile["accounts"] if isinstance(account, dict)},
        }
        with self._lock:
            if len(self._cache) >= PROFILE_CACHE_LIMIT:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                if len(self._cache) >= PROFILE_CACHE_LIMIT:
                    self._cache.clear()
            self._cache[key] = (now + PROFILE_CACHE_SECONDS, result)
        return result

    def conversation(self, session: AgentSession, account_id: str, conversation_id: str) -> dict:
        """以坐席自己的权限读取会话，确认其能看到该会话，并返回会话中的客户。"""
        return self._get(session, f"/api/v1/accounts/{quote(account_id, safe='')}/conversations/{quote(conversation_id, safe='')}")

    def messages(self, session: AgentSession, account_id: str, conversation_id: str) -> list[dict]:
        """读取会话最近一页消息（Chatwoot 默认返回最新 20 条）。"""
        result = self._get(session, f"/api/v1/accounts/{quote(account_id, safe='')}/conversations/"
                                    f"{quote(conversation_id, safe='')}/messages")
        payload = result.get("payload")
        return payload if isinstance(payload, list) else []


def conversation_contact(conversation: dict) -> dict:
    meta = conversation.get("meta") if isinstance(conversation.get("meta"), dict) else {}
    for candidate in (meta.get("sender"), meta.get("contact"), conversation.get("contact")):
        if isinstance(candidate, dict) and str(candidate.get("id") or "").isdigit():
            additional = candidate.get("additional_attributes") if isinstance(candidate.get("additional_attributes"), dict) else {}
            identifier = candidate.get("identifier") or additional.get("userId") or additional.get("user_id")
            return {"id": str(candidate["id"]), "name": str(candidate.get("name") or "")[:200] or None,
                    "identifier": str(identifier)[:200] if identifier not in (None, "") else None}
    contact_id = conversation.get("contact_id")
    if str(contact_id or "").isdigit():
        return {"id": str(contact_id), "name": None, "identifier": None}
    return {}
