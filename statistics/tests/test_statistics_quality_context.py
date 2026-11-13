"""质检共享统计上下文，并保留坐席细化的默认分时行为。"""

from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import StatisticsRepository


def repository_fixture():
    """只替换数据库边界，名单合并、排序、分页与报表组装均运行真实实现。"""
    period = ReportPeriod.build("day", date(2026, 10, 1))
    now = period.start + timedelta(hours=17, minutes=30)
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [
        {"source_key": "source", "account_id": 1, "agent_id": "7", "agent_name": "名单名称"}]
    context = SimpleNamespace(
        seat_totals=Mock(return_value={("source", 1, "7"): {
            "agent_name": "事实名称", "inquiries": 2, "replies": 3, "failed_inquiries": 1}}),
        seat_buckets=Mock(return_value={("source", 1, "7"): {"hour": {"replies": 3}}}),
        round_rows=Mock(return_value=[{"incoming": 3, "handoff": 2, "ai_replied_only": 1,
                                      "rejected": 1, "ai_failed": 1}]))

    class Repository(StatisticsRepository):
        @contextmanager
        def _read(self, budget):
            yield connection

    repository = Repository(None)
    repository.composer = Mock(return_value=context)
    repository._presence_minutes = Mock(return_value=[])
    return repository, context, connection, period, now


def test_quality_uses_one_context_without_seat_buckets():
    repository, context, _, period, now = repository_fixture()
    result = repository.quality_report(period, now=now, source_key="source", account_id=1)
    assert "buckets" not in result["agents"][0]
    assert result["agents"][0]["agent_name"] == "名单名称"
    assert result["total_inquiries"] == 2 and result["human_failure_rate"] == 50
    assert result["ai"]["total_rounds"] == 3
    repository.composer.assert_called_once()
    context.seat_buckets.assert_not_called()
    context.round_rows.assert_called_once_with(None)


def test_agents_defaults_keep_buckets_and_review_visibility():
    repository, context, connection, period, now = repository_fixture()
    result = repository._agents(connection, period, period.start, now, "source", 1, 1, 20, now)
    assert result["agents"][0]["buckets"] == {"hour": {"replies": 3}}
    assert "failed_inquiries" not in result["agents"][0]
    context.seat_buckets.assert_called_once_with(period.grain, period.buckets, seats=None)
