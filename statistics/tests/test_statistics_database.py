"""Opt-in checks on a disposable quality_query_test_* database, never a live archive."""

from datetime import date, datetime, timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from psycopg.types.json import Jsonb

from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import BUSY_NOTICE, StatisticsRepository
from quality.presence.repository import PresenceRepository
from quality.tests.test_processing import START, agent_reply, event
from quality.tests import test_query_database as fixture


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires isolated reporting test database")
class StatisticsDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        self.reports = StatisticsRepository(self.reader.pool)
        self.presence = PresenceRepository(SimpleNamespace(connection_kwargs={**self.settings.connection_kwargs,
            "user": "archive_presence", "password": os.environ["ARCHIVE_PRESENCE_PASSWORD"]}))
        self.presence.open()
        self.addCleanup(self.presence.close)
        self.period = ReportPeriod.build("day", date(2026, 10, 1))

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    receive_assignment = fixture.QueryDatabaseTest.receive_assignment

    def assign_first_repliers(self, *conversations):
        """Record an ICS assignment for rounds that have none: the round's first public replier.

        Since FACT_VERSION 7 only ICS-assigned seats are credited with any round;
        these scenarios describe rounds a seat was assigned to and answered. The
        assignment time is the observed transfer request (none without one), so
        the transfer clock of every round stays unchanged.
        """
        scope = "AND r.conversation_id = ANY(%s)" if conversations else ""
        self.admin.execute(f"""
            UPDATE ops.round_index r SET data = r.data || jsonb_build_object(
                'responsibility_basis','ics_assignment','responsible_agent_id',first.sender_id,
                'responsible_agent_name',first.sender_name,
                'responsible_assigned_at',r.data->'handoff_requested_at')
            FROM (SELECT DISTINCT ON (m.round_id) m.round_id,m.sender_id,m.sender_name FROM ops.message_index m
                WHERE m.source_key=%s AND m.sender_role='agent' AND m.direction='outgoing'
                    AND NOT m.is_private AND COALESCE(m.data->>'is_rating','false') <> 'true'
                    AND m.sender_id IS NOT NULL AND NOT m.is_deleted
                ORDER BY m.round_id,m.sent_at NULLS LAST,m.message_id) first
            WHERE first.round_id=r.round_id AND r.source_key=%s
                AND r.data->>'responsibility_basis' IS DISTINCT FROM 'ics_assignment' {scope}
        """, [self.source, self.source, *([list(conversations)] if conversations else [])])
        self.refresh_facts()

    def refresh_facts(self):
        from quality.processing.service_facts import store
        ids = [row["round_id"] for row in self.admin.execute(
            "SELECT round_id FROM ops.round_index WHERE source_key=%s", (self.source,)).fetchall()]
        with self.admin.transaction():
            store(self.admin, ids)

    def round_data(self, conversation, **values):
        self.admin.execute("UPDATE ops.round_index SET data=data || %s WHERE source_key=%s AND conversation_id=%s",
            (Jsonb(values), self.source, conversation))

    def seats(self, **kwargs):
        report = self.reports.agent_report(kwargs.pop("period", self.period), source_key=self.source,
            account_id=1, now=kwargs.pop("now", START + timedelta(days=1)), **kwargs)
        return report, {a["agent_id"]: a for a in report["agents"]}

    def quality(self, **kwargs):
        report = self.reports.quality_report(kwargs.pop("period", self.period), source_key=self.source,
            account_id=1, now=kwargs.pop("now", START + timedelta(days=1)), **kwargs)
        return report, {a["agent_id"]: a for a in report["agents"]}

    def review(self, conversation, ai, human, revision=0):
        row = self.admin.execute("SELECT round_id,reception_type FROM ops.round_index WHERE source_key=%s AND conversation_id=%s ORDER BY round_no DESC LIMIT 1",
            (self.source, conversation)).fetchone()
        # R9 (migration 019): AI-only rounds take no human verdict.
        human = None if row["reception_type"] == "ai_only" else human
        return self.reader.save_round_review(row["round_id"], ai, human, "统计口径检查", "test-admin", revision)

    def sample(self, minute, agents, account=1):
        self.presence.append({"status": "ready", "source_key": self.source, "account_id": account,
            "sampled_at": (START + timedelta(minutes=minute, seconds=10)).isoformat(),
            "agents": [{"agent_id": str(agent), "agent_name": f"Seat {agent}", "agent_name_source": "chatwoot",
                        "availability_status": status} for agent, status in agents]})

    def test_excel_exports_full_filtered_roster_and_current_quality(self):
        from openpyxl import load_workbook
        from ics_agent.statistics import export
        from ics_agent.statistics.tests.test_statistics_export import section
        from quality.query.config import QuerySettings

        export_settings = QuerySettings(_env_file=None, reader_password="unused-export-test")
        self.sample(0, [(agent, "online") for agent in range(7, 30)])
        self.sample(0, [(999, "online")], account=2)
        self.receive(event(1, content="我要转人工"), event(2, 1, content="请帮我查一下"), agent_reply(3, 2),
            event(11, 1440, conversation_id=22),
            event(12, 1441, conversation_id=22, sender="agent_bot", direction="outgoing", content="AI回复"),
            event(21, 2880, conversation_id=32))
        self.drain()
        self.assign_first_repliers()
        self.review(12, "failed", "failed")
        period = ReportPeriod.build("custom", date(2026, 10, 1), date(2026, 10, 2))
        clock = Mock(wraps=datetime)
        clock.now.return_value = START + timedelta(days=3)
        for kind in ("time", "agents", "quality"):
            with patch.object(export, "datetime", clock):
                artifact = export.create_export(self.reader.pool, export_settings, kind, period,
                    source_key=self.source, account_id=1)
            try:
                book = load_workbook(artifact.path)
                self.assertEqual(book.sheetnames, [export.TITLES[kind], *(export.AGENT_SHEETS if kind == "agents" else ()), "口径说明"])
                sheet = book[export.TITLES[kind]]
                if kind == "time":
                    _, body = section(sheet, "时段数据")
                    self.assertEqual([row[1] for row in body], [1, 1, 2])
                else:
                    headers, body = section(sheet, "客服回复质量" if kind == "agents" else "人工质检表")
                    seats = [row for row in body if row[0] != "合计"]
                    self.assertEqual(len(seats), 23)
                    self.assertTrue(all(row[1].startswith(f"{self.source} / 1 / #") for row in seats))
                    self.assertNotIn(f"{self.source} / 2 / #999", [row[1] for row in seats])
                    self.assertEqual(seats[0][headers.index("回复询盘总量")], 1)
                    if kind == "agents":
                        self.assertEqual(seats[0][headers.index("首响时长")], timedelta(seconds=60))
                        self.assertEqual(seats[0][headers.index("平均响应时长")], timedelta(seconds=60))
                        self.assertEqual(len(section(book["坐席回复询盘"], "坐席回复询盘（按实际回复时间归入时段）")[1]), 24)
                    else:
                        self.assertEqual(seats[0][headers.index("质检不合格量")], 1)
                        self.assertEqual(seats[0][headers.index("不合格率")], 1)
                        ai = section(sheet, "AI质检报表（按轮次开始时间统计全部对话）")[1][0]
                        self.assertEqual(ai[2:7], [2, 1, 1, 0, 1])
                        self.assertAlmostEqual(ai[7], 0.5)
                book.close()
            finally:
                artifact.cleanup()
        with patch.object(export, "datetime", clock):
            artifact = export.create_export(self.reader.pool, export_settings, "time", period,
                source_key=self.source, account_id=1, channel_key="wechat")
        try:
            book = load_workbook(artifact.path)
            self.assertEqual(section(book["时间统计"], "时段数据")[1][-1][:2], ["合计", 0])
            self.assertIn("渠道：wechat", book["时间统计"]["A2"].value)
            book.close()
        finally:
            artifact.cleanup()

    def test_round_metrics_deduplicate_notices_messages_and_preserve_calendar_boundary(self):
        self.receive(event(1, content="我要转人工"), event(2, 1, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(3, 2, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE), agent_reply(4, 3),
            agent_reply(5, 4), agent_reply(6, 5, private=True),
            event(11, conversation_id=22), event(12, 1, conversation_id=22, sender="agent_bot", direction="outgoing"),
            event(21, conversation_id=32, content="我要转人工"), event(31, 1440, conversation_id=42))
        self.drain()
        self.admin.execute("UPDATE ops.round_index SET rating=3,rating_status='rated' WHERE source_key=%s AND conversation_id=12", (self.source,))
        report = self.reports.time_report(self.period, source_key=self.source, account_id=1, now=START + timedelta(days=2))
        self.assertEqual(report["totals"], {"incoming": 3, "ai_only": 1, "handoff": 2, "rejected": 1, "effective_human": 1, "low_rating": 1})
        self.assertEqual(report["rows"][16]["incoming"], 3)
        self.assertEqual(sum(r["incoming"] for r in report["rows"]), 3)
        self.assertEqual(sum(report["channels"].values()), 3)
        month = self.reports.time_report(ReportPeriod.build("month", date(2026, 10, 20)), source_key=self.source, account_id=1, now=START + timedelta(days=2))
        self.assertEqual([r["incoming"] for r in month["rows"][:2]], [3, 1])
        filtered = self.reports.time_report(self.period, source_key=self.source, account_id=1, channel_key="wechat", now=START + timedelta(days=2))
        self.assertEqual(filtered["totals"]["incoming"], 0)

    def test_seat_inquiries_deduplicate_within_period_but_attribute_actual_reply_hour(self):
        self.receive(event(1), agent_reply(2, 1), agent_reply(3, 2), agent_reply(4, 61),
            agent_reply(5, 3, private=True, agent_id=99), agent_reply(6, 4, content_type="input_csat", agent_id=99))
        self.drain()
        self.assign_first_repliers()
        report = self.reports.agent_report(self.period, source_key=self.source, account_id=1, now=START + timedelta(days=1))
        seats = {row["agent_id"]: row for row in report["agents"]}
        # Seat 99's private note and rating card are not public replies.
        self.assertEqual(set(seats), {"7"})
        self.assertEqual(seats["7"]["inquiries"], 1)
        self.assertEqual(seats["7"]["replies"], 3)
        self.assertEqual(seats["7"]["buckets"][self.period.buckets[16]["key"]], 1)
        self.assertEqual(seats["7"]["buckets"][self.period.buckets[17]["key"]], 1)
        self.assertEqual(report["total_inquiries"], 1)
        # The same ID in another source must not leak into the scoped report.
        empty = self.reports.agent_report(self.period, source_key=self.source + "-absent", account_id=1, now=START + timedelta(days=1))
        self.assertEqual(empty["total"], 0)

    def test_presence_observed_zero_gap_partial_coverage_and_scope(self):
        self.sample(0, [(7, "online"), (8, "offline")])
        self.sample(2, [(7, "busy"), (8, "offline")])
        self.sample(0, [(7, "online")], account=2)
        now = START + timedelta(minutes=3, seconds=20)
        report = self.reports.agent_report(self.period, source_key=self.source, account_id=1, now=now)
        points = report["presence_points"]
        self.assertEqual([p["online"] for p in points[960:963]], [1, None, 0])
        self.assertAlmostEqual(report["presence_rows"][16]["coverage"], 66.7)
        self.assertEqual(report["presence_rows"][16]["status"], "partial")
        self.assertEqual(report["presence_rows"][16]["online"], .5)
        self.assertEqual(report["total"], 2)
        self.assertTrue(all(a["account_id"] == 1 for a in report["agents"]))
        month = self.reports.agent_report(ReportPeriod.build("month", date(2026, 10, 1)), source_key=self.source, account_id=1, now=now)
        year = self.reports.agent_report(ReportPeriod.build("year", date(2026, 10, 1)), source_key=self.source, account_id=1, now=now)
        self.assertEqual(month["presence_rows"][0]["online"], .5)
        self.assertEqual(year["presence_rows"][9]["online"], .5)

    def test_all_seats_are_available_through_pagination(self):
        self.sample(0, [(agent, "offline") for agent in range(1, 36)])
        first = self.reports.agent_report(self.period, source_key=self.source, account_id=1, now=START + timedelta(minutes=1), page_size=20)
        second = self.reports.agent_report(self.period, source_key=self.source, account_id=1, now=START + timedelta(minutes=1), page=2, page_size=20)
        self.assertEqual(first["total"], 35)
        self.assertEqual([len(first["agents"]), len(second["agents"])], [20, 15])
        self.assertEqual(len({a["agent_id"] for a in first["agents"] + second["agents"]}), 35)

    def test_unprocessed_archive_jobs_are_reported_before_projection(self):
        before = self.reports.time_report(self.period, source_key=self.source, account_id=1)["processing"]["pending_jobs"]
        self.receive(event(1))
        result = self.reports.time_report(self.period, source_key=self.source, account_id=1)
        self.assertEqual(result["totals"]["incoming"], 0)
        self.assertGreaterEqual(result["processing"]["pending_jobs"], before + 1)
        self.drain()

    def test_quality_averages_only_completed_customer_waits_and_deduplicates_ratings(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 3), event(3, 4), event(4, 5),
            event(101, .5), event(102, 1),
            agent_reply(5, 7), agent_reply(6, 7.5), event(7, 8), event(8, 9),
            agent_reply(9, 10, private=True, agent_id=99),
            agent_reply(10, 11, content_type="input_csat", agent_id=99),
            event(21, 10, conversation_id=22, content="我要转人工"),
            event(103, 10.5, conversation_id=22), agent_reply(22, 11, conversation_id=22))
        self.drain()
        self.assign_first_repliers()
        self.admin.execute("""UPDATE ops.round_index SET rating=CASE conversation_id WHEN 12 THEN 3 ELSE 5 END,
            rating_status='rated' WHERE source_key=%s""", (self.source,))
        report, seats = self.seats()
        self.assertEqual(set(seats), {"7"})
        a = seats["7"]
        # R3: every wait starts at the customer's last message. Round 12 waits
        # 120s (1 -> 3) and 120s (5 -> 7), round 22 30s (10.5 -> 11). First
        # responses (120 + 30) / 2; the average is weighted by waits, not rounds:
        # (120 + 120 + 30) / 3.
        self.assertEqual((a["first_response_seconds"], a["first_response_samples"]), (75, 2))
        self.assertEqual((a["average_response_seconds"], a["response_samples"], a["response_rounds"]), (90, 3, 2))
        self.assertEqual((a["inquiries"], a["rated_inquiries"], a["average_rating"], a["low_rating"]), (2, 2, 4, 1))
        self.assertEqual(report["total_lost_inquiries"], 0)

    def test_round_answered_by_a_colleague_of_the_assigned_seat_counts_for_nobody(self):
        # Seat 8 is assigned by ICS but never replies; seat 7 covers (shift handover).
        self.receive(event(1, content="我要转人工"), event(2, 2), agent_reply(3, 3, agent_id=7),
            event(4, 4), agent_reply(5, 6, agent_id=7), *[event(10 + n, 7 + n) for n in range(6)])
        self.receive_assignment("8", trigger_message_id=1, minute=1, business_round="helped-round")
        self.drain()
        self.review(12, "failed", "failed")
        self.admin.execute("UPDATE ops.round_index SET rating=2,rating_status='rated' WHERE source_key=%s", (self.source,))
        self.refresh_facts()
        for attempt in ("stored", "live"):
            report, seats = self.quality()
            # Only the successful assignment itself still counts.
            self.assertEqual(set(seats), {"8"}, attempt)
            a = seats["8"]
            self.assertEqual((a["assigned_inquiries"], a["inquiries"], a["replies"]), (1, 0, 0), attempt)
            self.assertEqual((a["dialogues"], a["high_quality_dialogues"]), (0, 0), attempt)
            self.assertEqual((a["average_rating"], a["low_rating"], a["failed_inquiries"]), (None, 0, 0), attempt)
            self.assertEqual((a["consultation_seconds"], a["first_response_seconds"], a["response_samples"]),
                (0, None, 0), attempt)
            self.assertEqual((report["total_inquiries"], report["total_failed_inquiries"]), (0, 0), attempt)
            self.admin.execute("UPDATE ops.round_service_facts SET fact_version=0 WHERE source_key=%s", (self.source,))
        self.refresh_facts()
        stored = self.admin.execute("""SELECT f.responsible_agent_id FROM ops.round_service_facts f
            JOIN ops.round_index r USING(round_id) WHERE r.source_key=%s""", (self.source,)).fetchall()
        self.assertEqual([row["responsible_agent_id"] for row in stored], [None])

    def test_assisted_round_does_not_affect_the_customers_next_round(self):
        # Round 1: assigned seat 8, colleague 7 answers -> nobody. The round is
        # resolved; in round 2 seat 7 alone answers and is assessed normally.
        self.receive(event(1, content="我要转人工"), event(2, 2), agent_reply(3, 3, agent_id=7),
            event(4, 4, event_type="conversation_status_changed", status="resolved"),
            event(5, 10, content="还有一个问题"), agent_reply(6, 11, agent_id=7), event(7, 12), agent_reply(8, 15, agent_id=7))
        self.receive_assignment("8", trigger_message_id=1, minute=1, business_round="handover")
        self.drain()
        self.assign_first_repliers()
        rounds = self.admin.execute("SELECT count(*) AS n FROM ops.round_index WHERE source_key=%s", (self.source,)).fetchone()
        self.assertEqual(rounds["n"], 2)
        for attempt in ("stored", "live"):
            _, seats = self.seats()
            self.assertEqual((seats["8"]["assigned_inquiries"], seats["8"]["inquiries"]), (1, 0), attempt)
            self.assertEqual((seats["7"]["inquiries"], seats["7"]["replies"], seats["7"]["consultation_seconds"]),
                (1, 2, 4 * 60), attempt)
            self.admin.execute("UPDATE ops.round_service_facts SET fact_version=0 WHERE source_key=%s", (self.source,))

    def test_reply_timings_start_after_successful_assignment_not_busy_refusal(self):
        self.receive(event(1, content="我要转人工"),
            event(2, 1, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(3, 2, content="我要转人工"), event(4, 3), agent_reply(5, 4, agent_id=8))
        self.receive_assignment("8", trigger_message_id=3, minute=2, business_round="retry-success", session_minute=2)
        self.drain()
        _, seats = self.seats()
        self.assertEqual((seats["8"]["first_response_seconds"], seats["8"]["average_response_seconds"]), (60, 60))
        self.assertEqual((seats["8"]["first_response_samples"], seats["8"]["response_samples"]), (1, 1))

    def test_response_limit_excludes_samples_before_wait_weighted_averages(self):
        # Round 12: first wait 601s is excluded, later waits 30s and 90s remain.
        # Round 22 retains exactly 600s. Round 32 has no valid samples and adds
        # nothing. Report mean = (30 + 90 + 600) / 3 waits (R3, not per round).
        self.receive(event(1, content="我要转人工"), event(2, 1),
            agent_reply(3, 1 + 601 / 60), event(4, 12), agent_reply(5, 12.5),
            event(6, 13), agent_reply(7, 23 + 1 / 60),
            event(8, 24), event(9, 24.5), agent_reply(10, 26),
            event(21, conversation_id=22, content="我要转人工"),
            event(22, 1, conversation_id=22), agent_reply(23, 11, conversation_id=22),
            event(31, conversation_id=32, content="我要转人工"),
            event(32, 1, conversation_id=32), agent_reply(33, 1 + 600.001 / 60, conversation_id=32),
            event(41, conversation_id=42, content="我要转人工"),
            event(42, 1, conversation_id=42), agent_reply(43, 12, conversation_id=42, agent_id=8))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        a = seats["7"]
        self.assertEqual((a["first_response_seconds"], a["first_response_samples"]), (600, 1))
        self.assertEqual((a["average_response_seconds"], a["response_samples"], a["response_rounds"]), (240, 3, 2))
        self.assertEqual(a["inquiries"], 3)
        self.assertEqual((seats["8"]["first_response_seconds"], seats["8"]["first_response_samples"]), (None, 0))
        self.assertEqual((seats["8"]["average_response_seconds"], seats["8"]["response_samples"],
                          seats["8"]["response_rounds"]), (None, 0, 0))
        self.assertEqual(seats["8"]["inquiries"], 1)

    def test_multi_seat_rounds_add_no_response_timings_to_any_seat(self):
        # Round 12: seat 8 helps seat 7. The round counts for nobody: no timing,
        # inquiry, reply or rating for either seat.
        # Round 22: seat 8 alone, credited. Seat 99's private note and rating
        # card in round 32 do not make seat 7's round an assisted one.
        self.receive(event(1, content="我要转人工"), event(2, 1), agent_reply(3, 2),
            event(4, 3), event(5, 4), agent_reply(6, 6, agent_id=8), agent_reply(7, 7, agent_id=8),
            event(8, 8), agent_reply(9, 9, agent_id=8, private=True),
            event(21, 20, conversation_id=22, content="我要转人工"), event(22, 21, conversation_id=22),
            agent_reply(23, 23, conversation_id=22, agent_id=8),
            event(31, 30, conversation_id=32, content="我要转人工"), event(32, 31, conversation_id=32),
            agent_reply(33, 32, conversation_id=32), agent_reply(34, 33, conversation_id=32, agent_id=99, private=True),
            agent_reply(35, 34, conversation_id=32, agent_id=99, content_type="input_csat"))
        self.drain()
        self.assign_first_repliers()
        self.admin.execute("UPDATE ops.round_index SET rating=2,rating_status='rated' WHERE source_key=%s AND conversation_id=12", (self.source,))
        _, seats = self.seats()
        self.assertEqual((seats["7"]["first_response_seconds"], seats["7"]["first_response_samples"]), (60, 1))
        self.assertEqual((seats["7"]["response_samples"], seats["7"]["response_rounds"]), (1, 1))
        self.assertEqual((seats["8"]["first_response_seconds"], seats["8"]["average_response_seconds"]), (120, 120))
        self.assertEqual((seats["8"]["response_samples"], seats["8"]["response_rounds"]), (1, 1))
        self.assertEqual([seats[k]["inquiries"] for k in ("7", "8")], [1, 1])
        self.assertEqual([seats[k]["replies"] for k in ("7", "8")], [1, 1])
        self.assertEqual([seats[k]["low_rating"] for k in ("7", "8")], [0, 0])
        self.refresh_facts()
        rows = self.admin.execute("""SELECT f.responsible_agent_id,f.assisted,f.first_response_seconds,f.response_samples
            FROM ops.round_service_facts f JOIN ops.round_index r USING(round_id)
            WHERE r.source_key=%s AND r.conversation_id=12""", (self.source,)).fetchall()
        self.assertEqual([dict(row) for row in rows], [{"responsible_agent_id": None, "assisted": True,
                                                       "first_response_seconds": None, "response_samples": None}])
        # Before the colleague replied the round had a single seat and counted.
        _, earlier = self.seats(now=START + timedelta(minutes=5))
        self.assertEqual((earlier["7"]["first_response_seconds"], earlier["7"]["first_response_samples"]), (60, 1))
        self.assertEqual((earlier["7"]["inquiries"], earlier["7"]["low_rating"]), (1, 1))
        _, second = self.seats(page=2, page_size=1)
        self.assertEqual(set(second), {"8"})
        self.assertEqual(second["8"]["first_response_seconds"], 120)

    def test_lost_transfers_use_confirmed_responsibility_and_include_seats_without_replies(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 2, private=True),
            agent_reply(3, 3, content_type="input_csat"), event(4, 4, sender="agent_bot", direction="outgoing"),
            event(21, 5, conversation_id=22, content="我要转人工"),
            event(31, 6, conversation_id=32, content="我要转人工"),
            event(32, 7, conversation_id=32, event_type="conversation_updated", assignee={"id": 7}),
            event(41, 7, conversation_id=42, content="我要转人工"), agent_reply(42, 9, conversation_id=42, agent_id=8),
            event(51, 8, conversation_id=52), event(61, 1440, conversation_id=62, content="我要转人工"))
        self.drain()
        for conversation, agent in [(12, "7"), (22, "99"), (42, "7")]:
            requested = self.admin.execute("""SELECT data->>'handoff_requested_at' AS at FROM ops.round_index
                WHERE source_key=%s AND conversation_id=%s""", (self.source, conversation)).fetchone()["at"]
            self.round_data(conversation, responsibility_basis="ics_assignment", responsible_agent_id=agent,
                responsible_agent_name="Assigned " + agent, responsible_assigned_at=requested)
        report, seats = self.seats()
        self.assertEqual((report["total_lost_inquiries"], report["unassigned_lost_inquiries"]), (3, 1))
        self.assertEqual(seats["7"]["lost_inquiries"], 1)
        self.assertEqual((seats["99"]["lost_inquiries"], seats["99"]["inquiries"]), (1, 0))
        self.assertIsNone(seats["99"]["first_response_seconds"])
        self.assertIsNone(seats["99"]["average_rating"])
        # Conversation 42 belongs to its assigned seat 7; seat 8 only helped.
        self.assertNotIn("8", seats)
        earlier, seats = self.seats(now=START + timedelta(minutes=8))
        self.assertEqual(earlier["total_lost_inquiries"], 4)
        self.assertEqual(seats["7"]["lost_inquiries"], 2)
        # Only ICS-assigned seats appear: 7 and 99 (seat 8 only helped in conversation 42).
        first, _ = self.seats(page_size=1)
        self.assertEqual((first["total"], first["total_lost_inquiries"]), (2, 3))

    def test_reply_timings_select_transfers_and_keep_completed_replies_after_the_window(self):
        self.receive(event(1, 470, content="我要转人工"), event(3, 471), agent_reply(2, 480))
        self.drain()
        self.assign_first_repliers()
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        for period in [self.period, ReportPeriod.build("month", date(2026, 10, 1)),
                       ReportPeriod.build("year", date(2026, 10, 1)),
                       ReportPeriod.build("custom", date(2026, 10, 1), date(2026, 10, 1))]:
            _, seats = self.seats(period=period, now=START + timedelta(days=2))
            self.assertEqual(seats["7"]["first_response_seconds"], 540)
            self.assertEqual(seats["7"]["average_response_seconds"], 540)
            self.assertEqual(seats["7"]["response_samples"], 1)
            self.assertEqual(seats["7"]["response_rounds"], 1)
        _, seats = self.seats(period=tomorrow, now=START + timedelta(days=2))
        self.assertIsNone(seats["7"]["first_response_seconds"])
        self.assertIsNone(seats["7"]["average_response_seconds"])
        self.assertEqual(seats["7"]["inquiries"], 1)
        yesterday, _ = self.seats(now=START + timedelta(days=2))
        self.assertEqual(yesterday["total_inquiries"], 0)
        self.assertEqual(yesterday["total_lost_inquiries"], 0)

    def test_unknown_transfer_or_message_times_never_invent_zero_waits(self):
        self.receive(event(1), agent_reply(2, 1), event(3, 2), agent_reply(4, 3),
            event(21, conversation_id=22, content="我要转人工"), event(22, 2, conversation_id=22),
            agent_reply(23, 3, conversation_id=22, agent_id=8))
        self.drain()
        self.assign_first_repliers()
        self.admin.execute("UPDATE ops.message_index SET sent_at=NULL WHERE source_key=%s AND message_id=22", (self.source,))
        _, seats = self.seats()
        self.assertIsNone(seats["7"]["first_response_seconds"])
        self.assertEqual((seats["7"]["average_response_seconds"], seats["7"]["response_samples"]), (None, 0))
        self.assertIsNone(seats["8"]["first_response_seconds"])
        self.assertIsNone(seats["8"]["average_response_seconds"])
        self.assertEqual(seats["8"]["inquiries"], 1)

    def test_true_zero_second_first_reply_and_subsecond_averages_remain_valid(self):
        self.receive(event(1, content="我要转人工"), event(2), agent_reply(3), event(4, 1), agent_reply(5, 1 + .5 / 60))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        self.assertEqual((seats["7"]["first_response_seconds"], seats["7"]["first_response_samples"]), (0, 1))
        self.assertEqual((seats["7"]["average_response_seconds"], seats["7"]["response_samples"]), (.25, 2))

    def test_transfer_without_a_followup_customer_message_has_no_response_sample(self):
        # The customer must speak after the transfer: the request starts no wait.
        self.receive(event(1, content="我要转人工"), agent_reply(2, 1), agent_reply(3, 2),
            event(11, conversation_id=22, content="我要转人工"), event(12, 3, conversation_id=22))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        a = seats["7"]
        self.assertIsNone(a["first_response_seconds"])
        self.assertIsNone(a["average_response_seconds"])
        self.assertEqual((a["first_response_samples"], a["response_samples"], a["response_rounds"]), (0, 0, 0))
        self.assertEqual(a["inquiries"], 1)

    def test_proactive_greeting_does_not_replace_first_actual_customer_response(self):
        # A seat speaking first is no response; the first response answers the
        # customer's first burst after the transfer, timed from its last message.
        self.receive(event(1, content="我要转人工"), agent_reply(2, 1),
            event(3, 2), event(4, 3), agent_reply(5, 4), agent_reply(6, 5),
            event(7, 6), event(8, 7), agent_reply(9, 8))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        self.assertEqual(seats["7"]["first_response_seconds"], 60)
        self.assertEqual(seats["7"]["average_response_seconds"], 60)
        self.assertEqual((seats["7"]["response_samples"], seats["7"]["response_rounds"]), (2, 1))

    def test_response_timings_exclude_invalid_messages_and_use_valid_attachments(self):
        self.receive(event(1, content="我要转人工"), event(2, .5, content=" "),
            event(3, .75, private=True), event(4, 1),
            agent_reply(5, 1.25, content=""), agent_reply(6, 1.5, content_type="input_csat"),
            agent_reply(7, 1.75, private=True), event(8, 2, sender="agent_bot", direction="outgoing"),
            event(9, 2.25), agent_reply(10, 2.5),
            event(11, 3, content="", attachments=[{"id": 111, "file_name": "question.png", "file_type": "image"}]),
            agent_reply(12, 4, content="", attachments=[{"id": 121, "file_name": "answer.png", "file_type": "image"}]),
            event(13, 5), agent_reply(14, 6), event(15, 7))
        self.drain()
        self.assign_first_repliers()
        self.admin.execute("UPDATE ops.message_index SET is_deleted=true WHERE source_key=%s AND message_id IN (9,10,14)", (self.source,))
        _, seats = self.seats()
        # The valid attachment at 3 is the customer's last message before the reply at 4.
        self.assertEqual(seats["7"]["first_response_seconds"], 60)
        self.assertEqual(seats["7"]["average_response_seconds"], 60)
        self.assertEqual(seats["7"]["response_samples"], 1)

    def test_timing_transfer_boundary_and_now_do_not_fall_back_to_reply_dates(self):
        self.receive(event(1, 470, content="我要转人工"), event(2, 471), agent_reply(3, 481),
            event(11, 480, conversation_id=22, content="我要转人工"),
            event(12, 481, conversation_id=22), event(13, 482, conversation_id=22),
            agent_reply(14, 483, conversation_id=22, agent_id=8))
        self.drain()
        self.assign_first_repliers()
        _, before_reply = self.seats(now=START + timedelta(minutes=480))
        # Assigned to seat 7 but not answered yet: a lost transfer without timings.
        self.assertEqual((before_reply["7"]["lost_inquiries"], before_reply["7"]["first_response_seconds"]), (1, None))
        _, today = self.seats(now=START + timedelta(minutes=485))
        self.assertEqual(set(today), {"7"})
        self.assertEqual(today["7"]["first_response_seconds"], 600)
        self.assertEqual(today["7"]["inquiries"], 0)
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        report, seats = self.seats(period=tomorrow, now=START + timedelta(minutes=485))
        self.assertEqual(set(seats), {"7", "8"})
        self.assertIsNone(seats["7"]["average_response_seconds"])
        # R3: from the customer's last message (482), not the first (481).
        self.assertEqual((seats["8"]["first_response_seconds"], seats["8"]["average_response_seconds"]), (60, 60))
        self.assertEqual(report["total_inquiries"], 2)

    def test_timing_only_seats_are_paginated_without_losing_source_scope(self):
        events = []
        for i in range(3):
            conversation, base = 100 + i, 100 + i * 10
            events.extend([event(base, 470, conversation_id=conversation, content="我要转人工"),
                event(base + 1, 471, conversation_id=conversation),
                agent_reply(base + 2, 480, conversation_id=conversation, agent_id=i + 7)])
        self.receive(*events)
        self.drain()
        self.assign_first_repliers()
        report, first = self.seats(page_size=2, now=START + timedelta(days=2))
        _, second = self.seats(page=2, page_size=2, now=START + timedelta(days=2))
        self.assertEqual((report["total"], len(first), len(second)), (3, 2, 1))
        for a in [*first.values(), *second.values()]:
            self.assertEqual((a["inquiries"], a["first_response_seconds"], a["response_rounds"]), (0, 540, 1))
        empty = self.reports.agent_report(self.period, source_key=self.source + "-absent", account_id=1,
            now=START + timedelta(days=2))
        self.assertEqual(empty["total"], 0)

    def test_consultation_unions_nested_touching_and_separate_spans_per_seat(self):
        events = []
        for i, (first, last) in enumerate([(1, 10), (2, 4), (8, 12), (12, 14), (20, 21)]):
            base, conversation = 100 + i * 10, 100 + i
            events.extend([event(base, conversation_id=conversation, content="我要转人工"),
                agent_reply(base + 1, first, conversation_id=conversation),
                agent_reply(base + 2, last, conversation_id=conversation)])
        self.receive(*events,
            event(203, 25, conversation_id=100), agent_reply(204, 30, conversation_id=100, private=True),
            agent_reply(205, 35, conversation_id=100, content_type="input_csat"),
            event(206, 40, conversation_id=100, sender="agent_bot", direction="outgoing"))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        # R11: each round runs from its transfer at 0; the replies at 12 and 20 come
        # after gaps over 10 minutes. Union per seat: 0-12, 12-14, 20-21.
        self.assertEqual(seats["7"]["consultation_seconds"], 15 * 60)
        _, earlier = self.seats(now=START + timedelta(minutes=11))
        self.assertEqual(earlier["7"]["consultation_seconds"], 10 * 60)

    def test_consultation_runs_from_the_transfer_and_skips_assisted_rounds(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 1), agent_reply(3, 5),
            # Seat 8 helps in conversation 22 (shift handover): an assisted round has no
            # consultation time for anybody (its times would spill across shifts).
            event(11, 10, conversation_id=22, content="我要转人工"), agent_reply(12, 11, conversation_id=22),
            agent_reply(15, 15, conversation_id=22), agent_reply(13, 20, conversation_id=22, agent_id=8),
            agent_reply(14, 30, conversation_id=22), agent_reply(16, 35, conversation_id=22, agent_id=8),
            # Seat 8 alone answers the round assigned to seat 7: assisted too, no time.
            event(21, 40, conversation_id=32, content="我要转人工"),
            agent_reply(22, 41, conversation_id=32, agent_id=8), agent_reply(23, 44, conversation_id=32, agent_id=8),
            # Private notes, rating cards and blank replies are not public seat replies.
            event(31, 50, conversation_id=42, content="我要转人工"), agent_reply(32, 51, conversation_id=42),
            agent_reply(33, 52, conversation_id=42, agent_id=8, private=True),
            agent_reply(34, 53, conversation_id=42, agent_id=8, content=" "), agent_reply(35, 57, conversation_id=42))
        self.drain()
        self.assign_first_repliers()
        self.round_data(32, responsibility_basis="ics_assignment", responsible_agent_id="7",
            responsible_agent_name="Assigned 7")
        _, seats = self.seats()
        self.assertEqual(seats["7"]["consultation_seconds"], (5 + 7) * 60)
        self.assertEqual(seats.get("8", {}).get("consultation_seconds", 0), 0)
        self.assertEqual(seats["7"]["inquiries"], 2)
        # Before seat 8's first reply (20) conversation 22 is not assisted yet: 10-15 counts.
        _, earlier = self.seats(now=START + timedelta(minutes=18))
        self.assertEqual(earlier["7"]["consultation_seconds"], (5 + 5) * 60)

    def test_consultation_starts_at_the_successful_transfer(self):
        # ICS assigned seat 7 at minute 3: the customer message at 2 is still the
        # waiting phase; timing starts at the assignment itself (R11).
        self.receive(event(1, content="我要转人工"), event(2, 2), event(3, 4),
            agent_reply(4, 6), agent_reply(5, 9),
            # No transfer at all was observed: timing starts at the seat's first reply.
            event(11, 20, conversation_id=22), agent_reply(12, 22, conversation_id=22),
            event(13, 24, conversation_id=22), agent_reply(14, 25, conversation_id=22))
        self.drain()
        self.assign_first_repliers()
        self.round_data(12, responsibility_basis="ics_assignment", responsible_agent_id="7",
            responsible_agent_name="Assigned 7",
            responsible_assigned_at=(START + timedelta(minutes=3)).isoformat())
        _, seats = self.seats()
        self.assertEqual(seats["7"]["consultation_seconds"], (6 + 3) * 60)

    def test_consultation_clips_active_segments_at_calendar_window_edges(self):
        # One active segment from 23:50 to 00:10 Beijing time (replies every 5 minutes).
        self.receive(event(1, content="我要转人工"),
            *[agent_reply(2 + i, 470 + i * 5) for i in range(5)])
        self.drain()
        self.assign_first_repliers()
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        report, seats = self.seats(period=tomorrow, now=START + timedelta(days=3))
        self.assertEqual((report["total"], report["total_inquiries"]), (1, 1))
        self.assertEqual(seats["7"]["consultation_seconds"], 10 * 60)
        _, today = self.seats(now=START + timedelta(days=3))
        self.assertEqual(today["7"]["consultation_seconds"], 10 * 60)
        for period in [ReportPeriod.build("month", date(2026, 10, 1)),
                       ReportPeriod.build("year", date(2026, 10, 1))]:
            _, whole = self.seats(period=period, now=START + timedelta(days=3))
            self.assertEqual(whole["7"]["consultation_seconds"], 20 * 60)

    def test_consultation_after_the_last_reply_counts_on_its_day_and_lost_rounds_have_none(self):
        """R2/S3: segments may run past the last reply (customer messages); a round nobody answered has none.
        The report and its archive drill-down list the same rounds."""
        from quality.query.repository import ArchiveFilters
        # Transfer 23:40, reply 23:50, customer 23:56 and 00:04 (8 minutes apart, one segment).
        self.receive(event(1, 460, content="我要转人工"), agent_reply(2, 470), event(3, 476), event(4, 484),
            # Lost: transfer 01:40, the customer keeps writing, nobody replies.
            event(11, 100, conversation_id=22, content="我要转人工"), event(12, 102, conversation_id=22),
            event(13, 105, conversation_id=22))
        self.drain()
        self.assign_first_repliers()
        self.round_data(22, responsibility_basis="ics_assignment", responsible_agent_id="7",
            responsible_agent_name="Seat 7", responsible_assigned_at=(START + timedelta(minutes=100)).isoformat())
        self.refresh_facts()
        now = START + timedelta(days=3)
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        _, next_day = self.seats(period=tomorrow, now=now)
        self.assertEqual(next_day["7"]["consultation_seconds"], 4 * 60)
        _, today = self.seats(now=now)
        self.assertEqual(today["7"]["consultation_seconds"], 20 * 60)
        self.assertEqual(today["7"]["lost_inquiries"], 1)
        spans = self.admin.execute("""SELECT r.conversation_id,isempty(f.consultation_spans) AS empty
            FROM ops.round_service_facts f JOIN ops.round_index r USING(round_id) WHERE r.source_key=%s
            ORDER BY 1""", (self.source,)).fetchall()
        self.assertEqual([(row["conversation_id"], row["empty"]) for row in spans], [(12, False), (22, True)])
        for period, conversations in ((tomorrow, [12]), (self.period, [12])):
            rounds = self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1,
                metric="consultation_seconds", metric_seat="7", start=period.start, end=period.end, as_of=now))
            self.assertEqual([item["conversation_id"] for item in rounds["items"]], conversations)

    def test_consultation_splits_a_round_at_idle_gaps_over_ten_minutes(self):
        self.receive(event(1, content="我要转人工"),
            # Segment 1: transfer 0 -> 9 (the customer message at 5 keeps it going).
            agent_reply(2, 1), event(3, 5), agent_reply(4, 9),
            # 21 idle minutes: a new segment starts at the customer's message (30) and
            # runs to 60; customer at 41 bridges 32 -> 50, exactly 600 seconds continues.
            event(5, 30), agent_reply(6, 32), event(7, 41), agent_reply(8, 50), agent_reply(9, 60),
            # Hours later in the same unresolved round: a lone reply adds nothing.
            agent_reply(10, 300), agent_reply(11, 400), agent_reply(12, 405),
            # Blank and private messages neither extend nor bridge a segment, so the
            # 11 minutes from 405 to 416 split it: 400 -> 405 counts, 416 is alone.
            agent_reply(13, 410, content=" "), agent_reply(14, 413, private=True), agent_reply(15, 416))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        self.assertEqual(seats["7"]["consultation_seconds"], (9 + 30 + 5) * 60)
        # A segment still running at "now" only counts its replies so far.
        _, earlier = self.seats(now=START + timedelta(minutes=55))
        self.assertEqual(earlier["7"]["consultation_seconds"], (9 + 20) * 60)

    def test_r3_acceptance_waits_start_at_the_last_customer_message_and_weight_by_waits(self):
        sec = 1 / 60
        # "转人工" at 0 is assigned to seat 8 at once; the seat's greeting at 0:30 is no
        # response (the customer has not spoken yet). Waits 10, 20 and 30 seconds follow.
        self.receive(event(1, 0, content="我要转人工"), agent_reply(2, 30 * sec, agent_id=8),
            event(3, 1), agent_reply(4, 1 + 10 * sec, agent_id=8), event(5, 2), agent_reply(6, 2 + 20 * sec, agent_id=8),
            event(7, 3), agent_reply(8, 3 + 30 * sec, agent_id=8),
            # R3-06: a question split into three parts, reply 10 seconds after the last;
            # R3-06a: two messages, reply 15 seconds after the second; R3-06b/c: a second
            # consecutive reply and an unanswered tail add nothing.
            event(11, 10, conversation_id=22, content="我要转人工"), event(12, 11, conversation_id=22),
            event(13, 11 + 20 * sec, conversation_id=22), event(14, 11 + 40 * sec, conversation_id=22),
            agent_reply(15, 11 + 50 * sec, conversation_id=22, agent_id=8),
            event(16, 15, conversation_id=22), event(17, 15.5, conversation_id=22),
            agent_reply(18, 15.75, conversation_id=22, agent_id=8), agent_reply(19, 16, conversation_id=22, agent_id=8),
            event(20, 17, conversation_id=22))
        self.receive_assignment("8", trigger_message_id=1, minute=0, business_round="r3-01")
        self.drain()
        self.assign_first_repliers()
        rows = {row["conversation_id"]: row for row in self.admin.execute("""
            SELECT r.conversation_id,f.first_response_seconds,f.average_response_seconds,f.response_samples,
                f.response_seconds_total
            FROM ops.round_service_facts f JOIN ops.round_index r USING(round_id) WHERE f.source_key=%s""",
            (self.source,)).fetchall()}
        # R3-07a: each round stores its mean, number of waits and their total seconds.
        self.assertEqual((rows[12]["first_response_seconds"], rows[12]["average_response_seconds"],
                          rows[12]["response_samples"], rows[12]["response_seconds_total"]), (10, 20, 3, 60))
        self.assertEqual((rows[22]["first_response_seconds"], rows[22]["average_response_seconds"],
                          rows[22]["response_samples"], rows[22]["response_seconds_total"]), (10, 12.5, 2, 25))
        for attempt in ("stored", "live"):
            _, seats = self.seats()
            a = seats["8"]
            # First responses (10 + 10) / 2; mean weighted by waits (60 + 25) / 5,
            # not the mean of round means (20 + 12.5) / 2.
            self.assertEqual((a["first_response_seconds"], a["first_response_samples"]), (10, 2), attempt)
            self.assertEqual((a["average_response_seconds"], a["response_samples"], a["response_rounds"]),
                             (17, 5, 2), attempt)
            self.admin.execute("UPDATE ops.round_service_facts SET fact_version=0 WHERE source_key=%s", (self.source,))

    def test_r11_acceptance_consultation_runs_from_transfer_to_resolution_without_idle_gaps(self):
        resolved = {"event_type": "conversation_status_changed", "status": "resolved"}
        self.receive(
            # R11-01 (seat 7): transfer 0, messages every few minutes to 20, resolved at 25 -> 25 minutes.
            event(1, 0, content="我要转人工"), agent_reply(2, 1), event(3, 8), agent_reply(4, 14), event(5, 20),
            event(6, 25, **resolved),
            # R11-02 (seat 8): 0-10, customer again at 240, reply 245, resolved 248 -> 10 + 8 minutes.
            event(11, 0, conversation_id=22, content="我要转人工"), agent_reply(12, 2, conversation_id=22, agent_id=8),
            agent_reply(13, 10, conversation_id=22, agent_id=8), event(14, 240, conversation_id=22),
            agent_reply(15, 245, conversation_id=22, agent_id=8), event(16, 248, conversation_id=22, **resolved),
            # R11-03 (seat 9): last message at 10, resolved hours later -> ends at 10.
            event(21, 0, conversation_id=32, content="我要转人工"), agent_reply(22, 5, conversation_id=32, agent_id=9),
            event(23, 10, conversation_id=32), event(24, 400, conversation_id=32, **resolved),
            # R11-06 (seat 10): two customer messages 12 minutes apart split the round: 0-3 + 15-16;
            # R11-09: not resolved yet, the last segment ends at the last message.
            event(31, 0, conversation_id=42, content="我要转人工"), agent_reply(32, 1, conversation_id=42, agent_id=10),
            event(33, 3, conversation_id=42), event(34, 15, conversation_id=42),
            agent_reply(35, 16, conversation_id=42, agent_id=10))
        self.drain()
        self.assign_first_repliers()
        expected = {"7": 25, "8": 10 + 8, "9": 10, "10": 3 + 1}
        for attempt in ("stored", "live"):
            _, seats = self.seats()
            self.assertEqual({seat: seats[seat]["consultation_seconds"] / 60 for seat in expected}, expected, attempt)
            self.admin.execute("UPDATE ops.round_service_facts SET fact_version=0 WHERE source_key=%s", (self.source,))
        # Before the resolution is seen, R11-01 ends at its last message (20).
        _, earlier = self.seats(now=START + timedelta(minutes=24))
        self.assertEqual(earlier["7"]["consultation_seconds"], 20 * 60)

    def test_dialogues_exclude_silent_transfers_and_require_strictly_more_than_five_messages(self):
        events = []
        for i, amount in enumerate([0, 5, 6]):
            base, conversation = 100 + i * 100, 100 + i
            events.extend(event(base + n, n, conversation_id=conversation) for n in range(8))
            events.extend([event(base + 10, 10, conversation_id=conversation, content="我要转人工"),
                agent_reply(base + 11, 11, conversation_id=conversation)])
            events.extend(event(base + 20 + n, 12 + n, conversation_id=conversation) for n in range(amount))
        self.receive(*events, event(401, conversation_id=200), agent_reply(402, 1, conversation_id=200))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        a = seats["7"]
        self.assertEqual((a["inquiries"], a["dialogues"], a["high_quality_dialogues"]), (4, 2, 1))
        # R11: transfers at 10 run to the last customer message (11, 16, 17); union 10-17.
        self.assertEqual(a["consultation_seconds"], 7 * 60)
        self.receive(agent_reply(500, 480, conversation_id=102, agent_id=8))
        self.drain()
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        _, second_day = self.seats(period=tomorrow, now=START + timedelta(days=2))
        # A colleague's later reply makes the round assisted: it counts for nobody on any day.
        self.assertEqual(second_day, {})
        _, first_day = self.seats(now=START + timedelta(days=2))
        a = first_day["7"]
        self.assertEqual((a["inquiries"], a["dialogues"], a["high_quality_dialogues"]), (3, 1, 0))

    def test_valid_visitor_messages_include_attachments_and_preserve_same_second_order(self):
        # The request and five genuine followups share Chatwoot's second precision.
        self.receive(event(1, content="我要转人工"), *[event(i) for i in range(2, 7)],
            event(7, content="", attachments=[{"id": 71, "file_name": "question.png", "file_type": "image"}]),
            event(8, 1, content=" \t\n"), event(9, 1, content=""), event(10, 1, private=True),
            event(11, 1, content_type="input_csat"),
            event(12, 1, sender="agent_bot", direction="outgoing"), agent_reply(13, 2),
            agent_reply(14, 3))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        self.assertEqual(set(seats), {"7"})
        for a in seats.values():
            self.assertEqual((a["dialogues"], a["high_quality_dialogues"]), (1, 1))
        # Deleted messages remain in the audit archive but are no longer valid content.
        self.admin.execute("UPDATE ops.message_index SET is_deleted=true WHERE source_key=%s AND message_id=7", (self.source,))
        _, seats = self.seats()
        for a in seats.values():
            self.assertEqual((a["dialogues"], a["high_quality_dialogues"]), (1, 0))
        self.admin.execute("UPDATE ops.message_index SET is_deleted=true WHERE source_key=%s AND message_id BETWEEN 2 AND 6", (self.source,))
        _, seats = self.seats()
        for a in seats.values():
            self.assertEqual((a["dialogues"], a["high_quality_dialogues"]), (0, 0))

    def test_same_second_greeting_before_handoff_does_not_create_a_dialogue(self):
        self.receive(event(1, content="你好"), event(2, content="麻烦帮我转接人工客服"), agent_reply(3, 1),
            event(11, conversation_id=22, content="你好"),
            event(12, conversation_id=22, content="我要转人工"),
            event(13, conversation_id=22, content="订单有个问题"), agent_reply(14, 1, conversation_id=22),
            event(21, 2, event_type="message_updated", message_id=2,
                created_at=START.isoformat(), content="后来编辑的文字"))
        self.drain()
        self.assign_first_repliers()
        _, seats = self.seats()
        self.assertEqual((seats["7"]["inquiries"], seats["7"]["dialogues"]), (2, 1))

    def test_assignments_count_successful_original_seat_even_when_another_seat_replies(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 2, agent_id=7),
            event(3, 2.5, event_type="conversation_updated", assignee={"id": 99}),
            event(4, 3, event_type="conversation_status_changed", status="resolved"),
            event(5, 4, content="我要转人工"), event(11, conversation_id=22, content="我要转人工"))
        self.receive_assignment("8", 1, 1, business_round="assignment-first")
        self.receive_assignment("8", 1, 1, business_round="assignment-first-duplicate")
        self.receive_assignment("8", 5, 5, business_round="assignment-second", session_minute=4)
        self.drain()
        report, seats = self.seats()
        # Seat 7 answered the round assigned to seat 8: it counts for nobody,
        # but both successful assignments still count for seat 8.
        self.assertEqual(set(seats), {"8"})
        self.assertEqual((seats["8"]["assigned_inquiries"], seats["8"]["inquiries"], seats["8"]["lost_inquiries"]), (2, 0, 1))
        self.assertEqual((seats["8"]["replies"], seats["8"]["consultation_seconds"]), (0, 0))
        self.assertEqual(report["unassigned_lost_inquiries"], 1)
        _, first = self.seats(page_size=1)
        self.assertEqual((set(first), first["8"]["assigned_inquiries"]), ({"8"}, 2))
        absent = self.reports.agent_report(self.period, source_key=self.source + "-absent", account_id=1,
            now=START + timedelta(days=1))
        self.assertEqual(absent["total"], 0)

    def test_assignment_dates_use_success_time_including_calendar_boundary_and_now(self):
        self.receive(event(1, 470, content="我要转人工"), agent_reply(2, 482, agent_id=7))
        self.receive_assignment("7", 1, 480, business_round="midnight-success", session_minute=470)
        self.drain()
        _, yesterday = self.seats(now=START + timedelta(days=2))
        self.assertEqual(sum(a["assigned_inquiries"] for a in yesterday.values()), 0)
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        for period in [tomorrow, ReportPeriod.build("month", date(2026, 10, 1)),
                       ReportPeriod.build("year", date(2026, 10, 1))]:
            _, seats = self.seats(period=period, now=START + timedelta(days=2))
            self.assertEqual((seats["7"]["assigned_inquiries"], seats["7"]["inquiries"]), (1, 1))
        _, at_boundary = self.seats(period=tomorrow, now=START + timedelta(minutes=480))
        self.assertEqual(sum(a["assigned_inquiries"] for a in at_boundary.values()), 0)

    def test_assignment_requests_conflicts_and_unknown_success_times_do_not_invent_counts(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 2),
            event(11, conversation_id=22, content="我要转人工"), agent_reply(12, 2, conversation_id=22),
            event(21, conversation_id=32, content="我要转人工"), agent_reply(22, 2, conversation_id=32),
            event(31, conversation_id=42, content="我要转人工"),
            event(32, 1, conversation_id=42, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE))
        self.receive_assignment("8", 1, 1, business_round="conflict")
        self.receive_assignment("9", 1, 1, business_round="conflict")
        self.drain()
        self.round_data(22, responsibility_basis="ics_assignment", responsible_agent_id="7", responsible_assigned_at=None)
        # Chatwoot assignees alone, even with an apparent timestamp, are not ICS success evidence.
        self.round_data(32, responsibility_basis="assignment_unobserved", responsible_agent_id="7",
            responsible_assigned_at=(START + timedelta(minutes=1)).isoformat())
        _, seats = self.seats()
        self.assertEqual(sum(a["assigned_inquiries"] for a in seats.values()), 0)

    def test_quality_uses_independent_latest_verdicts_and_deduplicates_rounds(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 1), agent_reply(3, 2),
            agent_reply(4, 3), event(5, 4),
            event(11, conversation_id=22), event(12, 1, conversation_id=22, sender="agent_bot", direction="outgoing"),
            event(21, conversation_id=32, content="我要转人工"),
            event(22, 1, conversation_id=32, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(23, 2, conversation_id=32, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(31, conversation_id=42),
            event(41, conversation_id=52), event(42, 1, conversation_id=52, sender="agent_bot", direction="outgoing"))
        self.drain()
        self.assign_first_repliers()
        first = self.review(12, "passed", "failed")
        self.review(22, "failed", "failed")  # No human reply: excluded from the human numerator.
        self.review(32, "failed", "failed")
        self.review(52, "passed", "passed")
        report, seats = self.quality(page_size=1)
        self.assertEqual(report["ai"], {"total_rounds": 5, "handoff": 2, "ai_only": 2,
            "handoff_failed": 1, "failed_rounds": 2, "failure_rate": 40})
        # The failed verdict belongs to seat 7, the round's only replying seat.
        self.assertEqual((report["total_inquiries"], report["total_failed_inquiries"], report["human_failure_rate"]), (1, 1, 100))
        self.assertEqual((seats["7"]["inquiries"], seats["7"]["failed_inquiries"], seats["7"]["failure_rate"]), (1, 1, 100))
        second, seats = self.quality(page=2, page_size=1)
        self.assertEqual((seats, second["total_failed_inquiries"]), ({}, 1))
        self.review(12, "failed", "passed", int(first["revision"]))
        # Rebuilding projections preserves the saved review and does not double-count its revisions.
        self.receive(agent_reply(6, 5))
        self.drain()
        self.assign_first_repliers()
        updated, _ = self.quality()
        self.assertEqual((updated["ai"]["failed_rounds"], updated["ai"]["failure_rate"]), (3, 60))
        self.assertEqual((updated["total_failed_inquiries"], updated["human_failure_rate"]), (0, 0))

    def test_quality_reuses_seat_metrics_and_keeps_ai_start_and_human_reply_dates(self):
        self.receive(event(1, content="我要转人工"), agent_reply(2, 480))
        self.receive_assignment("7", 1, 1, business_round="quality-assigned")
        self.drain()
        self.review(12, "failed", "failed")
        today, seats = self.quality()
        self.assertEqual((today["ai"]["total_rounds"], today["ai"]["failed_rounds"], today["total_inquiries"]), (1, 1, 0))
        self.assertEqual((seats["7"]["assigned_inquiries"], seats["7"]["failed_inquiries"], seats["7"]["failure_rate"]), (1, 0, None))
        tomorrow = ReportPeriod.build("day", date(2026, 10, 2))
        for period in [tomorrow, ReportPeriod.build("month", date(2026, 10, 1)),
                       ReportPeriod.build("year", date(2026, 10, 1))]:
            report, quality_seats = self.quality(period=period, now=START + timedelta(days=2))
            _, workload = self.seats(period=period, now=START + timedelta(days=2))
            self.assertEqual(report["human_failure_rate"], 100)
            for agent_id, seat in quality_seats.items():
                self.assertEqual({k: seat[k] for k in ("assigned_inquiries", "dialogues", "high_quality_dialogues", "inquiries")},
                    {k: workload[agent_id][k] for k in ("assigned_inquiries", "dialogues", "high_quality_dialogues", "inquiries")})
        self.assertEqual(self.quality(period=tomorrow, now=START + timedelta(days=2))[0]["ai"]["failed_rounds"], 0)

    def test_quality_zero_denominators_scope_and_pending_rounds(self):
        empty, _ = self.quality()
        self.assertEqual(empty["ai"], {"total_rounds": 0, "handoff": 0, "ai_only": 0,
            "handoff_failed": 0, "failed_rounds": 0, "failure_rate": None})
        self.assertIsNone(empty["human_failure_rate"])
        self.receive(event(1), agent_reply(2, 1), event(11, conversation_id=22),
            event(12, 1, conversation_id=22, sender="agent_bot", direction="outgoing"))
        self.drain()
        self.assign_first_repliers()
        pending = self.review(12, "pending", "pending")
        report, seats = self.quality()
        self.assertEqual((report["ai"]["total_rounds"], report["ai"]["failure_rate"], report["human_failure_rate"]), (2, 0, 0))
        self.assertEqual(seats["7"]["failed_inquiries"], 0)
        self.receive(agent_reply(13, 2, conversation_id=22))
        self.drain()
        self.assign_first_repliers()
        self.review(12, "pending", "failed", int(pending["revision"]))
        mixed, _ = self.quality()
        self.assertEqual((mixed["total_inquiries"], mixed["total_failed_inquiries"], mixed["human_failure_rate"]), (2, 1, 50))
        absent = self.reports.quality_report(self.period, source_key=self.source + "-absent", account_id=1,
            now=START + timedelta(days=1))
        self.assertEqual((absent["ai"]["total_rounds"], absent["total_inquiries"], absent["total_failed_inquiries"]), (0, 0, 0))

    def test_ai_quality_requires_an_actual_public_ai_reply_and_counts_refusal_once(self):
        self.receive(event(1), event(2, 1, sender="agent_bot", direction="outgoing", private=True),
            event(11, conversation_id=22), event(12, 1, conversation_id=22, sender="agent_bot", direction="outgoing", content_type="input_csat"),
            event(21, conversation_id=32), event(22, 1, conversation_id=32, sender="agent_bot", direction="outgoing"),
            event(31, conversation_id=42), event(32, 1, conversation_id=42, sender="agent_bot", direction="outgoing", content=" \t\n"),
            event(41, conversation_id=52, content="我要转人工"),
            event(42, 1, conversation_id=52, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            event(43, 2, conversation_id=52, sender="agent_bot", direction="outgoing", content=BUSY_NOTICE),
            agent_reply(44, 3, conversation_id=52), event(51, 1440, conversation_id=62))
        self.drain()
        self.admin.execute("UPDATE ops.message_index SET is_deleted=true WHERE source_key=%s AND message_id=22", (self.source,))
        report, _ = self.quality()
        self.assertEqual((report["ai"]["total_rounds"], report["ai"]["ai_only"], report["ai"]["handoff_failed"]), (5, 0, 1))
