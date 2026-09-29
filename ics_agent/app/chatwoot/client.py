"""Chatwoot API 客户端。"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ics_agent.app.core.config import AppSettings, get_settings


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
    """通过 Chatwoot Application API 操作会话和发送消息。"""

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
        return self._create_message(
            account_id=account_id,
            conversation_id=conversation_id,
            content=content,
            private=False,
        )

    def toggle_typing_status(
        self,
        *,
        account_id: str,
        conversation_id: str,
        typing_status: str,
    ) -> dict[str, Any]:
        """切换 Chatwoot 会话中的坐席输入状态。"""
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        clean_status = str(typing_status or "").strip().lower()
        if clean_status not in {"on", "off"}:
            raise ChatwootAPIError(
                "Chatwoot typing status must be either on or off."
            )
        return self._request_json(
            "POST",
            self._conversation_path(account_id, conversation_id)
            + "/toggle_typing_status",
            {"typing_status": clean_status},
        )

    def create_private_note(
        self,
        *,
        account_id: str,
        conversation_id: str,
        content: str,
    ) -> dict[str, Any]:
        """向会话写入一条坐席可见、用户不可见的私有备注。"""
        return self._create_message(
            account_id=account_id,
            conversation_id=conversation_id,
            content=content,
            private=True,
        )

    def get_conversation(
        self,
        *,
        account_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """读取 Chatwoot 会话当前状态。"""
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        return self._request_json(
            "GET",
            self._conversation_path(account_id, conversation_id),
        )

    def get_contact(
        self,
        *,
        account_id: str,
        contact_id: str,
    ) -> dict[str, Any]:
        """读取 Chatwoot 联系人当前状态。"""
        account_id = str(account_id or "").strip()
        contact_id = str(contact_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if not contact_id:
            raise ChatwootAPIError("Chatwoot contact_id is missing.")
        return self._request_json(
            "GET",
            (
                f"/api/v1/accounts/{quote(account_id, safe='')}"
                f"/contacts/{quote(contact_id, safe='')}"
            ),
            access_token=self._contact_api_token(),
        )

    def list_agents(
        self,
        *,
        account_id: str,
    ) -> list[dict[str, Any]]:
        """列出账号下的 Chatwoot 坐席及其可用状态。"""
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        result = self._request_json(
            "GET",
            f"/api/v1/accounts/{quote(account_id, safe='')}/agents",
            access_token=self._user_api_token("Chatwoot agent operations"),
        )
        return self._response_items(result)

    def list_team_members(
        self,
        *,
        account_id: str,
        team_id: int,
    ) -> list[dict[str, Any]]:
        """列出指定 Team 下的 Chatwoot 坐席及其可用状态。"""
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if team_id <= 0:
            raise ChatwootAPIError("Chatwoot team_id is invalid.")
        result = self._request_json(
            "GET",
            (
                f"/api/v1/accounts/{quote(account_id, safe='')}"
                f"/teams/{quote(str(team_id), safe='')}/team_members"
            ),
            access_token=self._user_api_token("Chatwoot team operations"),
        )
        return self._response_items(result)

    def list_inbox_members(
        self,
        *,
        account_id: str,
        inbox_id: int,
    ) -> list[dict[str, Any]]:
        """列出指定 Inbox 下的 Chatwoot 坐席及其可用状态。"""
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if inbox_id <= 0:
            raise ChatwootAPIError("Chatwoot inbox_id is invalid.")
        result = self._request_json(
            "GET",
            (
                f"/api/v1/accounts/{quote(account_id, safe='')}"
                f"/inbox_members/{quote(str(inbox_id), safe='')}"
            ),
            access_token=self._user_api_token("Chatwoot inbox operations"),
        )
        return self._response_items(result)

    def set_contact_blocked(
        self,
        *,
        account_id: str,
        contact_id: str,
        blocked: bool,
    ) -> dict[str, Any]:
        """设置 Chatwoot 联系人的 blocked 状态。"""
        account_id = str(account_id or "").strip()
        contact_id = str(contact_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if not contact_id:
            raise ChatwootAPIError("Chatwoot contact_id is missing.")
        return self._request_json(
            "PUT",
            (
                f"/api/v1/accounts/{quote(account_id, safe='')}"
                f"/contacts/{quote(contact_id, safe='')}"
            ),
            {"blocked": bool(blocked)},
            access_token=self._contact_api_token(),
        )

    def add_conversation_labels(
        self,
        *,
        account_id: str,
        conversation_id: str,
        labels: list[str],
    ) -> dict[str, Any]:
        """给会话添加标签；Chatwoot 会按标签集合处理重复项。"""
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        clean_labels = sorted(
            {
                label.strip()
                for label in labels
                if label and label.strip()
            }
        )
        if not clean_labels:
            raise ChatwootAPIError("Chatwoot conversation labels are empty.")
        return self._request_json(
            "POST",
            self._conversation_path(account_id, conversation_id) + "/labels",
            {"labels": clean_labels},
        )

    def update_conversation(
        self,
        *,
        account_id: str,
        conversation_id: str,
        status: str | None = None,
        assignee_id: int | None = None,
        team_id: int | None = None,
    ) -> dict[str, Any]:
        """更新会话状态或分配信息。"""
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        if status is not None:
            if assignee_id is not None or team_id is not None:
                raise ChatwootAPIError(
                    "Chatwoot status and assignment must be updated separately."
                )
            return self.set_conversation_status(
                account_id=account_id,
                conversation_id=conversation_id,
                status=status,
            )
        payload: dict[str, Any] = {}
        if assignee_id is not None:
            payload["assignee_id"] = assignee_id
        if team_id is not None:
            payload["team_id"] = team_id
        if not payload:
            raise ChatwootAPIError("Chatwoot conversation update is empty.")
        return self._request_json(
            "PATCH",
            self._conversation_path(account_id, conversation_id),
            payload,
        )

    def set_conversation_status(
        self,
        *,
        account_id: str,
        conversation_id: str,
        status: str,
    ) -> dict[str, Any]:
        """通过 Chatwoot 的状态切换端点设置会话状态。"""
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        clean_status = str(status or "").strip().lower()
        if not clean_status:
            raise ChatwootAPIError("Chatwoot conversation status is empty.")
        return self._request_json(
            "POST",
            self._conversation_path(account_id, conversation_id)
            + "/toggle_status",
            {"status": clean_status},
        )

    def assign_conversation(
        self,
        *,
        account_id: str,
        conversation_id: str,
        assignee_id: int | None = None,
        team_id: int | None = None,
    ) -> dict[str, Any]:
        """按配置将会话分配给 Team 和/或 Agent。"""
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        payload: dict[str, Any] = {}
        if assignee_id is not None:
            payload["assignee_id"] = assignee_id
        if team_id is not None:
            payload["team_id"] = team_id
        if not payload:
            raise ChatwootAPIError("Chatwoot assignment is empty.")
        return self._request_json(
            "POST",
            self._conversation_path(account_id, conversation_id)
            + "/assignments",
            payload,
        )

    def _create_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        content: str,
        private: bool,
    ) -> dict[str, Any]:
        account_id, conversation_id = self._validate_conversation(
            account_id,
            conversation_id,
        )
        if not content.strip():
            raise ChatwootAPIError("Chatwoot message content is empty.")
        payload: dict[str, Any] = {
            "content": content,
            "message_type": "outgoing",
            "private": private,
            "content_type": "text",
        }
        agent_bot_id = self._settings.chatwoot_agent_bot_id.strip()
        if agent_bot_id:
            payload["sender_type"] = "AgentBot"
            payload["sender_id"] = agent_bot_id
        return self._request_json(
            "POST",
            self._conversation_path(account_id, conversation_id) + "/messages",
            payload,
        )

    def _conversation_path(
        self,
        account_id: str,
        conversation_id: str,
    ) -> str:
        return (
            f"/api/v1/accounts/{quote(account_id, safe='')}"
            f"/conversations/{quote(conversation_id, safe='')}"
        )

    @staticmethod
    def _validate_conversation(
        account_id: str,
        conversation_id: str,
    ) -> tuple[str, str]:
        account_id = str(account_id or "").strip()
        conversation_id = str(conversation_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if not conversation_id:
            raise ChatwootAPIError("Chatwoot conversation_id is missing.")
        return account_id, conversation_id

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = (access_token or "").strip() or (
            self._settings.chatwoot_agent_bot_access_token.strip()
            or self._settings.chatwoot_api_token.strip()
        )
        if not token:
            raise ChatwootAPIError(
                "CHATWOOT_AGENT_BOT_ACCESS_TOKEN or "
                "CHATWOOT_API_TOKEN is not configured."
            )

        raw_payload = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "api_access_token": token,
        }
        if raw_payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self._settings.chatwoot_base_url.rstrip("/") + path,
            data=raw_payload,
            headers=headers,
            method=method,
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

    @staticmethod
    def _response_items(result: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = result.get("data")
        if candidates is None:
            candidates = result.get("payload")
        if candidates is None and "id" in result:
            candidates = [result]
        if not isinstance(candidates, list):
            return []
        return [
            item
            for item in candidates
            if isinstance(item, dict)
        ]

    def _user_api_token(self, operation: str) -> str:
        token = (
            self._settings.chatwoot_contact_api_token.strip()
            or self._settings.chatwoot_api_token.strip()
        )
        if not token:
            raise ChatwootAPIError(
                f"CHATWOOT_CONTACT_API_TOKEN or CHATWOOT_API_TOKEN "
                f"is required for {operation}."
            )
        return token

    def _contact_api_token(self) -> str:
        return self._user_api_token("Chatwoot contact operations")


_CLIENT = ChatwootClient()


def get_chatwoot_client() -> ChatwootClient:
    """返回进程级 Chatwoot API 客户端。"""
    return _CLIENT
