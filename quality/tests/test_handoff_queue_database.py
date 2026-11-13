"""在隔离归档库验证排队与评分复核共同作用时的统计口径。"""

from datetime import timedelta
import os
import unittest

from quality.processing.projector import HANDOFF_BUSY_NOTICE
from quality.query.repository import ArchiveFilters
from quality.tests import test_query_database as fixture
from quality.tests.test_processing import START, event
from quality.tests.test_handoff_queue_projection import notice


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "需要隔离归档测试库")
class HandoffQueueDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    setUp = fixture.QueryDatabaseTest.setUp
    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    rows = fixture.QueryDatabaseTest.rows

    def test_queue_excludes_handoff_loss_refusal_and_clock(self):
        self.receive(event(1, content="转人工"), notice(2, 1))
        self.drain()
        fact = self.rows("ops.round_service_facts")[0]
        self.assertTrue(fact["is_ai_only"])
        for key in ("is_handoff", "is_lost", "busy_refused"):
            self.assertFalse(fact[key], key)
        self.assertIsNone(fact["handoff_at"])
        self.assertIsNone(fact["first_response_seconds"])
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, lost=True))["total"], 0)

    def test_prior_refusal_remains_without_new_lost_request(self):
        self.receive(event(1, content="转人工"),
                     event(2, 1, sender="agent_bot", direction="outgoing", content=HANDOFF_BUSY_NOTICE),
                     event(3, 2, content="转人工"), notice(4, 3))
        self.drain()
        fact = self.rows("ops.round_service_facts")[0]
        self.assertTrue(fact["busy_refused"])
        self.assertTrue(fact["is_handoff"])
        self.assertFalse(fact["is_lost"])
        self.assertIsNone(fact["handoff_at"])

    def test_invalid_rating_on_queued_round_does_not_change_service_metrics(self):
        self.receive(event(1, content="转人工"), notice(2, 1),
                     event(3, 2, event_type="conversation_status_changed", status="resolved"),
                     event(4, 3, message_id=500, sender="agent_bot", direction="outgoing",
                           content_type="input_csat", content_attributes={
                               "ics_closed_at": (START + timedelta(minutes=2)).isoformat()}),
                     event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat",
                           csat_survey_response={"rating": 1, "message_id": 500}))
        self.drain()
        filters = ArchiveFilters(source_key=self.source)
        before = self.reader.overview(filters)
        round_id = self.rows("ops.round_index")[0]["round_id"]
        current = self.reader.rating_review(round_id)
        self.reader.save_rating_review(round_id, "invalid", "admin", int(current["revision"]), current["content_revision"])
        after = self.reader.overview(filters)
        self.assertEqual(before["rated_rounds"], 1)
        self.assertEqual(after["rated_rounds"], 0)
        self.assertIsNone(after["average_rating"])
        for key in ("total_rounds", "total_customers", "ai_only_rounds", "human_rounds",
                    "human_without_public_reply_rounds", "responsibility_unrecorded_rounds"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, rating=4, rating_op="lt"))["total"], 1)
