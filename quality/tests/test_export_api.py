"""Download scope, authorization, cleanup, and export resource isolation."""

import asyncio
from pathlib import Path
import tempfile
import threading
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import require_admin
from quality.query import routes
from quality.query.config import QuerySettings
from quality.query.export import ExportFile, ExportNotFound, ExportTooLarge
from quality.query.repository import ArchiveFilters
from quality.query.runtime import ArchiveUnavailable, ExportBusy, QueryRuntime


@pytest.fixture(autouse=True)
def isolated_directory(monkeypatch):
    directory = Mock(enrich=AsyncMock(side_effect=lambda options: options), close=AsyncMock())
    monkeypatch.setattr("quality.query.directory.ChatwootDirectory", lambda *args: directory)


@pytest.fixture
def download_client(monkeypatch):
    runtime = Mock()
    runtime.export = AsyncMock()
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app), runtime, app


def artifact():
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as output:
        output.write(b"controlled-workbook-response")
        return ExportFile(output.name, "ics-archive-test.xlsx")


def test_all_excel_downloads_require_existing_admin_login(download_client):
    client, runtime, app = download_client
    app.dependency_overrides.clear()
    for path in ("export.xlsx", "rounds/r1/export.xlsx", "customers/c1/export.xlsx"):
        assert client.get("/admin/api/quality/" + path).status_code == 401
    runtime.export.assert_not_called()


def test_filtered_download_reuses_exact_filters_and_ignores_pagination(download_client):
    client, runtime, _ = download_client
    result = artifact()
    runtime.export.return_value = result
    try:
        response = client.get("/admin/api/quality/export.xlsx", params={
            "mode": "rounds", "source_key": "server-a", "account_id": 2, "inbox_id": 3,
            "agent_id": "7", "agent_scope": "assigned", "keyword": "original",
            "start": "2026-10-16T08:00:00+08:00", "page": 99, "page_size": 1,
        })
        assert response.status_code == 200
        assert response.content == b"controlled-workbook-response"
        assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in response.headers["content-disposition"]
        mode, filters = runtime.export.call_args.args
        assert mode == "rounds" and filters.keyword == "original"
        assert filters.inbox_id == 3
        assert (filters.source_key, filters.account_id, filters.agent_id, filters.agent_scope) == ("server-a", 2, "7", "assigned")
        assert runtime.export.call_args.kwargs == {}
        assert not Path(result.path).exists()
    finally:
        result.cleanup()


@pytest.mark.parametrize("path,mode,scope", [
    ("customers/customer-one/export.xlsx", "customers", {"customer_id": "customer-one"}),
    ("rounds/round-one/export.xlsx", "rounds", {"round_id": "round-one"}),
])
def test_detail_download_does_not_inherit_any_list_filters(download_client, path, mode, scope):
    client, runtime, _ = download_client
    result = artifact()
    runtime.export.return_value = result
    try:
        assert client.get("/admin/api/quality/" + path, params={"keyword": "ignored", "page": 9}).status_code == 200
        runtime.export.assert_awaited_once_with(mode, ArchiveFilters(), **scope)
    finally:
        result.cleanup()


@pytest.mark.parametrize("error,status", [
    (ExportBusy(), 429), (ExportTooLarge("请缩小筛选范围"), 422),
    (ExportNotFound(), 404), (ArchiveUnavailable("sensitive SQL"), 503),
])
def test_export_errors_are_explicit_without_a_partial_file(download_client, error, status):
    client, runtime, _ = download_client
    runtime.export.side_effect = error
    response = client.get("/admin/api/quality/export.xlsx")
    assert response.status_code == status
    assert "detail" in response.json() and "sensitive" not in response.text
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize("params", [{"mode": "other"}, {"agent_id": "7"}, {"rating": 6}])
def test_invalid_export_scope_does_not_start_work(download_client, params):
    client, runtime, _ = download_client
    assert client.get("/admin/api/quality/export.xlsx", params=params).status_code == 422
    runtime.export.assert_not_called()


