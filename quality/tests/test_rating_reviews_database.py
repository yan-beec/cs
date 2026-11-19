"""独立测试库中的评分复核、筛选、并发与权限验证。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import os
import unittest

import psycopg

from quality.processing.stat_metrics import METRICS
from quality.query.repository import ArchiveFilters, ReviewConflict, ReviewValidationError
from quality.tests import test_query_database as fixture
from quality.tests.test_processing import START, event


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "需要隔离归档测试库")
class RatingReviewDatabaseTest(unittest.TestCase):
    setUpClass = classmethod(fixture.QueryDatabaseTest.setUpClass.__func__)
    tearDownClass = classmethod(fixture.QueryDatabaseTest.tearDownClass.__func__)
    setUp = fixture.QueryDatabaseTest.setUp
    overview_round = fixture.QueryDatabaseTest.overview_round
    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    rows = fixture.QueryDatabaseTest.rows

    def rated(self, name, score):
        return self.overview_round(name, rating_status="rated", rating=score)

    def save(self, round_id, status, previous=None):
        previous = previous or self.reader.rating_review(round_id)
        return self.reader.save_rating_review(round_id, status, "admin", int(previous["revision"]), previous["content_revision"])

    def test_one_three_five_and_filters_restore_without_changing_other_metrics(self):
        ids = [self.rated(str(score), score) for score in (1, 3, 5)]
        scope = ArchiveFilters(source_key=self.source)
        before = self.reader.overview(scope)
        self.assertEqual((before["rated_rounds"], before["average_rating"], before["low_rating_rounds"]), (3, 3, 2))
        saved = self.save(ids[0], "invalid")
        after = self.reader.overview(scope)
        self.assertEqual((after["rated_rounds"], after["average_rating"], after["low_rating_rounds"]), (2, 4, 1))
        for key in before:
            if key not in ("rated_rounds", "average_rating", "low_rating_rounds", "agents", "generated_at"):
                self.assertEqual(before[key], after[key], key)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, rating=4, rating_op="lt"))["total"], 2)
        drill = ArchiveFilters(source_key=self.source, metric="low_rating_rounds", start=START-timedelta(days=1), end=START+timedelta(days=1))
        self.assertEqual(self.reader.list_rounds(drill)["total"], 1)
        self.assertEqual(self.reader.overview(drill)["total_rounds"], 1)
        item = self.reader.round_detail(ids[0])["round"]
        self.assertEqual((item["rating"], item["rating_review"]["status"]), (1, "invalid"))
        self.assertEqual(len(self.reader.customer_rounds(item["customer_id"])["items"]), 3)
        # 原始事实上的评分不变，四个指标统一按当前状态排除。
        with self.reader.pool.connection() as connection:
            for key in ("rated_inquiries", "rating_sum", "low_rating", "low_rating_rounds"):
                metric = METRICS[key]
                row = connection.execute(f"SELECT {metric.aggregate()} AS value FROM (VALUES (%s::text,1),(%s::text,3),(%s::text,5)) f(round_id,rating)", ids).fetchone()
                self.assertEqual(row["value"], 8 if key == "rating_sum" else 2 if key == "rated_inquiries" else 1)
        self.save(ids[0], "valid", saved)
        self.assertEqual(self.reader.overview(scope)["average_rating"], 3)
        for round_id in ids:
            self.save(round_id, "invalid")
        empty = self.reader.overview(scope)
        self.assertEqual((empty["rated_rounds"], empty["low_rating_rounds"], empty["average_rating"]), (0, 0, None))

    def test_content_revision_idempotence_concurrency_and_permissions(self):
        round_id = self.rated("content", 2)
        old = self.reader.rating_review(round_id)
        # 不相关的投影时间不改变内容版本；关联补全必须改变版本。
        self.admin.execute("UPDATE ops.round_index SET data=data || '{\"replayed_at\":\"later\"}' WHERE round_id=%s", (round_id,))
        self.assertEqual(self.reader.rating_review(round_id)["content_revision"], old["content_revision"])
        self.admin.execute("UPDATE ops.round_index SET data=data || '{\"invitation_message_id\":500,\"rating_feedback\":\"feedback\"}' WHERE round_id=%s", (round_id,))
        with self.assertRaises(ReviewConflict):
            self.save(round_id, "invalid", old)
        current = self.reader.rating_review(round_id)
        self.assertEqual(current["selected_rating"]["rating_key"], "message:500")
        def attempt(status):
            try:
                return self.save(round_id, status, current)
            except ReviewConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ["valid", "invalid"]))
        self.assertEqual(results.count("conflict"), 1)
        saved = self.reader.rating_review(round_id)
        self.assertEqual(self.save(round_id, saved["status"], saved), saved)
        self.assertEqual(len(self.rows("quality.round_rating_reviews")), 1)
        with self.reader.pool.connection() as connection:
            for table in ("quality.round_rating_reviews", "quality.round_rating_review_current"):
                self.assertFalse(connection.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE') AS writes", (table,)).fetchone()["writes"])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.reader.pool.connection() as connection:
                connection.execute("SET TRANSACTION READ WRITE")
                connection.execute("DELETE FROM quality.round_rating_review_current WHERE round_id=%s", (round_id,))
        unscored = self.overview_round("unscored")
        with self.assertRaises(ReviewValidationError):
            self.save(unscored, "valid")
        self.assertIsNone(self.reader.rating_review("missing"))
        self.assertIsNone(self.reader.save_rating_review("missing", "valid", "admin", 0, "missing"))

    def test_review_survives_real_projection_rebuild(self):
        closed = START + timedelta(minutes=1)
        self.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
            event(4, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                  content_attributes={"ics_round_id":"original-round", "ics_closed_at":closed.isoformat()}),
            event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat",
                  csat_survey_response={"rating":2,"message_id":500}))
        self.drain()
        round_id = self.rows("ops.round_index")[0]["round_id"]
        saved = self.save(round_id, "invalid")
        report = self.processor.rebuild(self.source)
        self.assertTrue(report.successful)
        self.assertEqual(len(report.completed), 1)
        self.assertEqual(self.reader.rating_review(round_id), saved)
        self.assertEqual(self.reader.overview(ArchiveFilters(source_key=self.source))["rated_rounds"], 0)
        # 没有邀请/外部轮号时也保留实际评价键；内容相同的选中评价变化仍冲突。
        self.admin.execute("""UPDATE ops.round_index SET data=data ||
            '{"invitation_message_id":null,"external_round_id":null}' WHERE round_id=%s""", (round_id,))
        self.admin.execute("""UPDATE ops.rating_index SET rating_key='event:one',
            invitation_message_id=NULL,external_round_id=NULL WHERE round_id=%s""", (round_id,))
        old = self.reader.rating_review(round_id)
        self.assertEqual(old["selected_rating"]["rating_key"], "event:one")
        self.admin.execute("UPDATE ops.rating_index SET rating_key='event:two' WHERE round_id=%s", (round_id,))
        self.assertNotEqual(self.reader.rating_review(round_id)["content_revision"], old["content_revision"])
        with self.assertRaises(ReviewConflict):
            self.save(round_id, "valid", old)

    def test_atomic_dirty_days_across_month_and_later_fact_insert(self):
        from quality.processing.service_facts import store
        round_id = self.rated("cross-month", 1)
        days = [date(2041, 1, 31), date(2041, 2, 1), date(2041, 2, 2)]
        self.admin.execute("UPDATE ops.round_index SET started_at=%s WHERE round_id=%s",
                           (datetime(2041, 1, 31, tzinfo=timezone.utc), round_id))
        with self.admin.transaction():
            store(self.admin, [round_id])
        self.admin.execute("""UPDATE ops.round_service_facts SET first_reply_at=%s,last_reply_at=%s
            WHERE round_id=%s""", (datetime(2041, 2, 1, tzinfo=timezone.utc),
                                  datetime(2041, 2, 2, tzinfo=timezone.utc), round_id))
        with self.admin.transaction():
            for day in days:
                self.admin.execute("""INSERT INTO ops.stat_rollup_days(day,state,fact_version,rollup_version)
                    VALUES(%s,'built',11,3) ON CONFLICT(day) DO NOTHING""", (day,))
            self.admin.execute("DELETE FROM ops.stat_dirty_days WHERE day=ANY(%s)", (days,))
        self.save(round_id, "invalid")
        dirty = self.admin.execute("SELECT day FROM ops.stat_dirty_days WHERE day=ANY(%s) ORDER BY day", (days,)).fetchall()
        self.assertEqual([row["day"] for row in dirty], days)
        # 版本冲突不能写入历史，也不能制造日期失效。
        self.admin.execute("DELETE FROM ops.stat_dirty_days WHERE day=ANY(%s)", (days,))
        with self.assertRaises(ReviewConflict):
            self.reader.save_rating_review(round_id, "valid", "admin", 0, "stale")
        self.assertFalse(self.admin.execute("SELECT 1 FROM ops.stat_dirty_days WHERE day=ANY(%s)", (days,)).fetchall())
        pending = self.rated("pending-fact", 5)
        self.admin.execute("UPDATE ops.round_index SET started_at=%s WHERE round_id=%s",
                           (datetime(2041, 1, 31, tzinfo=timezone.utc), pending))
        self.save(pending, "invalid")
        with self.admin.transaction():
            store(self.admin, [pending])
        self.assertTrue(self.admin.execute("SELECT 1 FROM ops.stat_dirty_days WHERE day=%s", (days[0],)).fetchone())
        self.assertEqual(self.reader.rating_review(pending)["status"], "invalid")
