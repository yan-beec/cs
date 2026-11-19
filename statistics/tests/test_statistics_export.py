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
               **dict.fromkeys([key for key, *_ in (*export.SEAT_FIELDS, *export.HUMAN_FIELDS)], 0),
               "first_response_seconds": None, "average_response_seconds": 82.5,
               "replies": 2, "online_minutes": 0, "busy_minutes": 0, "offline_minutes": 0, "unknown_minutes": 0,
               "buckets": {}} for i in range(seats)]
    return {**result, "agents": agents, "total": seats, "total_inquiries": 0, "total_replies": 2 * seats,
            "unassigned_inquiries": 0, "assisted_inquiries": 0, "unassigned_lost_inquiries": 0,
            "total_failed_inquiries": 0, "human_failure_rate": None,
            "presence_summary": {"observed_minutes": 0, "samples": 0, "expected_samples": 0, "peak_online": None},
            "ai": {"total_rounds": 4, "handoff": 3, "ai_only": 1, "handoff_failed": 1, "failed_rounds": 1, "failure_rate": 25.0},
            "presence_points": [{"key": PERIOD.buckets[0]["key"], "label": "2026-10-01", "online": 0},
                                {"key": PERIOD.buckets[1]["key"], "label": "2026-10-02", "online": None}]}


def section(sheet, title):
    """(表头, 数据行[, 合计行]) of the table under a section title; stops at a blank or note row."""
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    start = next(i for i, row in enumerate(rows) if row and row[0] == title) + 1
    while not any(value is not None for value in rows[start]):  # Space reserved for a chart.
        start += 1
    header, body = rows[start], []
    for row in rows[start + 1:]:
        if not any(value is not None for value in row[1:]):
            break
        body.append(row)
    return header, body


def cards(sheet):
    """统计概况可能排成多行：标签行与数值行交替出现，直到空行。"""
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    start = next(i for i, row in enumerate(rows) if row and row[0] == "统计概况") + 1
    result = {}
    while start + 1 < len(rows) and any(value is not None for value in rows[start]):
        result.update({label: value for label, value in zip(rows[start], rows[start + 1]) if label})
        start += 2
    return result


