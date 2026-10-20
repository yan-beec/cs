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
    assert kwargs == {"source_key": "one", "account_id": 1, "channel_key": "wechat"}


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
