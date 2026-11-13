"""Calendar boundaries, missing observations and authorized report execution."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import require_admin
from ics_agent.statistics import routes
from ics_agent.statistics.periods import BEIJING, ReportPeriod
from ics_agent.statistics.repository import StatisticsRepository


@pytest.mark.parametrize("mode,selected,size,first,end", [
    ("day", date(2026, 10, 20), 24, "2026-10-20T00:00:00+08:00", "2026-10-21T00:00:00+08:00"),
    ("month", date(2024, 2, 29), 29, "2024-02-01T00:00:00+08:00", "2024-03-01T00:00:00+08:00"),
    ("month", date(2025, 2, 20), 28, "2025-02-01T00:00:00+08:00", "2025-03-01T00:00:00+08:00"),
    ("year", date(2026, 10, 20), 12, "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00"),
    ("month", date(2099, 12, 1), 31, "2099-12-01T00:00:00+08:00", "2100-01-01T00:00:00+08:00"),
])
def test_calendar_windows_are_contiguous_and_end_exclusive(mode, selected, size, first, end):
    period = ReportPeriod.build(mode, selected)
    assert len(period.buckets) == size
    assert period.start.isoformat() == first and period.end.isoformat() == end
    assert all(a["end"] == b["start"] for a, b in zip(period.buckets, period.buckets[1:]))
    assert period.buckets[-1]["end"] == period.end
    assert period.start.utcoffset() == timedelta(hours=8)


@pytest.mark.parametrize("first,last,size,exclusive_end", [
    (date(2026, 10, 20), date(2026, 10, 20), 1, "2026-10-21T00:00:00+08:00"),
    (date(2026, 9, 29), date(2026, 10, 2), 4, "2026-10-03T00:00:00+08:00"),
    (date(2025, 12, 30), date(2026, 1, 2), 4, "2026-01-03T00:00:00+08:00"),
    (date(2024, 2, 28), date(2024, 3, 1), 3, "2024-03-02T00:00:00+08:00"),
    (date(2099, 12, 31), date(2099, 12, 31), 1, "2100-01-01T00:00:00+08:00"),
])
def test_custom_dates_include_both_days_and_use_contiguous_daily_buckets(first, last, size, exclusive_end):
    period = ReportPeriod.build("custom", first, last)
    assert period.grain == "day" and period.selected == first
    assert period.start == datetime.combine(first, datetime.min.time(), BEIJING)
    assert period.end.isoformat() == exclusive_end
    assert len(period.buckets) == size
    assert [b["label"] for b in period.buckets] == [(first + timedelta(days=i)).isoformat() for i in range(size)]
    assert all(b["end"] - b["start"] == timedelta(days=1) for b in period.buckets)
    assert all(a["end"] == b["start"] for a, b in zip(period.buckets, period.buckets[1:]))
    assert period.buckets[-1]["end"] == period.end
    metadata = period.metadata(period.start + timedelta(hours=12))
    assert metadata["start_date"] == first.isoformat() and metadata["end_date"] == last.isoformat()
    assert metadata["buckets"][0]["partial"] and not metadata["buckets"][0]["future"]
    assert all(b["future"] for b in metadata["buckets"][1:])


@pytest.mark.parametrize("first,last", [
    (date(2026, 10, 20), None), (date(2026, 10, 20), date(2026, 10, 19)),
    (date(1999, 12, 31), date(2000, 1, 1)), (date(2099, 12, 31), date(2100, 1, 1)),
])
def test_custom_period_rejects_incomplete_reversed_and_out_of_bounds_dates(first, last):
    with pytest.raises(ValueError):
        ReportPeriod.build("custom", first, last)


@pytest.mark.parametrize("first,last,size", [
    (date(2026, 1, 1), date(2026, 12, 31), 365),
    (date(2026, 1, 1), date(2027, 1, 1), 366),
    (date(2028, 1, 1), date(2028, 12, 31), 366),
])
def test_custom_period_accepts_up_to_366_inclusive_calendar_days(first, last, size):
    period = ReportPeriod.build("custom", first, last)
    assert len(period.buckets) == size
    assert period.start.date() == first
    assert (period.end - timedelta(days=1)).date() == last


def test_custom_period_rejects_367_inclusive_calendar_days():
    with pytest.raises(ValueError, match="自定义时间段最多 366 天（含起止日期），请缩小范围。"):
        ReportPeriod.build("custom", date(2026, 1, 1), date(2027, 1, 2))


def minute(at, online, *, samples=2):
    return {"bucket": at, "online": online, "busy": 0, "offline": samples - online,
            "unknown": 0, "peak_online": online, "observed_minutes": 1, "samples": samples}


def test_presence_keeps_observed_zero_missing_and_future_distinct():
    period = ReportPeriod.build("day", date(2026, 10, 20))
    start = period.start
    now = start + timedelta(minutes=4, seconds=20)
    result = StatisticsRepository._presence(period,
        [minute(start, 0), minute(start + timedelta(minutes=2), 1, samples=1)],
        [{"started_minute": start, "ended_minute": None}] * 2,
        start, start + timedelta(minutes=4), now)
    points = result["presence_points"]
    assert len(points) == 1440
    assert [p["online"] for p in points[:5]] == [0, None, 1, None, None]
    assert not points[1]["future"] and points[4]["future"]
    row = result["presence_rows"][0]
    assert row["online"] == .5 and row["peak_online"] == 1
    assert row["samples"] == 3 and row["expected_samples"] == 8
    assert row["coverage"] == 37.5 and row["status"] == "partial"


def test_monthly_presence_averages_minutes_instead_of_summing_seats():
    period = ReportPeriod.build("month", date(2026, 10, 1))
    row = {**minute(period.start, 2, samples=120), "online": 2.5, "peak_online": 3, "observed_minutes": 60}
    result = StatisticsRepository._presence(period, [row], [], period.start, period.end, period.end)
    assert result["presence_points"][0]["online"] == 2.5
    assert result["presence_rows"][0]["peak_online"] == 3
    assert result["presence_points"][1]["online"] is None


def test_custom_presence_uses_daily_averages_and_keeps_missing_days_and_future_empty():
    period = ReportPeriod.build("custom", date(2026, 9, 30), date(2026, 10, 3))
    observed = [{**minute(period.start, 2, samples=120), "online": 2.5,
                 "peak_online": 3, "observed_minutes": 60}]
    now = period.start + timedelta(days=2, hours=12)
    result = StatisticsRepository._presence(period, observed,
        [{"started_minute": period.start, "ended_minute": None}], period.start, now, now)
    assert [p["label"] for p in result["presence_points"]] == ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]
    assert [p["online"] for p in result["presence_points"]] == [2.5, None, None, None]
    assert [p["future"] for p in result["presence_points"]] == [False, False, False, True]
    assert result["presence_rows"][0]["peak_online"] == 3
    assert result["presence_rows"][1]["status"] == "missing"
    assert result["presence_rows"][-1]["status"] == "future"
    assert result["presence_summary"]["observed_minutes"] == 60


def test_expired_retention_is_not_a_zero_or_an_unmonitored_day():
    period = ReportPeriod.build("month", date(2026, 9, 1))
    cutoff = datetime(2026, 9, 5, 4, tzinfo=BEIJING)
    result = StatisticsRepository._presence(period, [], [], cutoff, period.end, period.end)
    assert result["presence_rows"][0]["status"] == "expired"
    assert result["presence_rows"][4]["online"] is None
    assert all(row["online"] is None for row in result["presence_points"])


@pytest.fixture
def client(monkeypatch):
    runtime = SimpleNamespace(ready=True, repository=SimpleNamespace(pool=object()),
        _execute=AsyncMock(return_value={"rows": []}), call=AsyncMock(return_value={"sources": [], "channels": []}))
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app), runtime, app


@pytest.mark.parametrize("endpoint", ["time", "agents", "quality", "filters"])
def test_reports_reuse_admin_authorization(client, endpoint):
    browser, runtime, app = client
    app.dependency_overrides.clear()
    assert browser.get("/admin/api/statistics/" + endpoint).status_code == 401
    runtime._execute.assert_not_called()


@pytest.mark.parametrize("params", [
    {"mode": "week"}, {"date": "2026-02-30"}, {"date": "1999-12-31"}, {"date": "2100-01-01"},
    {"source_key": "instance"}, {"account_id": 1}, {"source_key": " bad ", "account_id": 1},
    {"page_size": 101}, {"page": 0},
])
def test_invalid_or_unscoped_filters_never_reach_database(client, params):
    browser, runtime, _ = client
    assert browser.get("/admin/api/statistics/agents", params=params).status_code == 422
    runtime._execute.assert_not_called()


@pytest.mark.parametrize("endpoint", ["agents", "quality"])
def test_paginated_reports_share_validation_and_bounded_executor(client, endpoint):
    browser, runtime, _ = client
    assert browser.get(f"/admin/api/statistics/{endpoint}", params={"page": 2, "page_size": 10,
        "mode": "year", "date": "2026-10-20"}).status_code == 200
    args, kwargs = runtime._execute.call_args
    assert args[0].__name__ == ("quality_report" if endpoint == "quality" else "agent_report")
    assert len(args[1].buckets) == 12
    assert kwargs.pop("budget").connect_seconds == 1
    assert kwargs == {"source_key": None, "account_id": None, "page": 2, "page_size": 10}
    runtime._execute.reset_mock()
    assert browser.get(f"/admin/api/statistics/{endpoint}", params={"page": 0}).status_code == 422
    runtime._execute.assert_not_called()


def test_reports_use_bounded_archive_executor_and_calendar_filters(client):
    browser, runtime, _ = client
    assert browser.get("/admin/api/statistics/time", params={"mode": "month", "date": "2024-02-29",
        "source_key": "one", "account_id": 1, "channel_key": "wechat"}).status_code == 200
    args, kwargs = runtime._execute.call_args
    assert args[0].__self__.pool is runtime.repository.pool
    assert args[1].start.day == 1 and len(args[1].buckets) == 29
    assert kwargs.pop("budget").connect_seconds == 1
    assert kwargs == {"source_key": "one", "account_id": 1, "channel_key": "wechat"}


@pytest.mark.parametrize("endpoint", ["time", "agents", "quality"])
def test_custom_range_reaches_each_report_with_inclusive_end_and_existing_filters(client, endpoint):
    browser, runtime, _ = client
    params = {"mode": "custom", "start_date": "2025-12-30", "end_date": "2026-01-02",
              "source_key": "one", "account_id": 1}
    params.update({"channel_key": "wechat"} if endpoint == "time" else {"page": 2, "page_size": 10})
    assert browser.get(f"/admin/api/statistics/{endpoint}", params=params).status_code == 200
    args, kwargs = runtime._execute.call_args
    period = args[1]
    assert args[0].__name__ == {"time": "time_report", "agents": "agent_report", "quality": "quality_report"}[endpoint]
    assert period.start.isoformat() == "2025-12-30T00:00:00+08:00"
    assert period.end.isoformat() == "2026-01-03T00:00:00+08:00"
    assert period.grain == "day" and len(period.buckets) == 4
    assert kwargs.pop("budget").connect_seconds == 1
    assert kwargs == {"source_key": "one", "account_id": 1,
                      **({"channel_key": "wechat"} if endpoint == "time" else {"page": 2, "page_size": 10})}


@pytest.mark.parametrize("endpoint", ["time", "agents", "quality"])
@pytest.mark.parametrize("params", [
    {"mode": "custom"}, {"mode": "custom", "start_date": "2026-10-01"},
    {"mode": "custom", "end_date": "2026-10-20"},
    {"mode": "custom", "start_date": "2026-10-20", "end_date": "2026-10-01"},
    {"mode": "custom", "start_date": "2026-02-30", "end_date": "2026-03-01"},
    {"mode": "custom", "start_date": "1999-12-31", "end_date": "2000-01-01"},
    {"mode": "custom", "start_date": "2099-12-31", "end_date": "2100-01-01"},
    {"mode": "month", "start_date": "2026-10-01", "end_date": "2026-10-20"},
])
def test_invalid_custom_ranges_are_rejected_before_any_database_query(client, endpoint, params):
    browser, runtime, _ = client
    assert browser.get(f"/admin/api/statistics/{endpoint}", params=params).status_code == 422
    runtime._execute.assert_not_called()


@pytest.mark.parametrize("endpoint", [
    "time", "agents", "quality", "time/export.xlsx", "agents/export.xlsx", "quality/export.xlsx",
    "users", "users/detail", "users/export.xlsx",
])
def test_oversized_custom_range_returns_422_for_reports_exports_and_users(client, endpoint):
    _, _, app = client
    browser = TestClient(app, raise_server_exceptions=False)
    response = browser.get(f"/admin/api/statistics/{endpoint}", params={
        "mode": "custom", "start_date": "2026-01-01", "end_date": "2027-01-02",
        "source_key": "one", "account_id": 1, "contact_id": "42",
    })
    assert response.status_code == 422
    assert response.json()["detail"] == "自定义时间段最多 366 天（含起止日期），请缩小范围。"


def test_report_failure_does_not_disable_archive_and_can_retry(client):
    browser, runtime, _ = client
    runtime._execute.side_effect = RuntimeError("do not expose connection credentials")
    response = browser.get("/admin/api/statistics/time")
    assert response.status_code == 503 and "credentials" not in response.text
    assert runtime.ready
    runtime._execute.side_effect = None
    assert browser.get("/admin/api/statistics/time").status_code == 200


@pytest.mark.parametrize("asset", ["reports.js", "reports.css"])
def test_local_assets_are_served_without_auth_and_invalid_names_are_rejected(client, asset):
    browser, _, app = client
    app.dependency_overrides.clear()
    assert browser.get("/admin/statistics/assets/" + asset).status_code == 200
    assert browser.get("/admin/statistics/assets/routes.py").status_code == 422


def test_statistics_directory_does_not_shadow_python_standard_library():
    import statistics
    assert statistics.mean([1, 2, 3]) == 2
    assert not hasattr(statistics, "__path__")


def test_user_ranking_window_refines_day_and_custom_with_inclusive_minutes(client):
    """R13：用户统计按日/自定义可加二级时刻，结束时刻含该分钟；前 100 名最多 5 页。"""
    browser, runtime, _ = client
    assert browser.get("/admin/api/statistics/users", params={"mode": "custom", "start_date": "2026-11-09",
        "end_date": "2026-11-09", "start_time": "09:00", "end_time": "18:59", "channel_key": "wechat"}).status_code == 200
    args, kwargs = runtime._execute.call_args
    assert args[0].__name__ == "user_report"
    assert args[1].start.isoformat() == "2026-11-09T09:00:00+08:00"
    assert args[1].end.isoformat() == "2026-11-09T19:00:00+08:00"
    assert kwargs.pop("budget").connect_seconds == 1
    assert kwargs == {"source_key": None, "account_id": None, "channel_key": "wechat", "page": 1, "page_size": 20}
    metadata = args[1].metadata(args[1].start)
    assert (metadata["start_label"], metadata["end_label"]) == ("2026-11-09 09:00:00", "2026-11-09 18:59:59")
    runtime._execute.reset_mock()
    for params in ({"page": 6}, {"mode": "month", "start_time": "09:00"}, {"start_time": "9:00"},
                   {"start_time": "18:00", "end_time": "09:00"}):
        assert browser.get("/admin/api/statistics/users", params=params).status_code == 422, params
    runtime._execute.assert_not_called()


def test_user_trace_requires_scoped_contact(client):
    browser, runtime, _ = client
    assert browser.get("/admin/api/statistics/users/detail", params={"contact_id": "42"}).status_code == 422
    assert browser.get("/admin/api/statistics/users/detail", params={
        "source_key": "one", "account_id": 1, "contact_id": "42", "date": "2026-11-09"}).status_code == 200
    args, kwargs = runtime._execute.call_args
    assert args[0].__name__ == "user_detail"
    assert kwargs.pop("budget").connect_seconds == 1
    assert kwargs == {"source_key": "one", "account_id": 1, "contact_id": "42", "channel_key": None}


def test_refined_period_clips_buckets_and_keeps_full_day_by_default():
    period = ReportPeriod.build("day", date(2026, 11, 9))
    assert period.refine() is period
    assert period.refine(datetime.min.time(), datetime.min.time().replace(hour=23, minute=59)) is period
    window = period.refine(datetime.min.time().replace(hour=9), None)
    assert window.refined and not period.refined and window.metadata(window.start)["refined"]
    assert window.start.hour == 9 and window.end == period.end and len(window.buckets) == 15
    assert window.buckets[0]["start"] == window.start


def test_original_reports_accept_only_whole_hour_time_detail(client):
    """R10：时间统计/坐席细化/质检统计也可指定时刻，但只到整点（回复类指标按小时预聚合）。"""
    browser, runtime, _ = client
    for kind, method in (("time", "time_report"), ("agents", "agent_report"), ("quality", "quality_report")):
        runtime._execute.reset_mock()
        assert browser.get(f"/admin/api/statistics/{kind}", params={"date": "2026-11-09", "start_time": "09:00",
                                                                     "end_time": "18:59"}).status_code == 200, kind
        args, _ = runtime._execute.call_args
        assert args[0].__name__ == method
        assert (args[1].start.isoformat(), args[1].end.isoformat()) == ("2026-11-09T09:00:00+08:00",
                                                                         "2026-11-09T19:00:00+08:00")
        assert args[1].refined and [b["label"] for b in args[1].buckets][0] == "09:00" and len(args[1].buckets) == 10
        runtime._execute.reset_mock()
        for params in ({"start_time": "09:30"}, {"end_time": "18:30"}, {"mode": "month", "start_time": "09:00"}):
            assert browser.get(f"/admin/api/statistics/{kind}", params=params).status_code == 422, (kind, params)
        runtime._execute.assert_not_called()


def test_composer_reads_rollups_only_for_days_fully_inside_the_window():
    from quality.processing import rollups
    seen = {}
    def fake_clean(connection, days, now):
        seen["days"] = list(days)
        return set(days)
    original = rollups.clean_days
    rollups.clean_days = fake_clean
    try:
        start = datetime(2026, 11, 7, 9, tzinfo=BEIJING)
        end = datetime(2026, 11, 9, 19, tzinfo=BEIJING)
        composer = rollups.Composer(None, start, end, datetime(2026, 11, 11, tzinfo=BEIJING), period_variant=None)
    finally:
        rollups.clean_days = original
    assert seen["days"] == [date(2026, 11, 8)]
    assert composer.live_days == [date(2026, 11, 7), date(2026, 11, 9)]