@pytest.mark.parametrize("kind", ["time", "agents", "quality"])
def test_workbook_mirrors_the_page_with_summary_tables_totals_and_help_sheet(kind):
    stream = BytesIO()
    data = report(kind)
    if kind != "time":
        data["agents"][1].update(first_response_seconds=30.0, first_response_samples=1, average_response_seconds=60.0,
                                 response_samples=3, inquiries=4, failed_inquiries=1, failure_rate=25.0)
        data.update(total_inquiries=4, total_replies=50, unassigned_inquiries=2, assisted_inquiries=1,
                    unassigned_lost_inquiries=0, total_failed_inquiries=1, human_failure_rate=25.0,
                    presence_summary={"observed_minutes": 10, "samples": 9, "expected_samples": 10, "peak_online": 3})
    export.build_workbook(stream, kind, data, PERIOD, channels=[("wechat", "微信")])
    book = load_workbook(BytesIO(stream.getvalue()))
    data_sheets = [export.TITLES[kind], *(export.AGENT_SHEETS if kind == "agents" else ())]
    assert book.sheetnames == [*data_sheets, "口径说明"]
    for name in data_sheets:  # Every data sheet starts with the same title and filter line.
        assert book[name]["A1"].value == export.TITLES[kind] and "统计周期" in book[name]["A2"].value
    sheet = book[export.TITLES[kind]]
    assert sheet["A1"].value == export.TITLES[kind]
    assert "2026-10-01 至 2026-10-02（2026-10-01 00:00:00 至 2026-10-02 23:59:59）" in sheet["A2"].value
    assert all(cell.data_type != "f" for row in sheet for cell in row)
    if kind == "time":
        assert cards(sheet)["进客量"] == 0
        header, body = section(sheet, "时段数据")
        assert header[0] == "时间区间" and header[-1] == "微信进客量"
        assert [row[0] for row in body] == ["2026-10-01 · 进行中", "2026-10-02", "合计"]
        assert body[0][1] == 0 and body[1][1] == "—"  # Real zero stays a number; future shows “—”.
        assert len(sheet._charts) == 1
    elif kind == "agents":
        assert cards(sheet)["参与统计坐席"] == 25 and cards(sheet)["状态采样覆盖率"] == pytest.approx(0.9)
        header, body = section(sheet, "客服回复质量")
        assert header[:4] == ["坐席", "来源 / 账号 / 坐席ID", "首响时长", "平均响应时长"]
        assert body[0][0] == "=1+1" and sheet.cell(row=sheet.max_row, column=1).data_type == "s"
        assert sheet[f"A{next(c.row for c in sheet['A'] if c.value == '=1+1')}"].data_type == "s"
        assert len(body) == 26 and body[24][0] == "坐席24"  # All seats, not only the UI's first page.
        assert body[0][2] == "—"  # No first response sample.
        seconds = lambda value: round(value.total_seconds(), 3)  # [h]:mm:ss cells read back as durations.
        assert (seconds(body[1][2]), seconds(body[1][3]), seconds(body[2][3])) == (30, 60, 82.5)
        total = body[-1]
        assert total[0] == "合计" and total[header.index("回复询盘总量")] == 4
        # Weighted by samples like the page: only seat 1 has first-response and response samples.
        assert (seconds(total[2]), seconds(total[3])) == (30, 60)
        workload = section(book["坐席回复询盘"], "坐席回复询盘（按实际回复时间归入时段）")
        assert workload[0][2:8] == ["回复询盘合计", "公开回复数", "在线分钟", "忙碌分钟", "2026-10-01", "2026-10-02"]
        assert workload[1][0][4] == "—"  # Missing presence does not become zero.
        presence = section(book["可用客服量"], "可用客服量（平均在线人数）")
        assert presence[1] == [["2026-10-01", 0] + [None] * (len(presence[1][0]) - 2),
                               ["2026-10-02", "—"] + [None] * (len(presence[1][0]) - 2)]
    else:
        assert cards(sheet)["人工不合格率"] == pytest.approx(0.25)
        header, body = section(sheet, "人工质检表")
        assert body[1][header.index("不合格率")] == pytest.approx(0.25)
        assert body[-1][0] == "合计" and body[-1][header.index("不合格率")] == pytest.approx(0.25)
        ai = section(sheet, "AI质检报表（按轮次开始时间统计全部对话）")
        assert ai[0][2] == "所有对话轮数" and ai[1][0][2] == 4 and ai[1][0][-1] == pytest.approx(0.25)
    help_sheet = book["口径说明"]
    texts = [row for row in help_sheet.iter_rows(values_only=True) if row and row[0]]
    assert ("字段说明", None) in texts and ("统计规则", None) in texts
    book.close()


def test_glossary_matches_the_page_help_texts():
    import json, shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = Path(export.__file__).parent / "web" / "reports.js"
    program = ("const fs=require('fs'),vm=require('vm');const ctx={window:{}};vm.createContext(ctx);"
               f"vm.runInContext(fs.readFileSync({json.dumps(str(script))},'utf8'),ctx);"
               "process.stdout.write(JSON.stringify(ctx.window.ICSReports.help));")
    help_texts = json.loads(subprocess.run([node, "-e", program], capture_output=True, text=True, encoding="utf-8",
                                           check=True).stdout)
    from ics_agent.statistics import glossary
    assert help_texts == {"time": glossary.TIME_HELP, "agents": glossary.AGENT_HELP,
                          "quality": glossary.QUALITY_HELP, "users": glossary.USER_HELP}


