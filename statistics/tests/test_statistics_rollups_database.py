"""Opt-in: day rollups (L3) give exactly the figures computed live from the per-round facts."""

from datetime import date, datetime, time as clock, timedelta, timezone
import os
import unittest

from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import StatisticsRepository
from ics_agent.statistics.tests import test_statistics_database as base
from quality.processing.rollups import RollupBuilder
from quality.tests.test_processing import START, agent_reply, event
from quality.tests import test_query_database as fixture

DAY = 24 * 60


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires isolated reporting test database")
class StatisticsRollupsDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        self.reports = StatisticsRepository(self.reader.pool)
        self.builder = RollupBuilder(self.processor.pool)
        # Other suites share the database: start from no rollups at all.
        self.admin.execute("TRUNCATE ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days, ops.stat_round_days,"
                           " ops.stat_seat_repeats, ops.stat_user_days")

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    assign_first_repliers = base.StatisticsDatabaseTest.assign_first_repliers
    refresh_facts = base.StatisticsDatabaseTest.refresh_facts
    review = base.StatisticsDatabaseTest.review

    def scenario(self):
        # START is Oct 1 16:00 Beijing time; minute 480 is Beijing midnight.
        # Conversation 12: seat 7 answers before and after midnight (one round
        # over two days); conversation 22 is lost; 32 is AI only.
        # Oct 3: seat 8 answers conversation 42, a low rating.
        self.receive(event(1, 460, content="我要转人工"), event(2, 461), agent_reply(3, 462), event(4, 470),
            agent_reply(5, 472), event(6, 478), agent_reply(7, 482),
            event(21, 100, conversation_id=22, content="我要转人工"),
            event(31, 200, conversation_id=32), event(32, 201, conversation_id=32, sender="agent_bot",
                                                      direction="outgoing"),
            event(41, 2 * DAY + 30, conversation_id=42, content="我要转人工"),
            event(42, 2 * DAY + 31, conversation_id=42), agent_reply(43, 2 * DAY + 33, conversation_id=42, agent_id=8),
            agent_reply(44, 2 * DAY + 34, conversation_id=42, agent_id=8))
        self.drain()
        self.assign_first_repliers()
        self.admin.execute("""UPDATE ops.round_index SET data=data || jsonb_build_object('responsibility_basis',
            'ics_assignment','responsible_agent_id','7','responsible_agent_name','Seat 7',
            'responsible_assigned_at',data->'handoff_requested_at') WHERE source_key=%s AND conversation_id=22""",
            (self.source,))
        self.admin.execute("UPDATE ops.round_index SET rating=2,rating_status='rated' WHERE source_key=%s AND conversation_id=42",
                           (self.source,))
        self.refresh_facts()
        self.review(12, "failed", "failed")

    def build_all(self, now):
        for _ in range(400):
            if not self.builder.step(now=now):
                return
        self.fail("rollups did not finish")

    def reports_for(self, now):
        result = []
        for period in (ReportPeriod.build("day", date(2026, 10, 1)), ReportPeriod.build("day", date(2026, 10, 2)),
                       ReportPeriod.build("month", date(2026, 10, 1)), ReportPeriod.build("year", date(2026, 10, 1)),
                       ReportPeriod.build("custom", date(2026, 10, 1), date(2026, 10, 3)),
                       # R10 二级时刻：首尾两天只算部分，中间整天可读日汇总。
                       ReportPeriod.build("custom", date(2026, 10, 1), date(2026, 10, 3)).refine(clock(20), clock(15, 59)),
                       ReportPeriod.build("day", date(2026, 10, 2)).refine(clock(0), clock(0, 59))):
            scope = {"source_key": self.source, "account_id": 1, "now": now}
            agents = self.reports.agent_report(period, **scope)
            quality = self.reports.quality_report(period, **scope)
            time = self.reports.time_report(period, **scope)
            keep = ("agents", "total", "total_inquiries", "total_replies", "total_lost_inquiries",
                    "unassigned_lost_inquiries", "unassigned_inquiries", "assisted_inquiries",
                    "total_failed_inquiries", "ai", "rows", "totals", "channels")
            strip = lambda report: {k: v for k, v in report.items() if k in keep}
            result.append((strip(agents), strip(quality), strip(time)))
        return result

    def built_days(self):
        return {row["day"] for row in self.admin.execute(
            "SELECT day FROM ops.stat_rollup_days WHERE state='built'").fetchall()}

    def test_rollups_equal_live_figures_for_days_months_years_and_custom_periods(self):
        self.scenario()
        live = self.reports_for(datetime.now(timezone.utc))
        self.build_all(None)
        now = datetime.now(timezone.utc)
        self.assertTrue({date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)} <= self.built_days())
        self.assertEqual(self.reports_for(now), live)
        month = live[2][0]
        seats = {a["agent_id"]: a for a in month["agents"]}
        # The round answered on two days counts once in the month.
        self.assertEqual((seats["7"]["inquiries"], seats["7"]["replies"]), (1, 3))
        self.assertEqual((seats["7"]["lost_inquiries"], seats["8"]["low_rating"]), (1, 1))
        days = [live[0][0], live[1][0]]
        self.assertEqual([{a["agent_id"]: a["inquiries"] for a in d["agents"]}.get("7") for d in days], [1, 1])
        # The monthly report really reads the stored day rows (minus repeats) of the rollups.
        self.admin.execute("""UPDATE ops.stat_seat_days SET inquiries=inquiries+100
            WHERE source_key=%s AND agent_id='7' AND variant='d' AND day='2026-10-01'""", (self.source,))
        tampered = self.reports.agent_report(ReportPeriod.build("month", date(2026, 10, 1)), source_key=self.source,
                                             account_id=1, now=now)
        self.assertEqual({a["agent_id"]: a for a in tampered["agents"]}["7"]["inquiries"], 101)
        # The round replied on Oct 1 and Oct 2 is a stored repeat on Oct 2, subtracted once.
        repeats = self.admin.execute("""SELECT day,inquiries FROM ops.stat_seat_repeats
            WHERE source_key=%s AND agent_id='7'""", (self.source,)).fetchall()
        self.assertEqual([(row["day"], row["inquiries"]) for row in repeats], [(date(2026, 10, 2), 1)])

    def test_refined_windows_compute_partial_edge_days_live(self):
        self.scenario()
        live = self.reports_for(datetime.now(timezone.utc))
        refined = {a["agent_id"]: a for a in live[5][0]["agents"]}
        # Oct 1 20:00 - Oct 3 15:59:59 keeps conversation 12 and excludes conversation 42 (Oct 3 16:30).
        self.assertEqual(refined["7"]["inquiries"], 1)
        self.assertFalse(refined.get("8", {}).get("inquiries"))
        self.assertEqual(live[5][2]["rows"][0]["key"], "2026-10-01T00:00:00+08:00")
        self.build_all(None)
        now = datetime.now(timezone.utc)
        # Edge-day rollups are never read for a refined window, even if they say otherwise.
        self.admin.execute("""UPDATE ops.stat_seat_days SET inquiries=inquiries+100
            WHERE source_key=%s AND day IN ('2026-10-01','2026-10-03')""", (self.source,))
        self.admin.execute("""UPDATE ops.stat_round_days SET incoming=incoming+100
            WHERE source_key=%s AND day IN ('2026-10-01','2026-10-03')""", (self.source,))
        again = self.reports_for(now)
        self.assertEqual(again[5], live[5])
        self.assertEqual(again[6], live[6])

    def test_changes_mark_built_days_dirty_and_reports_stay_exact(self):
        self.scenario()
        self.build_all(None)
        self.assertFalse(self.admin.execute("SELECT count(*) AS n FROM ops.stat_dirty_days").fetchone()["n"])
        # A late rating and a new review change Oct 1 and Oct 3 figures.
        self.admin.execute("UPDATE ops.round_index SET rating=1,rating_status='rated' WHERE source_key=%s AND conversation_id=12",
                           (self.source,))
        self.refresh_facts()
        self.review(42, "passed", "failed")
        dirty = {row["day"] for row in self.admin.execute("SELECT day FROM ops.stat_dirty_days").fetchall()}
        self.assertTrue({date(2026, 10, 1), date(2026, 10, 3)} <= dirty)
        now = datetime.now(timezone.utc)
        expected = self.reports_for(now)  # dirty days are computed live
        seats = {a["agent_id"]: a for a in expected[2][1]["agents"]}
        self.assertEqual((seats["7"]["low_rating"], seats["8"]["failed_inquiries"]), (1, 1))
        self.build_all(None)
        self.assertFalse(self.admin.execute("SELECT count(*) AS n FROM ops.stat_dirty_days").fetchone()["n"])
        self.assertEqual(self.reports_for(datetime.now(timezone.utc)), expected)

    def test_reports_before_a_days_build_time_do_not_use_its_rollup(self):
        self.scenario()
        early = START + timedelta(days=1, minutes=6)  # Oct 2, before the second-day reply
        expected = self.reports_for(early)
        self.build_all(None)
        self.assertEqual(self.reports_for(early), expected)

    def test_rating_review_during_rollup_build_keeps_day_dirty_until_rebuilt(self):
        """构建读过旧评分后提交复核，旧汇总不得被误认为最新。"""
        from unittest.mock import patch
        from quality.processing import rollups
        self.scenario()
        self.build_all(None)
        before = self.reports_for(datetime.now(timezone.utc))
        round_id = self.admin.execute("SELECT round_id FROM ops.round_index WHERE source_key=%s AND conversation_id=42",
                                      (self.source,)).fetchone()["round_id"]
        original = rollups.run_family
        changed = False
        def interleaved(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                current = self.reader.rating_review(round_id)
                self.reader.save_rating_review(round_id, "invalid", "admin", int(current["revision"]), current["content_revision"])
            return result
        with patch.object(rollups, "run_family", side_effect=interleaved):
            self.builder.build(date(2026, 10, 3))
        self.assertTrue(self.admin.execute("SELECT 1 FROM ops.stat_dirty_days WHERE day='2026-10-03'").fetchone())
        expected = self.reports_for(datetime.now(timezone.utc))
        before_seat = next(row for row in before[2][0]["agents"] if row["agent_id"] == "8")
        after_seat = next(row for row in expected[2][0]["agents"] if row["agent_id"] == "8")
        self.assertEqual((after_seat["rated_inquiries"], after_seat["low_rating"], after_seat["average_rating"]), (0, 0, None))
        for key in before_seat:
            if key not in ("rated_inquiries", "low_rating", "average_rating"):
                self.assertEqual(before_seat[key], after_seat[key], key)
        self.builder.build(date(2026, 10, 3))
        self.assertEqual(self.reports_for(datetime.now(timezone.utc)), expected)
        saved = self.reader.rating_review(round_id)
        self.reader.save_rating_review(round_id, "valid", "admin", int(saved["revision"]), saved["content_revision"])
        self.builder.build(date(2026, 10, 3))
        self.assertEqual(self.reports_for(datetime.now(timezone.utc)), before)


if __name__ == "__main__":
    unittest.main()
