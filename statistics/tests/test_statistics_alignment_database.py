"""Opt-in: every statistics report figure equals the rounds the archive lists for it.

For each count of the time, seat and quality reports, the archive drill-down
(ArchiveFilters.metric + window + credited seat + locked time) must return
exactly that many rounds -- with stored facts, with every round computed live,
and with day rollups built. This is the guarantee that report figures can be
traced back to the original conversations.
"""

from datetime import date, datetime, timedelta, timezone
import os
import unittest

from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import BUSY_NOTICE, StatisticsRepository
from ics_agent.statistics.tests import test_statistics_database as base
from quality.processing.rollups import RollupBuilder
from quality.query.repository import ArchiveFilters
from quality.tests.test_processing import START, agent_reply, event
from quality.tests import test_query_database as fixture

SEAT_COUNTS = {  # report field -> registry metric, all counted in rounds
    "inquiries": "inquiries", "dialogues": "dialogues", "high_quality_dialogues": "high_quality_dialogues",
    "low_rating": "low_rating", "rated_inquiries": "rated_inquiries", "assigned_inquiries": "assigned_inquiries",
    "lost_inquiries": "lost_inquiries", "first_response_samples": "first_response_count",
    "response_rounds": "response_rounds",
}
TIME_COUNTS = {"incoming": "incoming", "ai_only": "ai_only", "handoff": "handoff", "rejected": "rejected",
               "effective_human": "effective_human", "low_rating": "low_rating_rounds"}
