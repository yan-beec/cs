"""固定时钟与同一数据库状态下的质检完整响应、Excel 和加载次数回归。"""

from datetime import date, datetime, time, timedelta
import os
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from ics_agent.statistics import export
from ics_agent.statistics.periods import BEIJING, ReportPeriod
from ics_agent.statistics.quality_reports import percentage
from ics_agent.statistics.repository import StatisticsRepository, report_budget
from ics_agent.statistics.tests import test_statistics_database as base
from ics_agent.statistics.tests import test_statistics_rollups_database as rollup_base
from quality.presence.retention import retention_metadata
from quality.processing.rollups import Composer, RollupBuilder
from quality.processing.service_facts import FactSource
from quality.query.config import QuerySettings


NOW = datetime(2026, 10, 3, 16, 35, 27, tzinfo=BEIJING)


class HistoricalQualityRepository(StatisticsRepository):
    """保留优化前的两个上下文与默认坐席分时路径，作为回归对照。"""

    def quality_report(self, period, *, source_key=None, account_id=None, page=1, page_size=20,
                       now=None, budget=None):
        budget = budget or report_budget()
        now = now or datetime.now(BEIJING)
        retained_start = max(period.start, datetime.fromisoformat(retention_metadata(now)["retention_cutoff_at"]))
        through = min(period.end, now.replace(second=0, microsecond=0))
        with self._read(budget) as connection:
            human = self._agents(connection, period, retained_start, through, source_key, account_id,
                                 page, page_size, now, include_reviews=True)
            rows = self.composer(connection, period, now, source_key=source_key,
                                 account_id=account_id).round_rows(None) if period.start < now else []
        total = lambda key: sum(int(row.get(key) or 0) for row in rows)
        ai = {"total_rounds": total("incoming"), "handoff": total("handoff"), "ai_only": total("ai_replied_only"),
              "handoff_failed": total("rejected"), "failed_rounds": total("ai_failed")}
        ai["failure_rate"] = percentage(ai["failed_rounds"], ai["total_rounds"])
        return {**period.metadata(now), **human, "ai": ai,
                "human_failure_rate": percentage(human["total_failed_inquiries"], human["total_inquiries"])}