def test_layout_aligns_cards_and_charts_to_the_table_width():
    from openpyxl.drawing.spreadsheet_drawing import TwoCellAnchor
    # Cards fill the content width exactly, in rows of equal count, with near-equal widths.
    for widths, count in [([14] + [12] * 9, 6), ([12, 24] + [11] * 10, 8), ([8, 12, 10, 12, 13, 11, 21, 21, 10], 5)]:
        layout = export.card_layout(widths, count)
        assert sum(len(row) for row in layout) == count and len({len(row) for row in layout}) == 1
        for row in layout:
            assert row[0][0] == 1 and row[-1][1] == len(widths)
            assert all(a[1] + 1 == b[0] for a, b in zip(row, row[1:]))
    # The trend chart is pinned to cells inside the content width, above the table, not floating to the right.
    captured, original = [], export._Sheet.line_chart
    def spy(self, *args, **kwargs):
        original(self, *args, **kwargs)
        captured.append((self.sheet._charts[-1].anchor, self.width))
    stream = BytesIO()
    with patch.object(export._Sheet, "line_chart", spy):
        export.build_workbook(stream, "time", report("time"), PERIOD, channels=[("wechat", "微信")])
    sheet = load_workbook(BytesIO(stream.getvalue()))["时间统计"]
    header_row = next(c.row for c in sheet["A"] if c.value == "时间区间")
    (anchor, width), = captured
    assert isinstance(anchor, TwoCellAnchor) and anchor._from.col == 0 and anchor.to.col == width == 8
    assert anchor.to.row + 1 < header_row  # 0-based marker: the chart ends above the table header.


def test_user_workbook_has_ranking_only_without_trace_details():
    data = {**PERIOD.metadata(PERIOD.start + timedelta(hours=12)), "total_assignments": 5, "users_total": 2,
            "ranked_assignments": 5, "ranked_share": 100.0, "unidentified_assignments": 0, "before_earliest": False,
            "users": [{"rank": i + 1, "source_key": "s", "account_id": 1, "contact_id": str(40 + i), "display_name": f"用户{i}",
                       "channels": ["wechat"], "assignments": 3 - i, "conversations": 1, "blacklist_status": None,
                       "daily_count": 24 if i == 0 else 0, "weekly_count": 60 if i == 0 else None,
                       "first_assigned_at": "2026-10-01T01:00:00+00:00", "last_assigned_at": "2026-10-01T02:00:00+00:00"}
                      for i in range(2)]}
    stream = BytesIO()
    export.build_user_workbook(stream, data, channels=[("wechat", "微信")])
    book = load_workbook(BytesIO(stream.getvalue()))
    assert book.sheetnames == ["用户统计", "口径说明"]
    sheet = book["用户统计"]
    header, body = section(sheet, "用户成功转人工排行（前 100 名）")
    assert "涉及会话数" not in header
    assert list(header[5:7]) == ["近24小时计数", "当前周次计数"]
    assert list(body[0][5:7]) == [24, 60]
    assert list(body[1][5:7]) == [0, "—"]
    assert [row[2] for row in body] == ["40", "41"]
    texts = [value for row in sheet.iter_rows(values_only=True) for value in row if isinstance(value, str)]
    assert not any("溯源" in text for text in texts)
    # The ranking is the last table: nothing follows its rows.
    title_row = next(c.row for c in sheet["A"] if c.value == "用户成功转人工排行（前 100 名）")
    assert sheet.max_row == title_row + 1 + len(body)
    help_rows = [row[0] for row in book["口径说明"].iter_rows(values_only=True) if row and row[0]]
    assert "溯源明细" not in help_rows


def test_user_export_fetches_full_ranking_without_details(monkeypatch):
    repository = Mock()
    repository.user_report.return_value = {**PERIOD.metadata(PERIOD.start), "users": [], "total_assignments": 0,
                                           "users_total": 0, "ranked_assignments": 0, "ranked_share": None,
                                           "unidentified_assignments": 0, "before_earliest": False}
    monkeypatch.setattr(export, "StatisticsRepository", lambda _: repository)
    settings = QuerySettings(_env_file=None, reader_password="test-reader")
    artifact = export.create_export(object(), settings, "users", PERIOD)
    try:
        kwargs = repository.user_report.call_args.kwargs
        assert kwargs["full_ranking"] is True and "with_details" not in kwargs
    finally:
        artifact.cleanup()


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
            assert book.sheetnames == [export.TITLES[kind], *(export.AGENT_SHEETS if kind == "agents" else ()), "口径说明"]
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
