"""用户排行读取排队计数，验证同口径、跨周、来源隔离和不可用状态。"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ics_agent.app.handoff_queue.policy import success_counts, week_anchor
from ics_agent.app.handoff_queue.repository import HandoffQueueRepository
from ics_agent.statistics import repository
from ics_agent.statistics.periods import ReportPeriod


FRIDAY = datetime(2026, 11, 13, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("now", [FRIDAY - timedelta(microseconds=1), FRIDAY, FRIDAY + timedelta(seconds=1)])
def test_batch_snapshot_reuses_queue_windows_and_keeps_missing_zero(now):
    rows = [dict(account_id="1", contact_id="42", week_anchor=FRIDAY - timedelta(days=7), week_count=30,
                 success_times=[now - timedelta(days=1), now - timedelta(microseconds=1), now,
                                now + timedelta(microseconds=1)]),
            dict(account_id="2", contact_id="42", week_anchor=FRIDAY, week_count=7, success_times=[now])]
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = rows
    pool = Mock()
    pool.connection.return_value.__enter__ = Mock(return_value=connection)
    pool.connection.return_value.__exit__ = Mock(return_value=False)
    repo = HandoffQueueRepository(None, pool=pool)
    assert hasattr(repo, "counter_snapshot"), "缺少复用排队规则的批量计数读取"
    snapshot = repo.counter_snapshot([("1", "42"), ("2", "42"), ("1", "99")], now=now)
    assert snapshot["counts"] == {("1", "42"): success_counts(rows[0], now),
                                  ("2", "42"): success_counts(rows[1], now), ("1", "99"): (0, 0)}
    assert snapshot["as_of"] == now and snapshot["week_start"] == week_anchor(now)
    assert snapshot["counts"][("1", "42")][0] == 2
    assert connection.execute.call_count == 1


@pytest.fixture
def report_runtime(monkeypatch):
    from quality.assignment.config import AssignmentSyncSettings
    from ics_agent.app.handoff_queue.runtime import get_handoff_queue_runtime

    monkeypatch.setenv("ARCHIVE_SOURCE_KEY", "current-source")
    monkeypatch.setenv("ARCHIVE_ASSIGNMENT_SYNC_ENABLED", "false")
    assert AssignmentSyncSettings().source_key == "current-source"
    queue = get_handoff_queue_runtime()
    counts = Mock(return_value={"as_of": FRIDAY, "week_start": FRIDAY,
                               "counts": {("1", "42"): (3, 31), ("2", "42"): (0, 0)}})
    monkeypatch.setattr(queue, "ready", True)
    monkeypatch.setattr(queue, "repository", SimpleNamespace(counter_snapshot=counts))
    users = [{"source_key": source, "account_id": account, "contact_id": "42", "assignments": 99}
             for source, account in [("current-source", 1), ("current-source", 2), ("other-source", 1)]]
    archive = Mock(return_value={"users": users, "as_of": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(repository.user_ranking, "user_report", archive)
    active = []

    @contextmanager
    def read(self, budget):
        active.append(True)
        try:
            yield object()
        finally:
            active.clear()

    monkeypatch.setattr(repository.StatisticsRepository, "_read", read)
    return repository.StatisticsRepository(object()), counts, queue, archive, active


@pytest.mark.parametrize("full_ranking", [False, True])
def test_page_and_export_use_live_counts_without_period_channel_or_foreign_source(report_runtime, full_ranking):
    reports, counts, _, archive, active = report_runtime
    expected = counts.return_value

    def snapshot(keys, **kwargs):
        assert not active, "必须先释放归档连接，再读取主库计数"
        assert keys == [("1", "42"), ("2", "42")]
        assert "now" not in kwargs, "不能使用历史报表的时刻查询实时排队计数"
        return expected

    counts.side_effect = snapshot
    result = reports.user_report(ReportPeriod.build("day", date(2020, 1, 1)), channel_key="one-channel",
                                 full_ranking=full_ranking)
    assert [(u.get("daily_count"), u.get("weekly_count")) for u in result["users"]] == [(3, 31), (0, 0), (None, None)]
    assert result["queue_counts"]["as_of"] == FRIDAY.isoformat()
    assert result["queue_counts"]["week_start"] == FRIDAY.isoformat()
    assert result["queue_counts"]["available"] is True
    assert all(u["assignments"] == 99 for u in result["users"])
    counts.assert_called_once()
    assert archive.call_args.kwargs["full_ranking"] is full_ranking


@pytest.mark.parametrize("failure", ["not_ready", "read_failed"])
def test_unavailable_counts_are_not_fabricated_as_zero(report_runtime, monkeypatch, failure):
    reports, counts, queue, _, _ = report_runtime
    if failure == "not_ready":
        monkeypatch.setattr(queue, "ready", False)
    else:
        counts.side_effect = RuntimeError("禁止暴露连接密码")
    result = reports.user_report(ReportPeriod.build("day", date(2020, 1, 1)))
    assert all(u.get("daily_count") is None and u.get("weekly_count") is None for u in result["users"])
    assert result["queue_counts"]["available"] is False
    assert "禁止暴露" not in str(result)
