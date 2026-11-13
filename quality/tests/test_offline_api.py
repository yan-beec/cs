from fastapi import FastAPI
from fastapi.testclient import TestClient
import sqlite3
import pytest

from ics_agent.app.core.auth import require_admin
from quality.offline import routes
from quality.offline.config import OfflineSettings
from quality.offline.jobs import OfflineJobs


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_QUERY_ENABLED", "true")
    jobs = OfflineJobs(OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0,
                                       isolated_process=False))
    monkeypatch.setattr(routes, "get_offline_jobs", lambda: jobs)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app), jobs, app


def test_month_scope_is_validated_and_persisted(client):
    browser, jobs, _ = client
    assert browser.post("/admin/api/quality/offline", json={"mode": "month", "month": "2026-13"}).status_code == 422
    r = browser.post("/admin/api/quality/offline", json={"mode": "month", "month": "2026-10"})
    assert r.status_code == 202
    assert jobs.get(r.json()["id"])["request"]["month"] == "2026-10"
    assert browser.get("/admin/api/quality/offline").json()["items"][0]["id"] == r.json()["id"]


def test_admin_required_for_all_management_and_download_requires_ticket(client):
    browser, jobs, app = client
    job = jobs.enqueue({"mode": "all"})
    app.dependency_overrides.clear()
    path = "/admin/api/quality/offline"
    assert browser.post(path, json={"mode": "all"}).status_code == 401
    assert browser.get(path).status_code == 401
    assert browser.delete(path + "/" + job["id"]).status_code == 401
    assert browser.post(path + "/" + job["id"] + "/ticket").status_code == 401
    assert browser.get(path + "/" + job["id"] + "/download?ticket=invalid").status_code == 403


@pytest.mark.parametrize("request_scope,expected", [
    ({"mode": "month", "month": "2026-10"}, "聊天记录2026年10月.html"),
    ({"mode": "month", "month": "2027-01"}, "聊天记录2027年1月.html"),
])
def test_download_is_a_single_html_named_by_month(client, request_scope, expected):
    from urllib.parse import quote
    browser, jobs, _ = client
    job = jobs.enqueue(request_scope)
    def builder(request, work, progress, check):
        work.mkdir(parents=True)
        path = work / "archive.html"
        path.write_text("<!doctype html><title>留档</title>", encoding="utf-8")
        return path
    jobs.run_once(builder)
    assert jobs.list()[0]["filename"] == expected
    url = browser.post(f"/admin/api/quality/offline/{job['id']}/ticket").json()["url"]
    response = browser.get(url)
    assert response.status_code == 200 and response.text.startswith("<!doctype html>")
    assert response.headers["content-type"].startswith("text/html")
    assert quote(expected) in response.headers["content-disposition"]
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"] == "sandbox"
    # 完整下载后标记为已下载，文件在保留期内仍可重新下载（防止代理缓冲、浏览器取消保存时丢失）。
    assert (jobs.root / job["id"] / "archive.html").is_file()
    state = jobs.get(job["id"])
    assert state["status"] == "downloaded" and state["available"]
    first = state["updated"]
    again = browser.post(f"/admin/api/quality/offline/{job['id']}/ticket")
    assert again.status_code == 200
    assert browser.get(again.json()["url"]).status_code == 200
    assert jobs.get(job["id"])["updated"] == first  # 重新下载不顺延保留期


