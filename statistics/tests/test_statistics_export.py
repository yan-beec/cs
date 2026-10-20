"""Actual workbook contents, complete filtered downloads and export isolation."""

import asyncio
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from ics_agent.app.core.auth import require_admin
from ics_agent.statistics import export, routes
from ics_agent.statistics.periods import ReportPeriod
from quality.query.config import QuerySettings
from quality.query.export import ExportCancelled, ExportFile, ExportTooLarge
from quality.query.runtime import ArchiveUnavailable, ExportBusy, QueryRuntime


PERIOD = ReportPeriod.build("custom", date(2026, 10, 1), date(2026, 10, 2))


def report(kind, seats=25):
    result = PERIOD.metadata(PERIOD.start + timedelta(hours=12))
    if kind == "time":
        return {**result, "rows": [dict.fromkeys([key for key, _ in export.TIME_FIELDS], 0) | {"channels": {"wechat": 0}}] * 2,
                "channels": {"wechat": 0}, "totals": dict.fromkeys([key for key, _ in export.TIME_FIELDS], 0)}
    agents = [{"source_key": "source", "account_id": 1, "agent_id": str(i), "agent_name": "=1+1" if i == 0 else f"坐席{i}",
               **dict.fromkeys([key for key, _ in (*export.SEAT_FIELDS, *export.HUMAN_FIELDS)], 0),
               "first_response_seconds": None, "average_response_seconds": 82.5,
               "replies": 2, "online_minutes": 0, "busy_minutes": 0, "offline_minutes": 0, "unknown_minutes": 0,
               "buckets": {}} for i in range(seats)]
    return {**result, "agents": agents, "total": seats,
            "ai": {"total_rounds": 4, "handoff": 3, "ai_only": 1, "handoff_failed": 1, "failed_rounds": 1, "failure_rate": 25.0},
            "presence_points": [{"key": PERIOD.buckets[0]["key"], "online": 0}, {"key": PERIOD.buckets[1]["key"], "online": None}]}


@pytest.mark.parametrize("kind", ["time", "agents", "quality"])
def test_single_sheet_contains_only_headers_and_all_data(kind):
    stream = BytesIO()
    export.build_workbook(stream, kind, report(kind), PERIOD, channels=[("wechat", "微信")])
    book = load_workbook(BytesIO(stream.getvalue()))
    assert book.sheetnames == [export.TITLES[kind]]
    sheet = book.active
    rows = list(sheet.values)
    assert rows[0][0] == ("时段（北京时间）" if kind == "time" else "坐席")
    assert not sheet._charts
    assert all(cell.data_type != "f" for row in sheet for cell in row)
    assert all(cell.comment is None for row in sheet for cell in row)
    if kind == "time":
        assert sheet.max_row == 4  # Fields, two days, total. No intro or notes.
        assert rows[0][-1] == "微信进客量"
        assert rows[1][1] == 0 and rows[2][1] is None  # Real zero versus future.
        assert rows[-1][0] == "合计"
    else:
        assert rows[1][0] == "=1+1" and sheet["A2"].data_type == "s"
        assert rows[25][0] == "坐席24"  # Includes seats beyond the UI's first page.
        assert sheet["D2"].data_type == "s"
        if kind == "agents":
            assert sheet["E2"].value is None
            assert rows[1][5] == 82.5 and sheet["F2"].data_type == "n"
            assert rows[28][6] is None  # Missing presence does not become zero.
            assert rows[-2][:2] == ("2026-10-01 00:00", 0)
            assert rows[-1][:2] == ("2026-10-02 00:00", None)
        else:
            assert rows[-2][:2] == ("统计周期", "所有对话轮数")
            assert rows[-1][6] == 25.0
    book.close()


@pytest.mark.parametrize("kind", ["time", "agents", "quality"])
def test_export_uses_same_filters_and_fetches_all_seats_in_one_snapshot(monkeypatch, kind):
    repository = Mock()
    method = {"time": "time_report", "agents": "agent_report", "quality": "quality_report"}[kind]
    getattr(repository, method).return_value = report(kind)
    factory = Mock(return_value=repository)
    monkeypatch.setattr(export, "StatisticsRepository", factory)
    settings = QuerySettings(_env_file=None, reader_password="test-reader")
    pool = object()
    artifact = export.create_export(pool, settings, kind, PERIOD, source_key="source", account_id=1,
                                    channel_key="wechat" if kind == "time" else None)
    try:
        factory.assert_called_once_with(pool)
        args, kwargs = getattr(repository, method).call_args
        assert args == (PERIOD,) and kwargs["source_key"] == "source" and kwargs["account_id"] == 1
        if kind == "time":
            assert kwargs["channel_key"] == "wechat"
        else:
            assert kwargs["page"] == 1 and kwargs["page_size"] == settings.export_max_entities
        book = load_workbook(artifact.path, read_only=True)
        try:
            assert book.sheetnames == [export.TITLES[kind]]
        finally:
            book.close()
    finally:
        artifact.cleanup()


