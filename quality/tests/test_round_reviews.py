"""Human review authorization, validation and conflict handling."""
import asyncio
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import AdminPrincipal, require_admin
from quality.query import routes
from quality.query.repository import ReviewConflict
from quality.query.runtime import ArchiveUnavailable, QueryRuntime


@pytest.fixture
def review_client(monkeypatch):
    runtime = Mock(call=AsyncMock(return_value={"ai_status": "failed", "human_status": "passed", "revision": "1"}))
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: AdminPrincipal(username="review-admin")
    return TestClient(app), runtime, app


def test_read_and_save_require_existing_admin_auth(review_client):
    client, runtime, app = review_client
    app.dependency_overrides.clear()
    assert client.get("/admin/api/quality/rounds/r1/review").status_code == 401
    assert client.put("/admin/api/quality/rounds/r1/review", json={"ai_status": "failed", "human_status": "passed"}).status_code == 401
    runtime.call.assert_not_called()


@pytest.mark.parametrize("ai_status", ["passed", "failed", "pending"])
@pytest.mark.parametrize("human_status", ["passed", "failed", "pending"])
def test_saves_the_selected_round_and_authenticated_reviewer(review_client, ai_status, human_status):
    client, runtime, _ = review_client
    response = client.put("/admin/api/quality/rounds/r1/review",
                          json={"ai_status": ai_status, "human_status": human_status, "remark": "逐轮备注\n=保留原文", "revision": "7"})
    assert response.status_code == 200
    runtime.call.assert_awaited_once_with("save_round_review", "r1", ai_status, human_status, "逐轮备注\n=保留原文", "review-admin", 7)


@pytest.mark.parametrize("body", [
    {"ai_status": "bad", "human_status": "pending"},
    {"ai_status": "pending", "human_status": "bad"},
    {"ai_status": "pending"}, {"human_status": "pending"}, {"status": "failed"}, {"ai_status": "failed", "human_status": "passed", "remark": "x" * 5001},
    {"ai_status": "failed", "human_status": "passed", "remark": "nul\x00"}, {"ai_status": "failed", "human_status": "passed", "revision": -1},
    {"ai_status": "failed", "human_status": "passed", "revision": 2**63}, {"ai_status": "failed", "human_status": "passed", "reviewer": "forged"},
])
def test_invalid_review_never_reaches_database(review_client, body):
    client, runtime, _ = review_client
    assert client.put("/admin/api/quality/rounds/r1/review", json=body).status_code == 422
    runtime.call.assert_not_called()


def test_review_load_and_missing_round_are_explicit(review_client):
    client, runtime, _ = review_client
    assert client.get("/admin/api/quality/rounds/r1/review").status_code == 200
    runtime.call.assert_awaited_once_with("round_review", "r1")
    runtime.call.return_value = None
    assert client.get("/admin/api/quality/rounds/missing/review").status_code == 404
    assert client.put("/admin/api/quality/rounds/missing/review", json={"ai_status": "pending", "human_status": "pending"}).status_code == 404


def test_stale_browser_edit_returns_conflict(review_client):
    client, runtime, _ = review_client
    runtime.call.side_effect = ReviewConflict()
    response = client.put("/admin/api/quality/rounds/r1/review", json={"ai_status": "failed", "human_status": "failed"})
    assert response.status_code == 409 and "其他窗口" in response.json()["detail"]


def test_failed_database_save_is_not_reported_as_success(review_client):
    client, runtime, _ = review_client
    runtime.call.side_effect = ArchiveUnavailable("private database failure")
    response = client.put("/admin/api/quality/rounds/r1/review", json={"ai_status": "failed", "human_status": "passed"})
    assert response.status_code == 503 and "private" not in response.text


def test_review_conflict_does_not_disable_other_archive_queries():
    runtime = QueryRuntime()
    runtime.ready = True
    runtime.repository = Mock()
    runtime._execute = AsyncMock(side_effect=ReviewConflict())
    with pytest.raises(ReviewConflict):
        asyncio.run(runtime.call("save_round_review", "r1", "failed", "passed", "", "admin", 0))
    assert runtime.ready