def test_html_and_media_zip_are_two_downloads(client):
    from urllib.parse import quote
    browser, jobs, _ = client
    job = jobs.enqueue({"mode": "month", "month": "2026-10"})
    def builder(request, work, progress, check):
        work.mkdir(parents=True)
        (work / "media.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        path = work / "archive.html"
        path.write_text("<!doctype html>", encoding="utf-8")
        return path
    jobs.run_once(builder)
    state = jobs.list()[0]
    assert state["media_size"] == 22 and state["media_filename"] == "聊天记录2026年10月-附件.zip"
    assert state["media_folder"] == "ics-attachments-2026-10"
    assert state["size"] == 22 + len("<!doctype html>")
    base = f"/admin/api/quality/offline/{job['id']}"
    assert browser.post(base + "/ticket?part=other").status_code == 422
    html_url = browser.post(base + "/ticket").json()["url"]
    media_url = browser.post(base + "/ticket?part=media").json()["url"]
    assert media_url.endswith("&part=media")
    # 签名区分 HTML 与附件包，不能拿 HTML 的签名下载附件包。
    assert browser.get(html_url + "&part=media").status_code == 403
    response = browser.get(media_url)
    assert response.status_code == 200 and response.content.startswith(b"PK")
    assert response.headers["content-type"] == "application/zip"
    assert quote("聊天记录2026年10月-附件.zip") in response.headers["content-disposition"]
    assert browser.get(html_url).text == "<!doctype html>"


def test_job_without_media_has_no_media_download(client):
    browser, jobs, _ = client
    job = jobs.enqueue({"mode": "all"})
    def builder(request, work, progress, check):
        work.mkdir(parents=True)
        path = work / "archive.html"
        path.write_text("x", encoding="utf-8")
        return path
    jobs.run_once(builder)
    assert jobs.list()[0]["media_size"] == 0 and jobs.list()[0]["media_filename"] is None
    assert browser.post(f"/admin/api/quality/offline/{job['id']}/ticket?part=media").status_code == 409


def test_redownload_refused_after_downloaded_hours(client, monkeypatch):
    import time
    browser, jobs, _ = client
    job = jobs.enqueue({"mode": "all"})
    def builder(request, work, progress, check):
        work.mkdir(parents=True)
        path = work / "archive.html"
        path.write_text("<!doctype html>", encoding="utf-8")
        return path
    jobs.run_once(builder)
    url = browser.post(f"/admin/api/quality/offline/{job['id']}/ticket").json()["url"]
    assert browser.get(url).status_code == 200
    now = time.time()
    # 清理任务尚未运行，但超过保留期后按时间拒绝下载。
    monkeypatch.setattr(time, "time", lambda: now + 2 * 3600 + 1)
    again = browser.post(f"/admin/api/quality/offline/{job['id']}/ticket")
    assert again.status_code == 409 and "已删除" in again.json()["detail"]
    jobs.cleanup()
    assert not (jobs.root / job["id"]).exists()
    assert jobs.get(job["id"])["status"] == "downloaded"


def test_delete_record_endpoint(client):
    browser, jobs, app = client
    path = "/admin/api/quality/offline"
    running = jobs.enqueue({"mode": "all"})
    r = browser.delete(f"{path}/{running['id']}/record")
    assert r.status_code == 409 and "先取消" in r.json()["detail"]
    jobs.cancel(running["id"])
    assert browser.delete(f"{path}/{running['id']}/record").json() == {"deleted": True}
    assert browser.delete(f"{path}/{running['id']}/record").status_code == 404
    assert all(item["id"] != running["id"] for item in browser.get(path).json()["items"])
    app.dependency_overrides.clear()
    assert browser.delete(f"{path}/{running['id']}/record").status_code == 401


def test_interrupted_download_keeps_file_for_retry(client):
    browser, jobs, _ = client
    job = jobs.enqueue({"mode": "all"})
    def builder(request, work, progress, check):
        work.mkdir(parents=True)
        path = work / "archive.html"
        path.write_bytes(b"x" * (routes.CHUNK_BYTES * 3))
        return path
    jobs.run_once(builder)
    path = jobs.artifact(job["id"])
    sent = {}
    stream = routes.one_time_file(path, sent)
    next(stream)
    stream.close()  # 浏览器中途断开：生成器未读到末尾
    import asyncio
    asyncio.run(routes.consume_after_download(job["id"], sent))
    assert path.is_file() and jobs.get(job["id"])["status"] == "completed"


def test_other_scopes_are_named_with_export_date():
    from datetime import datetime, timedelta, timezone
    from quality.offline.naming import download_name
    at = datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)  # 北京时间 11 月 6 日凌晨
    assert download_name({"mode": "all"}, at) == "聊天记录全部历史-20261106.html"
    # 筛选导出精确到分钟（北京时间），同一天多次导出不重名。
    assert download_name({"mode": "rounds"}, at.timestamp()) == "聊天记录筛选导出-20261106-0100.html"
    assert download_name({"mode": "customers"}, at + timedelta(minutes=59, seconds=59)) == "聊天记录筛选导出-20261106-0159.html"


@pytest.mark.parametrize("method,suffix,body", [
    ("get", "", None), ("post", "", {"mode":"all"}),
    ("delete", "/"+"a"*32, None), ("post", "/"+"a"*32+"/ticket", None),
    ("get", "/"+"a"*32+"/download?ticket=invalid", None),
])
def test_storage_failure_reports_actionable_message(client, monkeypatch, method, suffix, body):
    browser, _, _ = client
    def unavailable():
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(routes, "get_offline_jobs", unavailable)
    response = browser.request(method, "/admin/api/quality/offline" + suffix, json=body)
    assert response.status_code == 503
    assert "目录" in response.json()["detail"] and "权限" in response.json()["detail"]