def test_limits_and_cancellation_leave_no_partial_file(monkeypatch, tmp_path):
    settings = QuerySettings(_env_file=None, reader_password="test-reader", export_max_entities=1)
    repository = Mock(agent_report=Mock(return_value=report("agents")))
    monkeypatch.setattr(export, "StatisticsRepository", lambda _: repository)
    with pytest.raises(ExportTooLarge):
        export.create_export(object(), settings, "agents", PERIOD)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ExportCancelled):
        export.create_export(object(), settings, "time", PERIOD, cancelled=cancelled)
    repository.time_report.assert_not_called()
    # Cancellation while writing must also remove the temporary download.
    repository.time_report.return_value = report("time")
    real_temp = export.tempfile.mkstemp
    monkeypatch.setattr(export.tempfile, "mkstemp", lambda **kw: real_temp(dir=tmp_path, **kw))
    def interrupted(*args, **kwargs):
        args[0].write(b"partial")
        raise ExportCancelled()
    monkeypatch.setattr(export, "build_workbook", interrupted)
    with pytest.raises(ExportCancelled):
        export.create_export(object(), settings, "time", PERIOD)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def client(monkeypatch):
    runtime = SimpleNamespace(export=AsyncMock())
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app), runtime, app


@pytest.mark.parametrize("kind", ["time", "agents", "quality"])
def test_download_auth_exact_custom_scope_and_cleanup(client, tmp_path, kind):
    browser, runtime, app = client
    path = tmp_path / "data.xlsx"
    path.write_bytes(b"workbook")
    runtime.export.return_value = ExportFile(str(path), "统计报表.xlsx")
    response = browser.get(f"/admin/api/statistics/{kind}/export.xlsx", params={
        "mode": "custom", "start_date": "2026-10-01", "end_date": "2026-10-02",
        "source_key": "source", "account_id": 1, "page": 2, "page_size": 1,
        **({"channel_key": "wechat"} if kind == "time" else {})})
    assert response.status_code == 200 and response.content == b"workbook"
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert not path.exists()
    args, kwargs = runtime.export.call_args
    assert args == (kind, PERIOD)
    assert kwargs["builder"] is export.create_export
    assert kwargs["source_key"] == "source" and kwargs["account_id"] == 1
    assert kwargs["channel_key"] == ("wechat" if kind == "time" else None)
    assert "page" not in kwargs and "page_size" not in kwargs
    runtime.export.reset_mock()
    app.dependency_overrides.clear()
    assert browser.get(f"/admin/api/statistics/{kind}/export.xlsx").status_code == 401
    runtime.export.assert_not_called()


@pytest.mark.parametrize("error,status", [(ExportBusy(), 429), (ExportTooLarge("范围过大"), 422), (ArchiveUnavailable(), 503)])
def test_export_errors_have_no_attachment(client, error, status):
    browser, runtime, _ = client
    runtime.export.side_effect = error
    response = browser.get("/admin/api/statistics/time/export.xlsx")
    assert response.status_code == status and "content-disposition" not in response.headers


@pytest.mark.parametrize("path,params", [
    ("time", {"mode": "custom", "start_date": "2026-10-02", "end_date": "2026-10-01"}),
    ("agents", {"source_key": "source"}), ("quality", {"channel_key": "wechat"}), ("unknown", {})])
def test_invalid_export_filters_do_not_start_worker(client, path, params):
    browser, runtime, _ = client
    assert browser.get(f"/admin/api/statistics/{path}/export.xlsx", params=params).status_code == 422
    runtime.export.assert_not_called()


def test_statistics_builder_uses_shared_export_capacity_and_dedicated_pool():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = QuerySettings(_env_file=None, reader_password="test-reader")
        repository = Mock()
        artifact = Mock(spec=ExportFile)
        builder = Mock(return_value=artifact)
        with patch("quality.query.runtime.ExportRepository", return_value=repository):
            try:
                assert await runtime.export("time", PERIOD, builder=builder, channel_key="wechat") is artifact
                args, kwargs = builder.call_args
                assert args == (repository.pool, runtime.settings, "time", PERIOD)
                assert kwargs["channel_key"] == "wechat" and isinstance(kwargs["cancelled"], threading.Event)
                repository.create_export.assert_not_called()
            finally:
                await runtime.close()
    asyncio.run(check())
