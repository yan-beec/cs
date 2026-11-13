"""Opt-in: R13 用户成功转人工排行与坐席细化发配量、对话归档按用户命中轮次一致。

场景（全部在 2026-10-01 北京时间）：
- 联系人 42：会话 12、22 由 ICS 分配成功（各 1 次）；会话 52 坐席网页端手动接待（无 ICS 分配，不计）；
  会话 62 只收到繁忙拒接（不计）；会话 72 纯 AI（不计）。
- 联系人 99：会话 32、42 由 ICS 分配成功（2 次，一次未回复也计）。
两人次数相同（2），按最近一次成功转人工时间决出名次：42 的最近一次（会话 12，22:40）晚于 99（会话 42，20:10）。
"""

from datetime import date, datetime, time, timedelta, timezone
import os
import unittest
from io import BytesIO

from ics_agent.statistics.export import build_user_workbook
from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import BUSY_NOTICE, StatisticsRepository
from ics_agent.statistics.tests import test_statistics_database as base
from quality.query.repository import ArchiveFilters
from quality.tests.test_processing import agent_reply, event
from quality.tests import test_query_database as fixture


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires isolated reporting test database")
class UserHandoffRankingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        self.reports = StatisticsRepository(self.reader.pool)

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    assign_first_repliers = base.StatisticsDatabaseTest.assign_first_repliers
    refresh_facts = base.StatisticsDatabaseTest.refresh_facts

    def scenario(self):
        self.receive(
            # 联系人 42：同一轮里连续 3 次“转人工”，只有一次成功分配（R13-02）。
            event(1, 400, content="我要转人工"), event(2, 401, content="转人工"), event(3, 402, content="人工"),
            agent_reply(4, 403),
            event(11, 300, conversation_id=22, content="我要转人工"), agent_reply(12, 302, conversation_id=22),
            event(51, 260, conversation_id=52), agent_reply(52, 262, conversation_id=52, agent_id=9),
            event(61, 100, conversation_id=62, content="我要转人工"),
            event(62, 101, conversation_id=62, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(71, 120, conversation_id=72),
            event(72, 121, conversation_id=72, sender="agent_bot", direction="outgoing"),
            # 之后移到联系人 99 的两个会话。
            event(21, 200, conversation_id=32, content="我要转人工"), agent_reply(22, 205, conversation_id=32, agent_id=8),
            event(41, 250, conversation_id=42, content="我要转人工"))
        self.drain()
        self.assign_first_repliers(12, 22, 32)
        self.admin.execute("""UPDATE ops.round_index SET data=data || jsonb_build_object('responsibility_basis',
            'ics_assignment','responsible_agent_id','8','responsible_agent_name','Seat 8',
            'responsible_assigned_at',data->'handoff_requested_at') WHERE source_key=%s AND conversation_id=42""",
                           (self.source,))
        other = f"customer:{self.source}:99"
        self.admin.execute("""INSERT INTO ops.customer_index (customer_id,source_key,account_id,contact_id,display_name)
            VALUES (%s,%s,1,'99','客户乙')""", (other, self.source))
        self.admin.execute("UPDATE ops.round_index SET customer_id=%s WHERE source_key=%s AND conversation_id IN (32,42)",
                           (other, self.source))
        self.admin.execute("UPDATE ops.message_index SET customer_id=%s WHERE source_key=%s AND conversation_id IN (32,42)",
                           (other, self.source))
        self.refresh_facts()
        return other

    def report(self, period, **kwargs):
        return self.reports.user_report(period, source_key=self.source, account_id=1,
                                        now=datetime.now(timezone.utc), **kwargs)

    def test_ranking_counts_successful_ics_assignments_like_assigned_inquiries(self):
        other = self.scenario()
        day = ReportPeriod.build("day", date(2026, 10, 1))
        report = self.report(day)
        # 只有 ICS 成功分配计次：12、22（联系人 42），32、42（联系人 99）。
        self.assertEqual((report["total_assignments"], report["users_total"], report["unidentified_assignments"]),
                         (4, 2, 0))
        self.assertEqual([(u["contact_id"], u["assignments"], u["conversations"]) for u in report["users"]],
                         [("42", 2, 2), ("99", 2, 2)])
        self.assertEqual((report["ranked_assignments"], report["ranked_share"]), (4, 100.0))
        # R13-14：与坐席细化发配量合计同一口径。
        agents = self.reports.agent_report(day, source_key=self.source, account_id=1, page_size=100,
                                           now=datetime.now(timezone.utc))
        self.assertEqual(sum(a["assigned_inquiries"] for a in agents["agents"]), report["total_assignments"])
        # 同一快照多次查询顺序一致；月、年视图同样的数字。
        for period in (ReportPeriod.build("month", date(2026, 10, 1)), ReportPeriod.build("year", date(2026, 1, 1))):
            again = self.report(period)
            self.assertEqual([u["contact_id"] for u in again["users"]], ["42", "99"])
        # 区间开始早于最早一条分配记录（同步开启前）时页面提示“该时间之前无成功转人工记录”。
        self.assertIsNotNone(report["earliest_assigned_at"])
        self.assertTrue(self.report(ReportPeriod.build("day", date(2026, 9, 30)))["before_earliest"])
        self.assertEqual(self.report(ReportPeriod.build("day", date(2026, 9, 30)))["users"], [])

        # 溯源明细条数 = 次数；每条有分配时间、会话/轮次、坐席。
        for user in report["users"]:
            detail = self.reports.user_detail(day, source_key=self.source, account_id=1, contact_id=user["contact_id"],
                                              now=datetime.now(timezone.utc))
            self.assertEqual(len(detail["items"]), user["assignments"])
            self.assertTrue(all(item["assigned_at"] and item["agent_id"] and item["round_no"] for item in detail["items"]))
            # R13-16：归档按客户 + 时间依据“ICS 成功分配时间” + 报表同一区间，命中轮次数一致。
            customer = user["customer_id"]
            filters = ArchiveFilters(source_key=self.source, account_id=1, customer_query=customer,
                                     time_field="assigned_at", start=day.start, end=day.end,
                                     as_of=datetime.now(timezone.utc))
            customers = self.reader.list_customers(filters)["items"]
            self.assertEqual([c["round_count"] for c in customers if c["customer_id"] == customer], [user["assignments"]])
        self.assertEqual(report["users"][1]["customer_id"], other)

    def test_time_refinement_and_export_follow_the_page(self):
        self.scenario()
        day = ReportPeriod.build("day", date(2026, 10, 1))
        # 分配时间（北京）：会话 32 19:20、42 20:10、22 21:00、12 22:40。只看 19:00–20:59（含）。
        window = day.refine(time(19, 0), time(20, 59))
        self.assertEqual(window.end - window.start, timedelta(hours=2))
        report = self.report(window)
        self.assertEqual([(u["contact_id"], u["assignments"]) for u in report["users"]], [("99", 2)])
        self.assertEqual((report["start_label"], report["end_label"]), ("2026-10-01 19:00:00", "2026-10-01 20:59:59"))
        full = self.report(day, full_ranking=True)
        self.assertNotIn("details", full)  # 导出不带逐条溯源明细（R13 导出只到排行）。
        self.assertEqual(sum(u["assignments"] for u in full["users"]), full["ranked_assignments"])
        from openpyxl import load_workbook
        buffer = BytesIO()
        build_user_workbook(buffer, full, channels=[("xingliao", "星聊")])
        book = load_workbook(BytesIO(buffer.getvalue()))
        from ics_agent.statistics.tests.test_statistics_export import section
        sheet = book["用户统计"]
        self.assertEqual(book.sheetnames, ["用户统计", "口径说明"])
        ranking = section(sheet, "用户成功转人工排行（前 100 名）")[1]
        self.assertEqual([row[2] for row in ranking[:2]], ["42", "99"])
        self.assertFalse(any(row[0] and "溯源明细" in str(row[0]) for row in sheet.iter_rows(values_only=True)))
        self.assertIn("2026-10-01（2026-10-01 00:00:00 至 23:59:59）", sheet["A2"].value)

    def test_day_rollups_give_the_live_ranking_and_are_read(self):
        """R2：已建日汇总（ops.stat_user_days）与现算排行逐字段一致，且整天确实读汇总。"""
        from quality.processing.rollups import RollupBuilder
        self.admin.execute("TRUNCATE ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days, ops.stat_round_days,"
                           " ops.stat_seat_repeats, ops.stat_user_days")
        self.scenario()
        periods = (ReportPeriod.build("day", date(2026, 10, 1)), ReportPeriod.build("month", date(2026, 10, 1)),
                   ReportPeriod.build("year", date(2026, 1, 1)),
                   ReportPeriod.build("custom", date(2026, 9, 30), date(2026, 10, 2)),
                   ReportPeriod.build("custom", date(2026, 9, 30), date(2026, 10, 2)).refine(time(12), time(20, 59)))
        keep = ("total_assignments", "unidentified_assignments", "users_total", "ranked_total", "ranked_assignments",
                "users")

        def snapshot():
            result = []
            for period in periods:
                for channel in (None, "unmapped"):
                    report = self.report(period, full_ranking=True, channel_key=channel)
                    result.append({key: report[key] for key in keep})
            return result

        live = snapshot()
        builder = RollupBuilder(self.processor.pool)
        for _ in range(400):
            if not builder.step():
                break
        built = {row["day"] for row in self.admin.execute("SELECT day FROM ops.stat_rollup_days WHERE state='built'")}
        self.assertIn(date(2026, 10, 1), built)
        self.assertTrue(self.admin.execute("SELECT count(*) AS n FROM ops.stat_user_days").fetchone()["n"])
        self.assertEqual(snapshot(), live)
        # The month really reads the stored day row.
        self.admin.execute("""UPDATE ops.stat_user_days SET assignments=assignments+10
            WHERE source_key=%s AND contact_id='99' AND day='2026-10-01'""", (self.source,))
        tampered = self.report(ReportPeriod.build("month", date(2026, 10, 1)))
        self.assertEqual({u["contact_id"]: u["assignments"] for u in tampered["users"]}["99"], 12)
        # Moving a round to another customer marks its assignment day dirty: computed live again.
        self.admin.execute("UPDATE ops.round_index SET customer_id=customer_id WHERE source_key=%s", (self.source,))
        first = self.admin.execute("SELECT customer_id FROM ops.customer_index WHERE source_key=%s AND contact_id='42'",
                                   (self.source,)).fetchone()["customer_id"]
        self.admin.execute("UPDATE ops.round_index SET customer_id=%s WHERE source_key=%s AND conversation_id=42",
                           (first, self.source))
        self.assertIn(date(2026, 10, 1), {row["day"] for row in self.admin.execute("SELECT day FROM ops.stat_dirty_days")})
        moved = self.report(ReportPeriod.build("month", date(2026, 10, 1)))
        self.assertEqual({u["contact_id"]: u["assignments"] for u in moved["users"]}, {"42": 3, "99": 1})

if __name__ == "__main__":
    unittest.main()
