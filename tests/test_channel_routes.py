"""后台渠道选择与上传、预览、查询参数的集成验证。"""

import asyncio
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from ics_agent.app.admin import files, routes as admin
from ics_agent.app.core.auth import require_admin
from ics_agent.app.public import routes as public
from ics_agent.app.chatwoot import routes as webhook
from ics_agent.app.chatwoot.jobs import WebhookJob
from ics_agent.app.chatwoot.payload import parse_event
from ics_agent.app.core.config import Settings


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    app = FastAPI()
    app.include_router(admin.router)
    app.include_router(public.router)
    app.dependency_overrides[require_admin] = lambda: object()
    with TestClient(app) as client:
        yield client


def workbook_bytes(answer):
    workbook = Workbook()
    workbook.active.append(["question", "answer", "need_human", "enabled"])
    workbook.active.append(["同一个问题", answer, 0, 1])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def test_upload_and_preview_use_selected_channels_own_faq_and_documents(client):
    for key in ("xingliao", "yanliao", "xingyingtong", "wechat"):
        query = {"channel_key": key}
        response = client.post("/admin/api/files/faq/upload", params=query,
                               files={"file": ("任意工作簿名.xlsx", workbook_bytes(key), "application/octet-stream")})
        assert response.status_code == 200
        response = client.post("/admin/api/files/knowledge-base/upload", params=query,
                               files=[("files", ("same.md", key.encode(), "text/markdown"))])
        assert response.status_code == 201
    for key in ("xingliao", "yanliao", "xingyingtong", "wechat"):
        query = {"channel_key": key}
        preview = client.get("/admin/api/files/faq/content", params=query).json()
        assert preview["sheets"][0]["rows"][1][1] == key
        response = client.get("/admin/api/files/knowledge-base/content", params={**query, "path": "same.md"})
        assert response.json()["content"] == key
    assert client.get("/admin/api/files/knowledge-base/content", params={"path": "same.md"}).json()["content"] == "xingliao"
    assert client.post("/admin/api/files/knowledge-base/delete", params={"channel_key": "wechat"},
                       json={"relative_path": "same.md"}).status_code == 200
    assert client.get("/admin/api/files/knowledge-base/content", params={"channel_key": "yanliao", "path": "same.md"}).status_code == 200


def test_unknown_channel_and_cross_directory_access_are_rejected(client):
    assert client.get("/admin/api/files/faq", params={"channel_key": "missing"}).status_code == 400
    assert client.post("/rag/query", json={"query": "问题", "channel_key": "missing"}).status_code == 422
    response = client.get("/admin/api/files/knowledge-base/content",
                          params={"channel_key": "wechat", "path": "../prompt_wechat.md"})
    assert response.status_code == 400


def test_query_api_forwards_channel_and_preserves_default_signature(client, monkeypatch):
    query = AsyncMock(return_value={"answer": "回复"})
    monkeypatch.setattr(public, "_run_rag_query", query)
    assert client.post("/rag/query", json={"query": "问题", "channel_key": "wechat"}).status_code == 200
    query.assert_awaited_with("问题", thread_id=None, channel_key="wechat")
    assert client.post("/rag/query", json={"query": "问题"}).status_code == 200
    query.assert_awaited_with("问题", thread_id=None)


def test_webhook_answer_forwards_channel_before_faq_lookup(monkeypatch):
    query = AsyncMock(return_value={"answer": "回复"})
    monkeypatch.setattr(webhook, "_run_rag_query", query)
    asyncio.run(webhook._build_webhook_answer(content="问题", thread_id="thread", event_key="event", channel_key="yanliao"))
    query.assert_awaited_once_with("问题", thread_id="thread", channel_key="yanliao")


@pytest.mark.parametrize("inbox_id,channel_key", [
    (1, "xingliao"), (2, "yanliao"), (3, "xingyingtong"), (4, "wechat"),
])
@pytest.mark.parametrize("inbox_location", ["conversation", "top_level", "inbox"])
def test_webhook_job_selects_channel_from_inbox_before_query(
    monkeypatch, caplog, inbox_id, channel_key, inbox_location,
):
    payload = {
        "event": "message_created", "id": 1201,
        "account": {"id": 1},
        "conversation": {"id": 56, "account_id": 1, "status": "pending"},
        "message_type": "incoming", "sender": {"id": 42, "type": "contact"},
        "content": "你是谁？",
    }
    if inbox_location == "conversation":
        payload["conversation"]["inbox_id"] = inbox_id
    elif inbox_location == "top_level":
        payload["inbox_id"] = inbox_id
    else:
        payload["inbox"] = {"id": inbox_id}

    control = SimpleNamespace(
        active_thread_id="chatwoot:1:56:session:channel-test", ai_enabled=True,
        handoff_required=False, session_closed_at=None, session_started_at=None,
    )
    repository = Mock()
    repository.record_user_message.return_value = control
    repository.get.return_value = control
    monkeypatch.setattr(webhook, "conversation_control_repository", repository)
    monkeypatch.setattr(webhook, "_refresh_handoff_from_chatwoot_status", AsyncMock(return_value=control))
    monkeypatch.setattr(webhook, "settings", Settings(_env_file=None, chatwoot_auto_reply_enabled=False))
    send_reply = AsyncMock(return_value={"sent": False})
    monkeypatch.setattr(webhook, "_send_chatwoot_auto_reply", send_reply)
    query = AsyncMock(return_value={
        "route": "rag", "faq_route": "rag", "answer": f"{channel_key} 的回复",
        "intent_label": None, "intent_confidence": None,
    })
    monkeypatch.setattr(webhook, "_run_rag_query", query)

    with caplog.at_level("INFO", logger=webhook.__name__):
        result = asyncio.run(webhook._process_webhook_job_unlocked(
            WebhookJob("channel-routing-test", parse_event(json.dumps(payload).encode())),
        ))

    options = {} if channel_key == "xingliao" else {"channel_key": channel_key}
    query.assert_awaited_once_with("你是谁？", thread_id=control.active_thread_id, **options)
    assert result["answer"] == f"{channel_key} 的回复"
    assert f"inbox_id={inbox_id}" in caplog.text
    assert f"channel_key={channel_key}" in caplog.text
    send_reply.assert_awaited_once()


def test_h5_identity_signatures_use_the_selected_inbox_secret(client, monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setattr(public, "settings", Settings(
        _env_file=None, chatwoot_profile_allow_query_identifiers=True,
        chatwoot_hmac_token="default-secret",
        chatwoot_channel_hmac_tokens={"yanliao": "yanliao-secret"},
    ))
    for key, secret in (("xingliao", "default-secret"), ("yanliao", "yanliao-secret")):
        response = client.get("/chatwoot/profile", params={"identifier": "same-user", "channel_key": key})
        assert response.status_code == 200
        assert response.json()["identifier_hash"] == hmac.new(
            secret.encode(), b"same-user", hashlib.sha256).hexdigest()
        assert secret not in response.text
    assert client.get("/chatwoot/profile", params={"identifier": "same-user", "channel_key": "wechat"}).status_code == 503