AI_COUNTS = {"total_rounds": "incoming", "handoff": "handoff", "ai_only": "ai_replied_only",
             "handoff_failed": "rejected", "failed_rounds": "ai_failed"}


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires isolated reporting test database")
class StatisticsArchiveAlignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        self.reports = StatisticsRepository(self.reader.pool)
        self.admin.execute("TRUNCATE ops.stat_rollup_days, ops.stat_dirty_days, ops.stat_seat_days, ops.stat_round_days,"
                           " ops.stat_seat_repeats, ops.stat_user_days")

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    assign_first_repliers = base.StatisticsDatabaseTest.assign_first_repliers
    refresh_facts = base.StatisticsDatabaseTest.refresh_facts
    review = base.StatisticsDatabaseTest.review

    def scenario(self):
        # Minute 480 is Beijing midnight between Oct 1 and Oct 2.
        self.receive(
            # 12: seat 7, answered across midnight, low rating, human review failed.
            event(1, 400, content="我要转人工"), event(2, 401), agent_reply(3, 402), event(4, 470),
            agent_reply(5, 475), event(6, 478), agent_reply(7, 485),
            # 22: assigned to 7 but seat 8 helps -> assisted.
            event(11, 300, conversation_id=22, content="我要转人工"), event(12, 301, conversation_id=22),
            agent_reply(13, 302, conversation_id=22), agent_reply(14, 303, conversation_id=22, agent_id=8),
            # 32: seat 8 alone, six visitor messages (high quality dialogue).
            event(21, 200, conversation_id=32, content="我要转人工"),
            *[event(22 + n, 201 + n, conversation_id=32) for n in range(6)],
            agent_reply(30, 210, conversation_id=32, agent_id=8),
            # 42: lost transfer assigned to seat 8.
            event(41, 250, conversation_id=42, content="我要转人工"),
            # 52: answered without any ICS assignment (manual Chatwoot assignment).
            event(51, 260, conversation_id=52), agent_reply(52, 262, conversation_id=52, agent_id=9),
            # 62: AI only with a busy refusal; 72: AI only.
            event(61, 100, conversation_id=62, content="我要转人工"),
            event(62, 101, conversation_id=62, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(71, 120, conversation_id=72), event(72, 121, conversation_id=72, sender="agent_bot", direction="outgoing"))
        self.drain()
        self.assign_first_repliers(12, 22, 32)
        for conversation, seat in ((42, "8"),):
            self.admin.execute("""UPDATE ops.round_index SET data=data || jsonb_build_object('responsibility_basis',
                'ics_assignment','responsible_agent_id',%s::text,'responsible_agent_name','Seat ' || %s::text,
                'responsible_assigned_at',data->'handoff_requested_at') WHERE source_key=%s AND conversation_id=%s""",
                (seat, seat, self.source, conversation))
        self.admin.execute("UPDATE ops.round_index SET rating=2,rating_status='rated' WHERE source_key=%s AND conversation_id=12",
                           (self.source,))
        self.refresh_facts()
        self.review(12, "passed", "failed")
        self.review(72, "failed", "pending")

    def archive_count(self, metric, period, now, seat=None, start=None, end=None):
        filters = ArchiveFilters(source_key=self.source, account_id=1, metric=metric,
                                 metric_seat=seat, start=start or period.start, end=end or period.end, as_of=now)
        return self.reader.list_rounds(filters, page_size=1)["total"]

    def check(self, period, now):
        scope = {"source_key": self.source, "account_id": 1, "now": now}
        agents = self.reports.agent_report(period, page_size=100, **scope)
        quality = self.reports.quality_report(period, page_size=100, **scope)
        times = self.reports.time_report(period, **scope)
        checked = 0
        for seat in agents["agents"]:
            for field, metric in SEAT_COUNTS.items():
                with self.subTest(period=period.mode, seat=seat["agent_id"], field=field):
                    self.assertEqual(self.archive_count(metric, period, now, seat["agent_id"]), seat[field])
                    checked += 1
            for bucket in period.buckets:
                value = seat["buckets"].get(bucket["key"], 0)
                with self.subTest(period=period.mode, seat=seat["agent_id"], bucket=bucket["key"]):
                    self.assertEqual(self.archive_count("inquiries", period, now, seat["agent_id"],
                                                        bucket["start"], bucket["end"]), value)
        for seat in quality["agents"]:
            with self.subTest(period=period.mode, seat=seat["agent_id"], field="failed_inquiries"):
                self.assertEqual(self.archive_count("failed_inquiries", period, now, seat["agent_id"]),
                                 seat["failed_inquiries"])
        with self.subTest(period=period.mode, field="total_failed_inquiries"):
            self.assertEqual(self.archive_count("failed_inquiries", period, now, "*"), quality["total_failed_inquiries"])
        for field, metric, seat in (("total_inquiries", "inquiries", "*"),
                                    ("unassigned_inquiries", "unassigned_inquiries", None),
                                    ("assisted_inquiries", "assisted_inquiries", None),
                                    ("total_lost_inquiries", "lost_inquiries", None),
                                    ("unassigned_lost_inquiries", "lost_inquiries", "")):
            with self.subTest(period=period.mode, field=field):
                self.assertEqual(self.archive_count(metric, period, now, seat), agents[field])
        for field, metric in TIME_COUNTS.items():
            with self.subTest(period=period.mode, field=field):
                self.assertEqual(self.archive_count(metric, period, now), times["totals"][field])
        for field, metric in AI_COUNTS.items():
            with self.subTest(period=period.mode, field=field):
                self.assertEqual(self.archive_count(metric, period, now), quality["ai"][field])
        return agents, quality, times, checked

    def periods(self):
        return (ReportPeriod.build("day", date(2026, 10, 1)), ReportPeriod.build("day", date(2026, 10, 2)),
                ReportPeriod.build("month", date(2026, 10, 1)), ReportPeriod.build("year", date(2026, 10, 1)),
                ReportPeriod.build("custom", date(2026, 10, 1), date(2026, 10, 2)))

    def test_every_report_count_equals_its_archive_rounds_stored_live_and_rolled_up(self):
        self.scenario()
        now = datetime.now(timezone.utc)
        agents, quality, times, checked = self.check(self.periods()[2], now)
        self.assertGreater(checked, 15)
        seats = {a["agent_id"]: a for a in agents["agents"]}
        # The scenario really covers the attribution rules.
        self.assertEqual(set(seats), {"7", "8"})
        self.assertEqual((seats["7"]["inquiries"], seats["7"]["assigned_inquiries"], seats["7"]["low_rating"]), (1, 2, 1))
        self.assertEqual((seats["8"]["high_quality_dialogues"], seats["8"]["lost_inquiries"]), (1, 1))
        self.assertEqual((agents["unassigned_inquiries"], agents["assisted_inquiries"]), (1, 1))
        self.assertEqual((times["totals"]["rejected"], quality["ai"]["failed_rounds"]), (1, 1))
        for period in self.periods():
            self.check(period, now)
        # Every round computed live (outdated rule version) gives the same answers.
        self.admin.execute("UPDATE ops.round_service_facts SET fact_version=0 WHERE source_key=%s", (self.source,))
        for period in self.periods():
            self.check(period, now)
        # With day rollups built (reports read them for whole days/months/years).
        self.refresh_facts()
        builder = RollupBuilder(self.processor.pool)
        for _ in range(400):
            if not builder.step():
                break
        later = datetime.now(timezone.utc)
        for period in self.periods():
            self.check(period, later)

    def test_archive_locked_time_matches_a_report_of_that_moment(self):
        self.scenario()
        # Before seat 7 answered after midnight and before seat 8 answered conversation 32.
        moment = START + timedelta(minutes=205)
        period = ReportPeriod.build("day", date(2026, 10, 1))
        agents, _, _, _ = self.check(period, moment)
        seats = {a["agent_id"]: a for a in agents["agents"]}
        # Conversation 32 was still waiting at that moment: a lost transfer of seat 8.
        self.assertEqual(seats["8"]["lost_inquiries"], 1)
        detail = self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1, metric="lost_inquiries",
                                                        metric_seat="8", start=period.start, end=period.end,
                                                        as_of=moment))["items"][0]
        self.assertEqual(detail["conversation_id"], 32)
        # Its chat content is locked to that moment as well.
        messages = self.reader.round_detail(detail["round_id"], as_of=moment)["messages"]
        self.assertTrue(messages and all(m["sent_at"] < moment for m in messages if m["sent_at"]))
        self.assertFalse(any(m["sender_role"] == "agent" for m in messages))


if __name__ == "__main__":
    unittest.main()