def test_download_file_is_removed_even_if_sending_fails():
    result = artifact()
    async def check():
        async def failed_send(message):
            raise RuntimeError("client disconnected")
        with pytest.raises(RuntimeError):
            await routes.ExportResponse(result)({"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}, AsyncMock(), failed_send)
    try:
        asyncio.run(check())
        assert not Path(result.path).exists()
    finally:
        result.cleanup()


def test_export_has_its_own_capacity_and_cancelled_files_are_removed():
    async def check():
        runtime = QueryRuntime()
        settings = QuerySettings(_env_file=None, query_enabled=True, reader_password="test-reader")
        started = threading.Event()
        generated = []
        repository = Mock()
        def create(*args, cancelled, **kwargs):
            started.set()
            cancelled.wait(timeout=4)
            result = artifact()
            generated.append(result)
            return result
        repository.create_export.side_effect = create
        with patch("quality.query.runtime.QuerySettings", return_value=settings), patch.object(runtime, "_recover", new=AsyncMock()), patch("quality.query.runtime.ExportRepository", return_value=repository):
            await runtime.start()
            runtime.ready = True
            runtime.repository = Mock()
            runtime.repository.list_rounds.return_value = {"total": 3}
            task = asyncio.create_task(runtime.export("rounds", ArchiveFilters()))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                assert await runtime.call("list_rounds", ArchiveFilters()) == {"total": 3}
                with pytest.raises(ExportBusy):
                    await runtime.export("rounds", ArchiveFilters())
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert generated and not Path(generated[0].path).exists()
                assert runtime.ready
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await runtime.close()
                for result in generated:
                    result.cleanup()
    asyncio.run(check())


def test_export_failure_does_not_disable_interactive_queries():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = QuerySettings(_env_file=None, reader_password="test-reader")
        repository = Mock()
        repository.create_export.side_effect = RuntimeError("sensitive conversation")
        with patch("quality.query.runtime.ExportRepository", return_value=repository):
            try:
                with pytest.raises(ArchiveUnavailable):
                    await runtime.export("rounds", ArchiveFilters())
                assert runtime.ready
            finally:
                await runtime.close()
    asyncio.run(check())


def test_disabled_archive_does_not_allocate_export_resources():
    async def check():
        runtime = QueryRuntime()
        with patch("quality.query.runtime.ExportRepository") as repository:
            with pytest.raises(ArchiveUnavailable):
                await runtime.export("rounds", ArchiveFilters())
            assert runtime.export_executor is None
            repository.assert_not_called()
    asyncio.run(check())


def test_cancelling_name_lookup_releases_export_capacity_without_starting_database_work():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = QuerySettings(_env_file=None, reader_password="test-reader")
        started = asyncio.Event()
        async def lookup(options):
            started.set()
            await asyncio.Event().wait()
        runtime.directory = Mock(enrich=AsyncMock(side_effect=lookup), close=AsyncMock())
        task = asyncio.create_task(runtime.export("rounds", ArchiveFilters()))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            with pytest.raises(ExportBusy):
                await runtime.export("rounds", ArchiveFilters())
            runtime.directory.enrich.assert_awaited_once()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.export_slot._value == 1
            assert runtime.export_repository is None
            assert not runtime.export_cancellations
            assert runtime.ready is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()
    asyncio.run(check())


def test_disconnected_request_drains_export_and_releases_its_slot(monkeypatch):
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = QuerySettings(_env_file=None, reader_password="test-reader")
        generated = []
        repository = Mock()
        def create(*args, cancelled, **kwargs):
            assert cancelled.wait(timeout=3), "Disconnected export was not cancelled"
            result = artifact()
            generated.append(result)
            return result
        repository.create_export.side_effect = create
        request = Mock()
        request.is_disconnected = AsyncMock(return_value=True)
        monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
        with patch("quality.query.runtime.ExportRepository", return_value=repository):
            try:
                response = await routes._export(request, "rounds", ArchiveFilters())
                assert response.status_code == 499
                assert generated and not Path(generated[0].path).exists()
                assert runtime.export_slot._value == 1
                assert runtime.ready
            finally:
                await runtime.close()
                for result in generated:
                    result.cleanup()
    asyncio.run(check())


def test_disconnect_concurrent_with_completion_cleans_completed_file(monkeypatch):
    async def check():
        result = artifact()
        runtime = Mock()
        runtime.export = AsyncMock(return_value=result)
        monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
        request = Mock()
        request.is_disconnected = AsyncMock(return_value=True)
        try:
            response = await routes._export(request, "customers", ArchiveFilters())
            assert response.status_code == 499
            assert not Path(result.path).exists()
        finally:
            result.cleanup()
    asyncio.run(check())


def test_handler_cancellation_drains_export_and_removes_late_file(monkeypatch):
    async def check():
        result = artifact()
        started = asyncio.Event()
        async def export(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return result
        runtime = Mock()
        runtime.export = export
        monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
        request = Mock()
        request.is_disconnected = AsyncMock(return_value=False)
        task = asyncio.create_task(routes._export(request, "rounds", ArchiveFilters()))
        try:
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not Path(result.path).exists()
        finally:
            result.cleanup()
    asyncio.run(check())
