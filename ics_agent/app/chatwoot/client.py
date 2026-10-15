"""Chatwoot API 客户端。"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ics_agent.app.core.config import AppSettings, get_settings
from .message_format import normalize_chatwoot_message


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
            content=normalize_chatwoot_message(content),
            private=False,
        )

    def send_csat_invitation(
        self,
        *,
        account_id: str,
        conversation_id: str,
        round_id: str,
        assigned_agent_id: str | None,
        closed_at: str,
        content: str,
        display_type: str,
    ) -> dict[str, Any]:
        """创建本轮的原生 CSAT 卡；独立于普通文本回复。"""
        payload: dict[str, Any] = {
            "content": content,
            "message_type": "outgoing",
            "private": False,
            "content_type": "input_csat",
            "content_attributes": {
                "display_type": display_type,
                "ics_round_id": round_id,
                "ics_assigned_agent_id": assigned_agent_id,
                "ics_closed_at": closed_at,
            },
        }
        agent_bot_id = self._settings.chatwoot_agent_bot_id.strip()
        if agent_bot_id:
            payload.update(sender_type="AgentBot", sender_id=agent_bot_id)
        return self._request_json(
            "POST", self._conversation_path(account_id, conversation_id) + "/messages",
            payload, timeout_seconds=self._settings.chatwoot_csat_api_timeout,
        )

    def find_csat_invitation(
        self,
        *,
        account_id: str,
        conversation_id: str,
        round_id: str,
        after_message_id: str | None,
    ) -> dict[str, Any] | None:
        """重试前分页核对远端，恢复已创建但本地尚未确认的卡片。"""
        after = int(after_message_id) if after_message_id else 0
        path = self._conversation_path(account_id, conversation_id) + "/messages"
        token = self._user_api_token("Chatwoot CSAT message reconciliation")
        deadline = time.monotonic() + self._settings.chatwoot_csat_api_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChatwootAPIError("Chatwoot CSAT message lookup timed out.")
            result = self._request_json(
                "GET", path + f"?after={after}", access_token=token, timeout_seconds=remaining,
            )
            items = result.get("payload")
            if not isinstance(items, list):
                raise ChatwootAPIError("Chatwoot CSAT message listing is invalid.")
            if not items:
                return None
            for item in items:
                attributes = item.get("content_attributes") or {}
                if (item.get("content_type") == "input_csat"
                        and attributes.get("ics_round_id") == round_id):
                    return item
            next_after = max(int(item["id"]) for item in items)
            if next_after <= after:
                raise ChatwootAPIError("Chatwoot CSAT message pagination did not advance.")
            after = next_after

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
            access_token=self._user_api_token(
                "Chatwoot typing status operations"
            ),
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
            access_token=self._user_api_token(
                "Chatwoot private note operations"
            ),
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

    def get_inbox(
        self,
        *,
        account_id: str,
        inbox_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """只读收件箱信息，供提醒显示渠道名称。"""
        account_id = str(account_id or "").strip()
        inbox_id = str(inbox_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if not inbox_id:
            raise ChatwootAPIError("Chatwoot inbox_id is missing.")
        return self._request_json(
            "GET",
            f"/api/v1/accounts/{quote(account_id, safe='')}/inboxes/{quote(inbox_id, safe='')}",
            access_token=self._user_api_token("Chatwoot inbox information"),
            timeout_seconds=timeout_seconds,
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

    def list_contact_conversations(
        self,
        *,
        account_id: str,
        contact_id: str,
    ) -> list[dict[str, Any]]:
        """读取联系人的会话，供静音时自动结束当前人工接待。"""
        account_id = str(account_id or "").strip()
        contact_id = str(contact_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if not contact_id:
            raise ChatwootAPIError("Chatwoot contact_id is missing.")
        result = self._request_json(
            "GET",
            (
                f"/api/v1/accounts/{quote(account_id, safe='')}"
                f"/contacts/{quote(contact_id, safe='')}/conversations"
            ),
            access_token=self._user_api_token(
                "Chatwoot contact conversation operations"
            ),
        )
        if not isinstance(result, dict):
            raise ChatwootAPIError("Chatwoot contact conversation list is invalid.")
        data = result.get("data")
        envelope = data if isinstance(data, dict) else result
        payload = data if isinstance(data, list) else envelope.get("payload")
        if payload is None:
            payload = result.get("payload")
        if not isinstance(payload, list):
            raise ChatwootAPIError("Chatwoot contact conversation list is missing.")
        if any(not isinstance(conversation, dict) for conversation in payload):
            raise ChatwootAPIError("Chatwoot contact conversation entry is invalid.")
        return payload

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

    def count_open_conversations(
        self,
        *,
        account_id: str,
        assignee_id: int,
        inbox_id: int | None = None,
        team_id: int | None = None,
    ) -> int:
        """统计指定坐席在当前 handoff scope 内负责的 open 会话数。

        Chatwoot 的会话列表响应在不同版本中可能是顶层 ``payload``、
        ``data.payload`` 或直接 ``data`` 列表；这里统一兼容三种形式。
        ``inbox_id`` 和 ``team_id`` 会传给 Chatwoot，避免跨 Inbox/Team
        把同一坐席的其他会话计入当前 handoff scope。
        """
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        if assignee_id <= 0:
            raise ChatwootAPIError("Chatwoot assignee_id is invalid.")
        if inbox_id is not None and inbox_id <= 0:
            raise ChatwootAPIError("Chatwoot inbox_id is invalid.")
        if team_id is not None and team_id <= 0:
            raise ChatwootAPIError("Chatwoot team_id is invalid.")

        # Chatwoot 的会话列表接口支持按 assigned/open/inbox/team 筛选，
        # 但没有稳定的 assignee_id 查询参数，因此仍按返回的
        # meta.assignee.id 计数。
        page = 1
        total = 0
        while page <= 100:
            query_params: dict[str, object] = {
                "assignee_type": "assigned",
                "status": "open",
                "page": page,
            }
            if inbox_id is not None:
                query_params["inbox_id"] = inbox_id
            if team_id is not None:
                query_params["team_id"] = team_id
            query = urlencode(query_params)
            result = self._request_json(
                "GET",
                (
                    f"/api/v1/accounts/{quote(account_id, safe='')}"
                    f"/conversations?{query}"
                ),
                access_token=self._user_api_token(
                    "Chatwoot conversation load operations"
                ),
            )
            payload = result.get("payload")
            data = result.get("data")
            if isinstance(data, dict):
                payload = data.get("payload", payload)
            elif isinstance(data, list):
                payload = data
            if not isinstance(payload, list):
                return total

            batch_count = 0
            for conversation in payload:
                if not isinstance(conversation, dict):
                    continue
                if inbox_id is not None:
                    conversation_inbox_id = conversation.get("inbox_id")
                    inbox = conversation.get("inbox")
                    if conversation_inbox_id is None and isinstance(inbox, dict):
                        conversation_inbox_id = inbox.get("id")
                    if (
                        conversation_inbox_id is not None
                        and str(conversation_inbox_id) != str(inbox_id)
                    ):
                        continue
                if team_id is not None:
                    conversation_team_id = conversation.get("team_id")
                    meta = conversation.get("meta")
                    team = conversation.get("team")
                    if conversation_team_id is None and isinstance(team, dict):
                        conversation_team_id = team.get("id")
                    if conversation_team_id is None and isinstance(meta, dict):
                        meta_team = meta.get("team")
                        if isinstance(meta_team, dict):
                            conversation_team_id = meta_team.get("id")
                    if (
                        conversation_team_id is not None
                        and str(conversation_team_id) != str(team_id)
                    ):
                        continue
                meta = conversation.get("meta")
                assignee = meta.get("assignee") if isinstance(meta, dict) else None
                current_id = assignee.get("id") if isinstance(assignee, dict) else None
                if str(current_id or "").strip() == str(assignee_id):
                    batch_count += 1
            total += batch_count
            if len(payload) == 0:
                break
            page += 1
        return total

    def list_open_conversation_assignments(
        self,
        *,
        account_id: str,
    ) -> dict[str, set[str]]:
        """一次扫描账号全部 open 会话，返回各坐席的会话 display ID 集合。

        人工容量按账号计算，不能按 Inbox/Team 分别放宽。只有完整读取到
        终止页才能使用结果；未知响应、分页截断和超时均阻止本次分配。
        """
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ChatwootAPIError("Chatwoot account_id is missing.")
        token = self._user_api_token("Chatwoot conversation load operations")
        deadline = time.monotonic() + 45.0
        assignments: dict[str, set[str]] = {}
        seen: dict[str, str | None] = {}
        expected_count = 0

        for page in range(1, 101):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChatwootAPIError("Chatwoot conversation load scan timed out.")
            query = urlencode(
                {"assignee_type": "all", "status": "open", "page": page}
            )
            try:
                result = self._request_json(
                    "GET",
                    f"/api/v1/accounts/{quote(account_id, safe='')}/conversations?{query}",
                    access_token=token,
                    timeout_seconds=min(self._settings.chatwoot_api_timeout, remaining),
                )
            except StopIteration as exc:
                # A test double or an exhausted paginated response must never
                # turn a valid prefix into a silently partial load.
                raise ChatwootAPIError(
                    "Chatwoot conversation list ended before a terminal page."
                ) from exc
            if time.monotonic() >= deadline:
                raise ChatwootAPIError("Chatwoot conversation load scan timed out.")
            if not isinstance(result, dict):
                raise ChatwootAPIError("Chatwoot conversation list is invalid.")

            data = result.get("data")
            envelope = data if isinstance(data, dict) else result
            payload = data if isinstance(data, list) else envelope.get("payload")
            if payload is None:
                payload = result.get("payload")
            if not isinstance(payload, list):
                raise ChatwootAPIError("Chatwoot conversation list is missing.")
            # all_count 对应 assignee_type=all 的结果，包括未分配会话。
            # 兼容不提供计数的版本，但有计数时不能把提前空页当成完成。
            for metadata in (result.get("meta"), envelope.get("meta")):
                if not isinstance(metadata, dict):
                    continue
                for key in ("all_count", "total_count"):
                    if key not in metadata:
                        continue
                    value = metadata[key]
                    if isinstance(value, bool) or not str(value).isdigit():
                        raise ChatwootAPIError(
                            "Chatwoot conversation total count is invalid."
                        )
                    expected_count = max(expected_count, int(value))

            if not payload:
                if len(seen) < expected_count:
                    raise ChatwootAPIError(
                        "Chatwoot conversation list ended before the advertised total."
                    )
                return assignments

            previous_size = len(seen)
            for conversation in payload:
                if not isinstance(conversation, dict):
                    raise ChatwootAPIError("Chatwoot conversation entry is invalid.")
                conversation_id = self._load_identifier(
                    conversation.get("display_id") or conversation.get("id"),
                    "conversation display",
                )
                meta = conversation.get("meta")
                if isinstance(meta, dict) and "assignee" in meta:
                    assignee = meta["assignee"]
                    if assignee is None:
                        assignee_id = None
                    elif isinstance(assignee, dict):
                        assignee_id = self._load_identifier(assignee.get("id"), "assignee")
                    else:
                        raise ChatwootAPIError("Chatwoot conversation assignee is invalid.")
                elif "assignee" in conversation:
                    assignee = conversation["assignee"]
                    if assignee is None:
                        assignee_id = None
                    elif isinstance(assignee, dict):
                        assignee_id = self._load_identifier(assignee.get("id"), "assignee")
                    else:
                        raise ChatwootAPIError("Chatwoot conversation assignee is invalid.")
                elif "assignee_id" in conversation:
                    value = conversation["assignee_id"]
                    assignee_id = (
                        None if value is None else self._load_identifier(value, "assignee")
                    )
                else:
                    raise ChatwootAPIError("Chatwoot conversation assignee is missing.")
                if conversation_id in seen and seen[conversation_id] != assignee_id:
                    raise ChatwootAPIError(
                        "Chatwoot conversation assignment changed during pagination."
                    )
                seen[conversation_id] = assignee_id
                if assignee_id is not None:
                    assignments.setdefault(assignee_id, set()).add(conversation_id)
            if len(seen) == previous_size:
                raise ChatwootAPIError("Chatwoot conversation pagination did not advance.")

        raise ChatwootAPIError("Chatwoot conversation list exceeded 100 pages.")

    @staticmethod
    def _load_identifier(value: Any, label: str) -> str:
        """容量查询中的标识不可缺失，否则无法可靠去重或计算坐席负载。"""
        text = str(value).strip()
        if isinstance(value, bool) or not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise ChatwootAPIError(f"Chatwoot {label} id is missing or invalid.")
        return str(int(text))

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
            access_token=self._user_api_token("Chatwoot conversation label operations"),
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
            if (
                assignee_id is not None
                or team_id is not None
            ):
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
            access_token=self._user_api_token(
                "Chatwoot conversation assignment operations"
            ),
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
            access_token=self._user_api_token(
                "Chatwoot conversation status operations"
            ),
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
            access_token=self._user_api_token(
                "Chatwoot conversation assignment operations"
            ),
        )

    def _create_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        content: str,
        private: bool,
        access_token: str | None = None,
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
            access_token=access_token,
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
        timeout_seconds: float | None = None,
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
                timeout=(
                    max(1.0, self._settings.chatwoot_api_timeout)
                    if timeout_seconds is None
                    else max(0.001, timeout_seconds)
                ),
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
