"""Opt-in checks for stored per-round statistics facts on a disposable quality_query_test_* database."""

from datetime import date, timedelta
import os
import unittest

from psycopg.types.json import Jsonb

from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import StatisticsRepository
from quality.processing.repository import scope_lock
from quality.processing.service_facts import FACT_VERSION, FactBackfill, store
from quality.tests.test_processing import START, agent_reply, event
from quality.tests import test_query_database as fixture
from ics_agent.statistics.tests import test_statistics_database as base


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires isolated reporting test database")
class StatisticsFactsDatabaseTest(unittest.TestCase):
    """Reports read stored facts, and give identical figures when a round is computed live."""

    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        self.statistics = StatisticsRepository(self.reader.pool)
        self.period = ReportPeriod.build("day", date(2026, 10, 1))

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    assign_first_repliers = base.StatisticsDatabaseTest.assign_first_repliers
    refresh_facts = base.StatisticsDatabaseTest.refresh_facts

    def round_data(self, conversation, **values):
        self.admin.execute("UPDATE ops.round_index SET data=data || %s WHERE source_key=%s AND conversation_id=%s",
            (Jsonb(values), self.source, conversation))

    def seats(self):
        report = self.statistics.agent_report(self.period, source_key=self.source, account_id=1,
                                              now=START + timedelta(days=1))
        return report, {a["agent_id"]: a for a in report["agents"]}

    def quality(self):
        report = self.statistics.quality_report(self.period, source_key=self.source, account_id=1,
                                                now=START + timedelta(days=1))
        return report, {a["agent_id"]: a for a in report["agents"]}

    def conversation_scenario(self):
        self.receive(
            event(1, 0, content="你好"), event(2, 1, content="我要转人工"),
            event(3, 2, content="订单查不到"), agent_reply(4, 3), event(5, 5, content="还有一个问题"),
            agent_reply(6, 7), agent_reply(7, 8, agent_id=8, agent_name="第二位客服"),
            event(11, 30, conversation_id=22, content="我要转人工"), event(12, 31, conversation_id=22),
            agent_reply(13, 45, conversation_id=22, agent_id=8, agent_name="第二位客服"),
            event(21, 60, conversation_id=32, content="只问AI"),
            event(22, 61, conversation_id=32, sender="agent_bot", direction="outgoing", content="AI回复"))
        self.drain()
        self.assign_first_repliers()

    def facts(self):
        return self.admin.execute("""SELECT round_id,visitor_messages,busy_refused,human_replied,ai_replied,
                handoff_at,fact_version FROM ops.round_service_facts WHERE source_key=%s ORDER BY round_id""",
            (self.source,)).fetchall()

    def seat_rows(self):
        return self.admin.execute("""SELECT round_id,responsible_agent_id,first_response_seconds,
                average_response_seconds,response_samples,consultation_spans::text AS spans
            FROM ops.round_service_facts WHERE source_key=%s AND responsible_agent_id IS NOT NULL
            ORDER BY round_id""", (self.source,)).fetchall()

    def snapshot(self):
        _, seats = self.seats()
        quality, quality_seats = self.quality()
        times = self.statistics.time_report(self.period, source_key=self.source, account_id=1,
                                            now=START + timedelta(days=1))
        return seats, quality_seats, quality["ai"], times["totals"]

    def test_processor_stores_current_facts_for_every_projected_round(self):
        self.conversation_scenario()
        rounds = self.admin.execute("SELECT count(*) AS n FROM ops.round_index WHERE source_key=%s",
                                    (self.source,)).fetchone()["n"]
        facts = self.facts()
        self.assertEqual(len(facts), rounds)
        self.assertTrue(all(row["fact_version"] == FACT_VERSION for row in facts))
        self.assertTrue(self.seat_rows())

    def test_stored_and_live_rules_give_identical_reports(self):
        self.conversation_scenario()
        stored = self.snapshot()
        # An outdated rule version is ignored: every round is computed live.
        self.admin.execute("UPDATE ops.round_service_facts SET fact_version=0 WHERE source_key=%s", (self.source,))
        self.assertEqual(self.snapshot(), stored)
        self.admin.execute("DELETE FROM ops.round_service_facts WHERE source_key=%s", (self.source,))
        self.assertEqual(self.snapshot(), stored)

    def test_stored_consultation_segments_match_the_live_rule(self):
        self.receive(event(1, 0, content="我要转人工"), agent_reply(2, 1), event(3, 6), agent_reply(4, 9),
            # 41 idle minutes in the same round, then a second active segment.
            event(5, 50), agent_reply(6, 52), agent_reply(7, 58),
            event(11, 100, conversation_id=22, content="我要转人工"), agent_reply(12, 101, conversation_id=22),
            agent_reply(13, 300, conversation_id=22))
        self.drain()
        self.assign_first_repliers()
        spans = {row["conversation_id"]: row["spans"] for row in self.admin.execute("""
            SELECT r.conversation_id,f.consultation_spans::text AS spans FROM ops.round_service_facts f
            JOIN ops.round_index r USING(round_id) WHERE f.source_key=%s""", (self.source,)).fetchall()}
        # R11: from the transfer; conversation 22 runs 100-101, its reply at 300 stands alone.
        self.assertEqual(spans[22].count("["), 1)
        self.assertEqual(spans[12].count("["), 2)
        stored = self.snapshot()
        self.assertEqual(stored[0]["7"]["consultation_seconds"], (9 + 8 + 1) * 60)
        self.admin.execute("DELETE FROM ops.round_service_facts WHERE source_key=%s", (self.source,))
        self.assertEqual(self.snapshot(), stored)

    def test_projection_changes_invalidate_only_affected_rounds(self):
        self.conversation_scenario()
        before = {row["round_id"] for row in self.facts()}
        changed = self.admin.execute("""SELECT round_id FROM ops.message_index WHERE source_key=%s AND message_id=5""",
                                     (self.source,)).fetchone()["round_id"]
        # Rewriting identical values (processor upserts) keeps the facts.
        self.admin.execute("UPDATE ops.message_index SET content=content WHERE source_key=%s", (self.source,))
        self.admin.execute("UPDATE ops.message_index SET customer_id=customer_id WHERE source_key=%s", (self.source,))
        self.assertEqual({row["round_id"] for row in self.facts()}, before)
        self.admin.execute("UPDATE ops.message_index SET is_deleted=true WHERE source_key=%s AND message_id=5", (self.source,))
        self.assertEqual({row["round_id"] for row in self.facts()}, before - {changed})
        live = self.snapshot()
        with self.processor.pool.connection() as connection:
            store(connection, [changed])
        self.assertEqual({row["round_id"] for row in self.facts()}, before)
        self.assertEqual(self.snapshot(), live)

    def test_round_projection_changes_invalidate_and_reports_follow_them(self):
        self.conversation_scenario()
        self.round_data(22, takeover_inherited=True)
        conversation_round = self.admin.execute("""SELECT round_id FROM ops.round_index
            WHERE source_key=%s AND conversation_id=22""", (self.source,)).fetchone()["round_id"]
        self.assertNotIn(conversation_round, {row["round_id"] for row in self.facts()})

    def test_backfill_restores_identical_facts_newest_first(self):
        self.conversation_scenario()
        facts, seats = self.facts(), self.seat_rows()
        self.admin.execute("DELETE FROM ops.round_service_facts WHERE source_key=%s", (self.source,))
        backfill = FactBackfill(self.processor.pool, scope_lock, batch_size=2, pause_seconds=0)
        for _ in range(10_000):
            if not backfill.step():
                break
        self.assertTrue(backfill.done)
        restored = self.facts()
        self.assertEqual([{k: v for k, v in row.items()} for row in restored],
                         [{k: v for k, v in row.items()} for row in facts])
        self.assertEqual(self.seat_rows(), seats)

    def test_backfill_leaves_conversations_locked_by_a_live_replay_for_a_later_retry(self):
        self.conversation_scenario()
        self.admin.execute("DELETE FROM ops.round_service_facts WHERE source_key=%s", (self.source,))
        backfill = FactBackfill(self.processor.pool, scope_lock, batch_size=100, pause_seconds=0)
        import psycopg
        holder = psycopg.connect(**{**self.settings.connection_kwargs, "user": "archive_admin",
            "password": os.environ["PROCESSING_TEST_ADMIN_PASSWORD"]})
        def missing():
            return {row["conversation_id"] for row in self.admin.execute("""SELECT r.conversation_id
                FROM ops.round_index r LEFT JOIN ops.round_service_facts f USING(round_id)
                WHERE r.source_key=%s AND f.round_id IS NULL""", (self.source,)).fetchall()}
        try:
            holder.execute("SELECT pg_advisory_lock(%s)", (scope_lock(self.source, 1, 12),))
            # Other suites share this database: scan until this source's free conversations are filled.
            for _ in range(10_000):
                if (self.source, 1, 12) in backfill.skipped or not backfill.step():
                    break
            self.assertEqual(missing(), {12})
            self.assertIn((self.source, 1, 12), backfill.skipped)
        finally:
            holder.close()
        while backfill.step():
            pass
        self.assertEqual(missing(), set())