def without_seat_buckets(report):
    """只去掉明确取消的坐席分时字段，保留其他所有字段与元数据。"""
    return {**report, "agents": [{k: v for k, v in agent.items() if k != "buckets"}
                                 for agent in report["agents"]]}


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "需要隔离统计测试数据库")
class StatisticsQualityContextDatabaseTest(unittest.TestCase):
    setUpClass = classmethod(base.StatisticsDatabaseTest.setUpClass.__func__)
    tearDownClass = classmethod(base.StatisticsDatabaseTest.tearDownClass.__func__)
    receive = base.StatisticsDatabaseTest.receive
    drain = base.StatisticsDatabaseTest.drain
    assign_first_repliers = base.StatisticsDatabaseTest.assign_first_repliers
    refresh_facts = base.StatisticsDatabaseTest.refresh_facts
    review = base.StatisticsDatabaseTest.review
    sample = base.StatisticsDatabaseTest.sample
    scenario = rollup_base.StatisticsRollupsDatabaseTest.scenario
    build_all = rollup_base.StatisticsRollupsDatabaseTest.build_all

    def setUp(self):
        base.StatisticsDatabaseTest.setUp(self)
        self.admin.execute("TRUNCATE ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days, "
                           "ops.stat_round_days, ops.stat_seat_repeats, ops.stat_user_days")
        self.builder = RollupBuilder(self.processor.pool)
        self.scenario()
        self.sample(100, [(81, "online")])
        self.sample(100, [(91, "online")], account=2)
        for agent, name in ((80, "名单专用"), (83, "名单末位"), (7, "名单覆盖名称")):
            self.admin.execute("""INSERT INTO ops.agent_presence_registry
                (source_key,account_id,agent_id,agent_name,agent_name_source,first_seen_at,last_seen_at)
                VALUES(%s,1,%s,%s,'chatwoot',%s,%s) ON CONFLICT DO NOTHING""",
                (self.source, str(agent), name, NOW - timedelta(days=3), NOW - timedelta(days=3)))
        self.admin.execute("""UPDATE ops.round_index SET data=data ||
            '{"responsible_agent_id":"82","responsible_agent_name":"仅发配"}'::jsonb
            WHERE source_key=%s AND conversation_id=22""", (self.source,))
        self.refresh_facts()
        self.build_all(NOW)
        # 汇总构建器使用数据库时钟；把测试元数据固定到报告时钟之前。
        self.admin.execute("UPDATE ops.stat_rollup_days SET built_at=%s WHERE state='built'",
                           (NOW - timedelta(minutes=1),))
        with self.reports._read(report_budget()) as connection:
            period = ReportPeriod.build("day", date(2026, 10, 1))
            self.assertEqual(self.reports.composer(connection, period, NOW).clean, {date(2026, 10, 1)})
        self.legacy = HistoricalQualityRepository(self.reader.pool)

    def cases(self):
        return [
            ("today", ReportPeriod.build("day", date(2026, 10, 3))),
            ("clean", ReportPeriod.build("day", date(2026, 10, 1))),
            ("r10", ReportPeriod.build("day", date(2026, 10, 1)).refine(time(16, 30), time(23, 30))),
            ("month", ReportPeriod.build("month", date(2026, 10, 1))),
            ("year", ReportPeriod.build("year", date(2026, 10, 1))),
            ("cross_month", ReportPeriod.build("custom", date(2026, 9, 30), date(2026, 10, 3))),
        ]

    def scopes(self):
        return ({}, {"source_key": self.source}, {"source_key": self.source, "account_id": 1},
                {"source_key": self.source, "account_id": 2})

    def states(self):
        """每次改变状态之后，成对比较之间只读取，禁止维护任务改变快照。"""
        yield "built", self.cases()
        self.admin.execute("INSERT INTO ops.stat_dirty_days(day) VALUES('2026-10-01') ON CONFLICT DO NOTHING")
        yield "dirty", [("dirty", ReportPeriod.build("day", date(2026, 10, 1)))]
        self.admin.execute("""DELETE FROM ops.round_service_facts f USING ops.round_index r
            WHERE f.round_id=r.round_id AND r.source_key=%s AND r.conversation_id=12""",
                           (self.source,))
        self.assertGreater(self.admin.execute("SELECT count(*) AS n FROM ops.round_fact_pending").fetchone()["n"], 0)
        yield "pending", [("pending", ReportPeriod.build("day", date(2026, 10, 1)))]

    def instrument(self, method, period, **kwargs):
        """计数器包裹真实数据库路径，不替换任何统计结果。"""
        with patch("ics_agent.statistics.repository.Composer", wraps=Composer) as contexts, \
                patch.object(Composer, "seat_buckets", autospec=True, side_effect=Composer.seat_buckets) as buckets, \
                patch.object(FactSource, "load", wraps=FactSource.load) as facts:
            result = method(period, now=NOW, **kwargs)
        return result, (contexts.call_count, buckets.call_count, facts.call_count)

    def test_full_quality_responses_and_work_counts_match_historical_behavior(self):
        for state, cases in self.states():
            for name, period in cases:
                for scope in self.scopes():
                    complete = self.legacy.quality_report(period, now=NOW, page_size=10000, **scope)
                    last = max(1, complete["total"])
                    for page, size in ((1, 1), (2, 1), (last, 1), (1, 10000)):
                        with self.subTest(state=state, period=name, scope=scope, page=page, size=size):
                            kwargs = {**scope, "page": page, "page_size": size}
                            old = self.legacy.quality_report(period, now=NOW, **kwargs)
                            new, counts = self.instrument(self.reports.quality_report, period, **kwargs)
                            self.assertEqual(new, without_seat_buckets(old))
                            self.assertTrue(all("buckets" not in agent for agent in new["agents"]))
                            self.assertEqual(counts[:2], (1, 0))
                            self.assertLessEqual(counts[2], 1)
                            if state == "built" and name == "clean":
                                self.assertEqual(counts[2], 0)
                    old_agent, old_counts = self.instrument(self.legacy.agent_report, period, **scope)
                    new_agent, new_counts = self.instrument(self.reports.agent_report, period, **scope)
                    self.assertEqual(new_agent, old_agent)
                    self.assertEqual(new_counts, old_counts)
                    self.assertTrue(all("buckets" in agent for agent in new_agent["agents"]))
        report = self.reports.quality_report(self.cases()[3][1], now=NOW, source_key=self.source,
                                             account_id=1, page_size=10000)
        seats = {agent["agent_id"]: agent for agent in report["agents"]}
        self.assertEqual(seats["80"]["inquiries"], 0)
        self.assertEqual((seats["81"]["inquiries"], seats["81"]["online_minutes"]), (0, 1))
        self.assertEqual((seats["82"]["inquiries"], seats["82"]["assigned_inquiries"]), (0, 1))
        self.assertEqual(seats["7"]["agent_name"], "名单覆盖名称")

    def workbook_values(self, repository, period, scope):
        clock = Mock(wraps=datetime)
        clock.now.return_value = NOW
        settings = QuerySettings(_env_file=None, reader_password="unused-export-test")
        with patch.object(export, "datetime", clock), patch.object(export, "StatisticsRepository", repository):
            artifact = export.create_export(self.reader.pool, settings, "quality", period, **scope)
        try:
            book = load_workbook(artifact.path)
            try:
                return {sheet.title: list(sheet.iter_rows(values_only=True)) for sheet in book}
            finally:
                book.close()
        finally:
            artifact.cleanup()

    def test_excel_cells_equal_historical_exports_for_all_states_and_scopes(self):
        for state, cases in self.states():
            for name, period in cases:
                for scope in self.scopes():
                    with self.subTest(state=state, period=name, scope=scope):
                        old = self.workbook_values(HistoricalQualityRepository, period, scope)
                        new = self.workbook_values(StatisticsRepository, period, scope)
                        self.assertEqual(new, old)
