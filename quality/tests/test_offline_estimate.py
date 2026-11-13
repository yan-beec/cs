"""筛选导出只导出命中轮次；导出前预估数量，超过提醒线需二次确认，超过上限拒绝，失败不影响服务。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import require_admin
from quality.offline import repository as offline_repository, routes
from quality.offline.bundle import BundleLimit, build_bundle
from quality.offline.config import OfflineSettings
from quality.offline.jobs import OfflineJobs
from quality.offline.media import MediaDownloader
from quality.offline.repository import assess, limit_message, scope_predicate


def settings(tmp_path, **kwargs):
    return OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0, isolated_process=False, **kwargs)


@pytest.mark.parametrize("mode", ["rounds", "customers"])
def test_filtered_scope_selects_matching_rounds_in_both_views(mode):
    sql, values = scope_predicate({"mode": mode, "filters": {"rating_status": "rated",
                                                             "start": "2026-10-01T00:00:00+08:00"}})
    # 只按命中的轮次取消息，不再导出客户的全部历史。
    assert sql.startswith("m.round_id IN (SELECT r.round_id FROM ops.round_index r")
    assert "m.customer_id IN" not in sql
    assert "rated" in values


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        scope_predicate({"mode": "everything"})


def test_assessment_levels(tmp_path):
    s = settings(tmp_path, warn_messages=100, max_messages=1000, warn_rounds=10, max_rounds=100)
    base = {"customers": 1, "attachments": 0, "attachment_bytes": 0, "text_bytes": 0}
    assert assess({**base, "messages": 0, "rounds": 0}, s)["level"] == "empty"
    assert assess({**base, "messages": 50, "rounds": 5}, s)["level"] == "ok"
    warned = assess({**base, "messages": 500, "rounds": 5}, s)
    assert warned["level"] == "warn" and "提醒线" in warned["warnings"][0]
    blocked = assess({**base, "messages": 1001, "rounds": 5}, s)
    assert blocked["level"] == "block" and "ARCHIVE_OFFLINE_MAX_MESSAGES" in blocked["reason"]
    assert assess({**base, "messages": 50, "rounds": 101}, s)["level"] == "block"
    assert limit_message({"messages": 1000, "rounds": 100}, s) is None
    media = assess({**base, "messages": 5, "rounds": 1, "attachment_bytes": s.max_embedded_total_bytes + 1}, s)
    assert media["level"] == "warn" and any("原始链接" in w for w in media["warnings"])


def test_bundle_stops_streaming_beyond_message_cap(tmp_path):
    rows = ({"message_id": str(n), "customer_id": "c1", "conversation_id": "1", "round_id": "r1",
             "sender_role": "contact", "content": "x"} for n in range(10))
    with pytest.raises(BundleLimit, match="上限 3"):
        build_bundle(tmp_path / "work", rows, settings(tmp_path, max_messages=3), MediaDownloader([]),
                     lambda **_: None, lambda: None)
    assert not (tmp_path / "work" / "archive.html").exists()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_QUERY_ENABLED", "true")
    jobs = OfflineJobs(settings(tmp_path))
    monkeypatch.setattr(routes, "get_offline_jobs", lambda: jobs)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app), jobs


def test_estimate_uses_same_scope_and_filters(client, monkeypatch):
    browser, _ = client
    seen = {}
    def fake(request, s):
        seen.update(request)
        return {"messages": 12, "rounds": 2, "level": "ok"}
    monkeypatch.setattr(offline_repository, "estimate", fake)
    r = browser.post("/admin/api/quality/offline/estimate?rating_status=rated", json={"mode": "customers"})
    assert r.status_code == 200 and r.json()["level"] == "ok"
    assert seen["mode"] == "rounds" and seen["filters"]["rating_status"] == "rated"


def test_estimate_failure_returns_503_without_breaking_service(client, monkeypatch):
    browser, _ = client
    def broken(request, s):
        raise ConnectionError("archive down")
    monkeypatch.setattr(offline_repository, "estimate", broken)
    r = browser.post("/admin/api/quality/offline/estimate", json={"mode": "all"})
    assert r.status_code == 503 and "暂时无法预估" in r.json()["detail"]
    # 锁已释放，下一次请求照常处理。
    monkeypatch.setattr(offline_repository, "estimate", lambda request, s: {"level": "ok"})
    assert browser.post("/admin/api/quality/offline/estimate", json={"mode": "all"}).status_code == 200


def test_concurrent_estimate_is_refused(client):
    browser, _ = client
    assert routes.estimate_lock.acquire(blocking=False)
    try:
        r = browser.post("/admin/api/quality/offline/estimate", json={"mode": "all"})
        assert r.status_code == 429
    finally:
        routes.estimate_lock.release()


def test_create_records_filtered_exports_as_rounds(client):
    browser, jobs = client
    r = browser.post("/admin/api/quality/offline?rating_status=rated", json={"mode": "customers", "month": "2026-10"})
    assert r.status_code == 202
    request = jobs.get(r.json()["id"])["request"]
    assert request["mode"] == "rounds" and request["month"] is None
    assert request["filters"]["rating_status"] == "rated"


def test_limit_failure_marks_only_the_job_failed(client):
    _, jobs = client
    job = jobs.enqueue({"mode": "all"})
    def too_big(request, work, progress, check):
        raise BundleLimit("导出范围内有 400,000 条消息，超过单个离线页面上限")
    assert jobs.run_once(too_big)
    state = jobs.get(job["id"])
    assert state["status"] == "failed" and "上限" in state["error"]
    assert not (jobs.root / job["id"]).exists()


@pytest.mark.parametrize("endpoint", ["", "/estimate"])
def test_filtered_summary_and_view_are_preserved(client, monkeypatch, endpoint):
    browser, jobs = client
    seen = {}
    def fake(request, settings):
        seen.update(request)
        return {"level": "ok"}
    monkeypatch.setattr(offline_repository, "estimate", fake)
    body = {"mode": "rounds", "summary": "关键词：退款\n当前分配坐席：小王", "view": "customers"}
    response = browser.post("/admin/api/quality/offline" + endpoint, json=body)
    assert response.status_code in (200, 202)
    saved = seen if endpoint else jobs.get(response.json()["id"])["request"]
    assert saved["summary"] == body["summary"] and saved["view"] == "customers"


@pytest.mark.parametrize("body", [{"mode": "rounds", "summary": "字" * 2001},
                                  {"mode": "rounds", "view": "invalid"}])
def test_scope_rejects_invalid_summary_and_view(client, body):
    browser, _ = client
    assert browser.post("/admin/api/quality/offline", json=body).status_code == 422


@pytest.mark.parametrize("mode", ["month", "all"])
def test_unfiltered_scope_does_not_save_page_summary_or_view(mode):
    request = routes.request_body(routes.Scope(mode=mode, month="2026-11", summary="页面条件", view="customers"),
                                  routes.ArchiveFilters())
    assert "summary" not in request and "view" not in request
