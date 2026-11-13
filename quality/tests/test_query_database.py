"""Opt-in integration checks; never point these at the user's archive database.

Apply all archive migrations to an isolated database first. Set PROCESSING_TEST_DATABASE=1,
ARCHIVE_POSTGRES_DB=quality_query_test_..., ARCHIVE_INGEST_PASSWORD and
PROCESSING_TEST_ADMIN_PASSWORD. No production names are accepted.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from dataclasses import replace
import os
from unittest.mock import patch
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from quality.capture.envelope import CaptureEnvelope
from quality.capture.repository import CaptureRepository
from quality.capture.config import CaptureSettings
from quality.processing.config import ProcessingSettings
from quality.processing.repository import ProcessingRepository
from quality.query.config import QuerySettings
from quality.query.repository import ArchiveFilters, QueryRepository
from quality.tests.test_processing import START, agent_reply, event


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated query test database")
class QueryDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = ProcessingSettings()
        if not cls.settings.postgres_db.startswith(("quality_query_test_", "quality_capture_test_")):
            raise RuntimeError("Query integration tests require a disposable database name")
        cls.admin = psycopg.connect(**{**cls.settings.connection_kwargs, "user": "archive_admin",
            "password": os.environ["PROCESSING_TEST_ADMIN_PASSWORD"], "row_factory": dict_row}, autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def setUp(self):
        self.source = "query-test-" + uuid.uuid4().hex
        self.capture = CaptureRepository(CaptureSettings(_env_file=None,
            source_key=self.source, allowed_account_ids="1", webhook_secret="test-signing-secret-0123456789abcdef",
            ingest_password=self.settings.ingest_password, postgres_host=self.settings.postgres_host,
            postgres_port=self.settings.postgres_port, postgres_db=self.settings.postgres_db,
            postgres_sslmode=self.settings.postgres_sslmode))
        self.capture.open()
        self.addCleanup(self.capture.close)
        self.processor = ProcessingRepository(self.settings, {("1", "3"): "xingliao"})
        self.processor.open()
        self.addCleanup(self.processor.close)
        self.reader = QueryRepository(QuerySettings(_env_file=None, query_enabled=True,
            reader_password=os.environ["ARCHIVE_READER_PASSWORD"],
            postgres_host=self.settings.postgres_host, postgres_port=self.settings.postgres_port,
            postgres_db=self.settings.postgres_db, postgres_sslmode=self.settings.postgres_sslmode))
        self.reader.open()
        self.addCleanup(self.reader.close)

    def receive(self, *events):
        receipts = []
        for item in events:
            receipts.append(self.capture.receive(CaptureEnvelope.parse(json.dumps(item["payload"]).encode())))
        return receipts

    def receive_bulk(self, events):
        """Capture equivalent bytes/jobs in bounded batches, only in this disposable DB."""
        from itertools import islice
        def parameters():
            for item in events:
                envelope = CaptureEnvelope.parse(json.dumps(item["payload"]).encode())
                yield (self.source, envelope.account_id, envelope.event_key, envelope.event_type,
                       envelope.conversation_id, envelope.message_id, envelope.raw_body, envelope.raw_body)
        pending = iter(parameters())
        with self.admin.transaction(), self.admin.cursor() as cursor:
            while batch := list(islice(pending, 500)):
                cursor.executemany("""INSERT INTO archive.events
                    (source_key,account_id,event_key,event_type,ingestion_kind,conversation_id,message_id,raw_body,payload)
                    VALUES(%s,%s,%s,%s,'webhook',%s,%s,%s,convert_from(%s,'UTF8')::jsonb)
                    ON CONFLICT(source_key,account_id,event_key) DO NOTHING""", batch)
            cursor.execute("""INSERT INTO ops.event_jobs(event_id) SELECT id FROM archive.events
                WHERE source_key=%s ON CONFLICT DO NOTHING""", (self.source,))

    @staticmethod
    def rounds_events(count, *, conversation_id=12, start=0, first_id=1):
        for index in range(count):
            yield event(first_id+index*2, start+index*10, conversation_id=conversation_id, content=f"round {index+1}")
            yield event(first_id+index*2+1, start+index*10+2, conversation_id=conversation_id,
                        event_type="conversation_status_changed", status="resolved")

    def drain(self):
        for _ in range(100):
            if not self.processor.process_next("test-worker"):
                return
        self.fail("Unexpected unbounded ready queue")

    def receive_assignment(self, agent_id="8", trigger_message_id=1, minute=1, *, business_round="business-round-1", session_minute=0):
        at = (START+timedelta(minutes=minute)).isoformat()
        payload = {"event": "ics_assignment_recorded", "account": {"id": 1},
            "conversation": {"id": 12, "account_id": 1}, "updated_at": at,
            "assignment": {"schema_version": 1, "origin": "ics", "assignment_key": business_round,
                "business_round_id": business_round, "agent_id": agent_id, "trigger_message_id": str(trigger_message_id),
                "session_started_at": (START+timedelta(minutes=session_minute)).isoformat(), "assigned_at": at,
                "recorded_at": at, "evidence_kind": "assignment_applied"}}
        row = self.admin.execute("""INSERT INTO archive.events
            (source_key,account_id,event_key,event_type,ingestion_kind,conversation_id,raw_body,payload)
            VALUES (%s,1,%s,'ics_assignment_recorded','backfill',12,%s,%s) RETURNING id""",
            (self.source, "assignment:"+business_round+":"+str(agent_id), json.dumps(payload).encode(), Jsonb(payload))).fetchone()
        self.admin.execute("INSERT INTO ops.event_jobs(event_id) VALUES(%s)", (row["id"],))
        return row["id"]

    def rows(self, table):
        # table names below are fixed constants owned by this test.
        from psycopg import sql
        return self.admin.execute(sql.SQL("SELECT * FROM {} WHERE source_key=%s ORDER BY 1").format(
            sql.Identifier(*table.split("."))), (self.source,)).fetchall()

    def overview_round(self, name, *, customer="42", source=None, account=1, reception="human",
                       status="active", rating_status="not_invited", rating=None, public_replies=0,
                       basis=None, responsible=None, agents=()):
        """Insert disposable derived rows to exercise aggregate edge cases."""
        source = source or self.source
        self.overview_sequence = getattr(self, "overview_sequence", 0) + 1
        customer_id = f"overview-customer:{source}:{account}:{customer}"
        round_id = f"overview-round:{self.source}:{name}"
        data = {} if public_replies is None else {"counts": {"agent": public_replies}}
        if basis is not None:
            data["responsibility_basis"] = basis
        if responsible is not None:
            data.update(responsible_agent_id=responsible, responsible_agent_name=f"Agent {responsible}")
        self.admin.execute("""INSERT INTO ops.customer_index
            (customer_id,source_key,account_id,contact_id,display_name)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(customer_id) DO NOTHING""",
            (customer_id, source, account, customer, "Overview " + customer))
        self.admin.execute("""INSERT INTO ops.round_index
            (round_id,source_key,account_id,conversation_id,customer_id,round_no,started_at,last_message_at,
             reception_type,reply_composition,status,rating_status,rating,data)
            VALUES(%s,%s,%s,%s,%s,1,%s,%s,%s,'none',%s,%s,%s,%s)""",
            (round_id, source, account, 10000+self.overview_sequence, customer_id, START, START,
             reception, status, rating_status, rating, Jsonb(data)))
        for agent_id, replied, assigned, noted in agents:
            self.admin.execute("""INSERT INTO ops.round_agent_index
                (round_id,agent_id,agent_name,replied,was_assigned,noted,reply_count)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (round_id, agent_id, f"Agent {agent_id}", replied, assigned, noted, int(replied)))
        return round_id

    def test_overview_totals_ratings_and_agent_attribution_count_each_round_once(self):
        self.overview_round("rated-low", status="resolved", rating_status="rated", rating=2, public_replies=2,
            basis="ics_assignment", responsible="8",
            agents=(("8", True, True, False), ("9", True, False, False), ("10", False, True, False)))
        self.overview_round("assigned-no-reply", basis="ics_assignment", responsible="8",
            agents=(("11", False, False, True),))
        self.overview_round("ai-rated", customer="99", reception="ai_only", status="resolved", rating_status="rated", rating=5)
        self.overview_round("conflict-pending", customer="99", status="idle_boundary", rating_status="pending", rating=1,
            basis="assignment_conflict", responsible="9")
        self.overview_round("legacy", customer="99", public_replies=1, basis="last_public_agent_reply", responsible="9",
            agents=(("10", True, False, False),))
        self.overview_round("unknown-rated", customer="99", reception="unknown", rating_status="rated", rating=4)
        self.overview_round("missing-counts", customer="99", public_replies=None)
        result = self.reader.overview(ArchiveFilters(source_key=self.source, account_id=1))
        expected = {"total_rounds": 7, "total_customers": 2, "ai_only_rounds": 1, "human_rounds": 5,
            "active_rounds": 4, "resolved_rounds": 2, "rated_rounds": 3, "low_rating_rounds": 1,
            "human_without_public_reply_rounds": 2, "responsibility_unrecorded_rounds": 3,
            "participating_agent_count": 3, "agents_truncated": False}
        for key, value in expected.items():
            self.assertEqual(result[key], value, key)
        self.assertAlmostEqual(result["average_rating"], 11/3)
        agents = {row["agent_id"]: row for row in result["participating_agents"]}
        self.assertEqual(set(agents), {"8", "9", "10"})
        self.assertEqual([agents["8"][key] for key in ("responsible_rounds", "replied_rounds", "low_rating_rounds")], [2, 1, 1])
        for agent_id in ("9", "10"):
            self.assertEqual([agents[agent_id][key] for key in ("responsible_rounds", "replied_rounds", "low_rating_rounds")], [0, 1, 0])
        self.assertTrue(all(row["source_key"] == self.source and row["account_id"] == 1 for row in agents.values()))
        from datetime import datetime
        self.assertIsNotNone(datetime.fromisoformat(result["generated_at"]).tzinfo)
        empty = self.reader.overview(ArchiveFilters(source_key=self.source + "-absent"))
        self.assertEqual(empty["total_rounds"], 0)
        self.assertEqual(empty["total_customers"], 0)
        self.assertEqual(empty["participating_agent_count"], 0)
        self.assertEqual(empty["participating_agents"], [])
        self.assertIsNone(empty["average_rating"])
        self.assertFalse(empty["agents_truncated"])

    def test_overview_reuses_round_filters_without_expanding_matching_customer_history(self):
        other_customer = event(5, 4, conversation_id=22, content="needle 100%_literal")
        other_customer["payload"]["sender"]["id"] = 99
        other_customer["payload"]["conversation"]["meta"]["sender"]["id"] = 99
        self.receive(event(1, content="needle 100%_literal", attachments=[{"id": 90, "file_name": "proof.png"}]),
            event(2, 1, event_type="message_updated", message_id=1, content="edited away",
                  created_at=START.isoformat(), attachments=[{"id": 90, "file_name": "proof.png"}],
                  updated_at=(START+timedelta(minutes=1)).isoformat()),
            agent_reply(6, 1, agent_id=7),
            event(3, 2, event_type="conversation_status_changed", status="resolved"),
            event(4, 3, content="unrelated next round"), other_customer,
            event(7, 5, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                  content_attributes={"ics_closed_at": (START+timedelta(minutes=2)).isoformat()},
                  csat_survey_response={"message_id": 500, "rating": 3}))
        self.receive_assignment(minute=1)
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        all_rounds = self.reader.overview(ArchiveFilters(**base))
        self.assertEqual((all_rounds["total_rounds"], all_rounds["total_customers"]), (3, 2))
        filters = ArchiveFilters(**base, customer_query="42", keyword="100%_literal", reception_type="human",
            status="resolved", rating_status="rated", rating=3, inbox_id=3, channel_key="xingliao",
            agent_id="7", agent_scope="replied", round_no=1, conversation_id=12, message_scope="contact",
            has_attachment=True, time_field="message_at", start=START, end=START+timedelta(minutes=3))
        result = self.reader.overview(filters)
        self.assertEqual(result["total_rounds"], 1)
        self.assertEqual(result["total_customers"], 1)
        self.assertEqual(result["rated_rounds"], 1)
        self.assertEqual(result["low_rating_rounds"], 1)
        self.assertEqual(result["average_rating"], 3)
        # ICS assigned seat 8 but seat 7 answered: an assisted round, assessed for nobody (statistics rule).
        self.assertEqual({row["agent_id"] for row in result["participating_agents"]}, {"7"})
        self.assertEqual(result["assisted_rounds"], 1)
        self.assertEqual(self.reader.overview(ArchiveFilters(**base, agent_id="8"))["total_rounds"], 0)
        self.assertEqual(self.reader.list_rounds(filters, page_size=1)["total"], result["total_rounds"])
        # The same customer has another round that does not contain the keyword.
        self.assertEqual(self.reader.overview(ArchiveFilters(**base, customer_query="42"))["total_rounds"], 2)
        self.assertEqual(self.reader.overview(ArchiveFilters(**base, agent_id="7", agent_scope="replied"))["total_rounds"], 1)
        self.assertEqual(self.reader.overview(ArchiveFilters(**base, keyword="100ZZliteral"))["total_rounds"], 0)
        self.assertEqual(self.reader.overview(ArchiveFilters(**base, has_attachment=False))["total_rounds"], 2)

    def test_rating_operator_compares_rated_rounds_only(self):
        self.overview_round("r2", status="resolved", rating_status="rated", rating=2)
        self.overview_round("r4", status="resolved", rating_status="rated", rating=4)
        self.overview_round("r5", status="resolved", rating_status="rated", rating=5)
        self.overview_round("pending-1", rating_status="pending", rating=1)
        base = {"source_key": self.source, "account_id": 1}
        counts = {op: self.reader.overview(ArchiveFilters(**base, rating=4, rating_op=op))["total_rounds"]
                  for op in ("lt", "eq", "gt")}
        self.assertEqual(counts, {"lt": 1, "eq": 1, "gt": 1})
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, rating=4, rating_op="lt"))["total"], 1)

    def test_lost_filter_follows_handoff_clock_and_public_agent_replies(self):
        self.receive(event(1, 0, content="需要人工"))
        self.receive_assignment(minute=1)
        self.drain()
        base = {"source_key": self.source, "account_id": 1, "agent_id": "8"}
        window = ArchiveFilters(**base, lost=True, time_field="handoff_at", start=START, end=START+timedelta(hours=1))
        self.assertEqual(self.reader.overview(window)["total_rounds"], 1)
        self.assertEqual(self.reader.list_rounds(window)["total"], 1)
        self.assertEqual(self.reader.list_customers(window)["total"], 1)
        later = ArchiveFilters(**base, lost=True, time_field="handoff_at", start=START+timedelta(minutes=2))
        self.assertEqual(self.reader.overview(later)["total_rounds"], 0, "转人工时间早于窗口")
        # 私密备注不算回复；公开人工回复后不再是流单。
        self.receive(agent_reply(2, 2, agent_id=8, private=True))
        self.drain()
        self.assertEqual(self.reader.overview(window)["total_rounds"], 1)
        self.receive(agent_reply(3, 3, agent_id=8))
        self.drain()
        self.assertEqual(self.reader.overview(window)["total_rounds"], 0)
        self.assertEqual(self.reader.overview(replace(window, lost=None))["total_rounds"], 1)

    def test_overview_caps_agent_list_and_keeps_full_count_with_source_account_isolation(self):
        marker = "overview-scope-" + uuid.uuid4().hex
        self.overview_round("many", customer=marker, public_replies=205, basis="ics_assignment", responsible="1",
            agents=tuple((str(index), True, False, False) for index in range(1, 206)))
        self.overview_round("other-source", customer=marker, source=self.source + "-other", public_replies=1,
            basis="ics_assignment", responsible="1", agents=(("1", True, False, False),))
        self.overview_round("other-account", customer=marker, account=2, public_replies=1,
            basis="ics_assignment", responsible="1", agents=(("1", True, False, False),))
        scoped = self.reader.overview(ArchiveFilters(source_key=self.source, account_id=1))
        self.assertEqual(scoped["total_rounds"], 1)
        self.assertEqual(scoped["participating_agent_count"], 205)
        self.assertEqual(len(scoped["participating_agents"]), 200)
        self.assertTrue(scoped["agents_truncated"])
        combined = self.reader.overview(ArchiveFilters(customer_query=marker))
        self.assertEqual(combined["total_rounds"], 3)
        self.assertEqual(combined["total_customers"], 3)
        self.assertEqual(combined["participating_agent_count"], 207)
        first_ids = [row for row in combined["participating_agents"] if row["agent_id"] == "1"]
        self.assertEqual(len(first_ids), 3)
        self.assertEqual({(row["source_key"], row["account_id"]) for row in first_ids},
            {(self.source, 1), (self.source, 2), (self.source + "-other", 1)})
        self.assertTrue(all(row["responsible_rounds"] == 1 and row["replied_rounds"] == 1 for row in first_ids))

    def test_atomic_processing_materializes_round_messages_and_durable_attachment_work(self):
        self.receive(event(1, content="original", attachments=[{"id": 91, "file_name": "test.png", "data_url": "https://example.invalid/file"}]),
            event(2, 1, sender="agent_bot", direction="outgoing"), event(3, 2, sender="user", direction="outgoing"))
        self.drain()
        rounds = self.rows("ops.round_index")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["reply_composition"], "mixed")
        self.assertEqual(rounds[0]["data"]["counts"]["contact"], 1)
        self.assertEqual(len(self.rows("ops.message_index")), 3)
        self.assertEqual(len(self.rows("archive.message_snapshots")), 3)
        count = self.admin.execute("""SELECT count(*) AS n FROM ops.attachment_jobs j
            JOIN archive.attachments a ON a.id=j.attachment_id
            JOIN archive.message_snapshots m ON m.id=a.snapshot_id WHERE m.source_key=%s""", (self.source,)).fetchone()["n"]
        self.assertEqual(count, 1)
        states = self.admin.execute("""SELECT j.status FROM ops.event_jobs j JOIN archive.events e ON e.id=j.event_id
            WHERE e.source_key=%s""", (self.source,)).fetchall()
        self.assertTrue(all(row["status"] == "completed" for row in states))

    def test_rebuild_is_idempotent_and_edits_keep_immutable_original(self):
        self.receive(event(1, message_id=100, content="original"),
            event(2, 1, event_type="message_updated", message_id=100, content="edited", updated_at=(START+timedelta(minutes=1)).isoformat()),
            event(3, 2, event_type="message_updated", message_id=100, content=None, content_attributes={"deleted": True},
                  updated_at=(START+timedelta(minutes=2)).isoformat()))
        self.drain()
        original = self.rows("ops.message_index")[0]
        for _ in range(2):
            self.assertEqual(len(self.processor.rebuild(self.source).completed), 1)
        latest = self.rows("ops.message_index")[0]
        self.assertEqual(original["original_snapshot_id"], latest["original_snapshot_id"])
        self.assertTrue(latest["is_deleted"])
        self.assertEqual(len(self.rows("archive.message_snapshots")), 3)
        self.assertEqual(self.admin.execute("SELECT content FROM archive.message_snapshots WHERE id=%s",
            (latest["original_snapshot_id"],)).fetchone()["content"], "original")
        self.assertEqual(self.rows("ops.round_index")[0]["data"]["counts"]["contact"], 1)

    def test_projection_failure_rolls_back_snapshots_and_leaves_durable_retry(self):
        receipt = self.receive(event(1))[0]
        claim = self.processor.claim("failure-worker")
        # There are no ready events from previous tests; claims are scoped by
        # their own persistent evidence, not process-local input.
        self.assertEqual(claim["event_id"], receipt.event_id)
        write = self.processor._write_projection
        def fail_after_write(*args, **kwargs):
            write(*args, **kwargs)
            raise RuntimeError("injected failure")
        with patch.object(self.processor, "_write_projection", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                self.processor.process_claim(claim)
        self.assertEqual(self.rows("archive.message_snapshots"), [])
        self.assertEqual(self.rows("ops.round_index"), [])
        self.assertEqual(len(self.rows("archive.events")), 1)
        self.processor._retry(claim, "RuntimeError")
        state = self.admin.execute("SELECT status,locked_by FROM ops.event_jobs WHERE event_id=%s", (receipt.event_id,)).fetchone()
        self.assertEqual(state, {"status": "retry", "locked_by": None})
        self.admin.execute("UPDATE ops.event_jobs SET available_at=CURRENT_TIMESTAMP WHERE event_id=%s", (receipt.event_id,))
        self.drain()

    def test_expired_lease_can_be_reclaimed_without_duplicate_snapshots(self):
        receipt = self.receive(event(1))[0]
        old = self.processor.claim("old-worker")
        self.admin.execute("UPDATE ops.event_jobs SET locked_until=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE event_id=%s", (receipt.event_id,))
        new = self.processor.claim("new-worker")
        self.assertNotEqual(old["locked_by"], new["locked_by"])
        self.processor.process_claim(old)
        self.assertEqual(self.rows("archive.message_snapshots"), [])
        self.processor.process_claim(new)
        self.assertEqual(len(self.rows("archive.message_snapshots")), 1)

    def test_parallel_workers_do_not_duplicate_logical_message_counts(self):
        self.receive(event(1), event(2, 1, sender="user", direction="outgoing"), event(3, 2))
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self.processor.process_next, ("parallel-1", "parallel-2")))
        self.admin.execute("""UPDATE ops.event_jobs SET available_at=CURRENT_TIMESTAMP
            WHERE status='retry' AND event_id IN (SELECT id FROM archive.events WHERE source_key=%s)""", (self.source,))
        self.drain()
        self.assertEqual(len(self.rows("ops.round_index")), 1)
        self.assertEqual(self.rows("ops.round_index")[0]["data"]["counts"]["contact"], 2)
        self.assertEqual(len(self.rows("archive.message_snapshots")), 3)

    def test_rating_has_immutable_history_and_remains_on_closed_round(self):
        closed = START+timedelta(minutes=1)
        self.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"), event(3, 2),
            event(4, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                  content_attributes={"ics_round_id": "round-from-ics", "ics_closed_at": closed.isoformat()}),
            event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat",
                  csat_survey_response={"rating": 5, "message_id": 500}))
        self.drain()
        rounds = sorted(self.rows("ops.round_index"), key=lambda row: row["round_no"])
        self.assertEqual([row["rating_status"] for row in rounds], ["rated", "not_invited"])
        self.assertEqual(len(self.rows("archive.rating_snapshots")), 2)
        self.assertEqual(self.rows("ops.rating_index")[0]["round_id"], rounds[0]["round_id"])
        detail = self.reader.round_detail(rounds[0]["round_id"])
        invitation = next(message for message in detail["messages"] if message["message_id"] == 500)
        self.assertEqual(invitation["visitor_rating"]["rating"], 5)
        self.assertEqual(invitation["visitor_rating"]["round_id"], rounds[0]["round_id"])
        history = self.reader.customer_history(rounds[0]["customer_id"])
        self.assertEqual(sum(message["visitor_rating"] is not None for message in history["messages"]), 1)

    def test_message_ratings_refresh_independently_and_preserve_original_invitation(self):
        attrs = {"ics_closed_at": (START + timedelta(minutes=1)).isoformat()}
        self.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
            event(3, 2), event(4, 3, message_id=500, sender="agent_bot", direction="outgoing",
                content_type="input_csat", content="请评价本次服务", content_attributes=attrs))
        self.drain()
        customer = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))["items"][0]
        history = self.reader.customer_history(customer["customer_id"])
        invitation = next(message for message in history["messages"] if message["message_id"] == 500)
        self.assertIsNone(invitation["visitor_rating"]["rating"])
        self.assertEqual(invitation["visitor_rating"]["status"], "pending")
        self.receive(event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat",
            updated_at=(START + timedelta(minutes=4)).isoformat(),
            content="修改后的邀请", content_attributes={**attrs, "submitted_values": {
                "csat_survey_response": {"rating": 2, "feedback_message": "等候太久"}}}),
            event(6, 10, conversation_id=22, message_id=600),
            event(7, 11, event_type="conversation_status_changed", conversation_id=22, status="resolved"),
            event(8, 12, conversation_id=22, message_id=601, sender="agent_bot", direction="outgoing",
                content_type="input_csat", content_attributes={"ics_closed_at": (START + timedelta(minutes=11)).isoformat()}),
            event(9, 13, event_type="message_updated", conversation_id=22, message_id=601, content_type="input_csat",
                csat_survey_response={"rating": 5, "feedback_message": "已解决"}))
        self.drain()
        history = self.reader.customer_history(customer["customer_id"])
        ratings = {message["message_id"]: message["visitor_rating"] for message in history["messages"] if message["visitor_rating"]}
        self.assertEqual(set(ratings), {500, 601})
        self.assertEqual((ratings[500]["rating"], ratings[500]["feedback"]), (2, "等候太久"))
        self.assertEqual((ratings[601]["rating"], ratings[601]["feedback"]), (5, "已解决"))
        self.assertEqual(ratings[500]["rating_at"], (START + timedelta(minutes=4)).isoformat())
        self.receive(event(10, 15, event_type="message_updated", message_id=500, content_type="input_csat",
            content_attributes={"submitted_values": {"csat_survey_response": {"rating": 1, "feedback_message": ""}}}))
        self.drain()
        messages = self.reader.customer_history(customer["customer_id"])["messages"]
        invitation = next(message for message in messages if message["message_id"] == 500)
        self.assertEqual((invitation["visitor_rating"]["rating"], invitation["visitor_rating"]["feedback"]), (1, ""))
        versions = self.reader.message_versions(self.source, 1, 500)["items"]
        original = next(snapshot for snapshot in versions if snapshot["id"] == invitation["original_snapshot_id"])
        self.assertEqual(original["content"], "请评价本次服务")
        self.assertEqual(next(message for message in messages if message["message_id"] == 601)["visitor_rating"]["rating"], 5)

    def test_runtime_roles_preserve_original_evidence_and_reader_is_read_only(self):
        self.receive(event(1))
        self.drain()
        with self.processor.pool.connection() as connection:
            for statement in ("UPDATE archive.events SET event_type='overwritten' WHERE source_key=%s",
                              "DELETE FROM archive.message_snapshots WHERE source_key=%s",
                              "DELETE FROM archive.rating_snapshots WHERE source_key=%s"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with connection.transaction():
                        connection.execute(statement, (self.source,))
        with self.admin.transaction():
            self.admin.execute("SET LOCAL ROLE archive_reader")
            self.assertEqual(len(self.rows("ops.round_index")), 1)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.admin.transaction():
                    self.admin.execute("UPDATE ops.round_index SET status='unknown' WHERE source_key=%s", (self.source,))

    def test_rebuild_recovers_nested_ratings_from_legacy_snapshots_without_mutating_evidence(self):
        from quality.processing.projector import project_conversation

        closed = START + timedelta(minutes=1)
        attrs = {"ics_round_id": "business-round-with-distinct-identifier",
                 "ics_closed_at": (closed + timedelta(microseconds=1)).isoformat()}
        self.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
                     event(3, 2),
                     event(4, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                           content_attributes=attrs),
                     event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat",
                           updated_at=(START + timedelta(minutes=4)).isoformat(),
                           content_attributes={**attrs, "submitted_values": {
                               "csat_survey_response": {"rating": 2, "feedback_message": "first feedback"}}}),
                     event(6, 5, event_type="message_updated", message_id=500, content_type="input_csat",
                           updated_at=(START + timedelta(minutes=5)).isoformat(),
                           content_attributes={**attrs, "submitted_values": {
                               "csat_survey_response": {"rating": 5, "feedback_message": "=latest feedback"}}}))

        def legacy_projection(events, *args, **kwargs):
            old_events = deepcopy(events)
            for item in old_events:
                item["payload"].get("content_attributes", {}).pop("submitted_values", None)
            return project_conversation(old_events, *args, **kwargs)

        with patch("quality.processing.repository.project_conversation", side_effect=legacy_projection):
            self.drain()
        self.assertTrue(all(row["rating"] is None for row in self.rows("archive.rating_snapshots")))
        tables = ("archive.events", "archive.message_snapshots", "archive.conversation_snapshots", "archive.rating_snapshots")
        evidence_before = {table: self.rows(table) for table in tables}
        self.assertEqual(len(self.processor.rebuild(self.source).completed), 1)
        for table in tables:
            self.assertEqual(self.rows(table), evidence_before[table], table)
        filters = ArchiveFilters(source_key=self.source, account_id=1, rating_status="rated", rating=5)
        rated = self.reader.list_rounds(filters)
        self.assertEqual(rated["total"], 1)
        row = rated["items"][0]
        self.assertEqual((row["round_no"], row["rating"], row["rating_feedback"]), (1, 5, "=latest feedback"))
        self.assertEqual(self.reader.list_customers(filters)["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1, round_no=2))["items"][0]["rating_status"], "not_invited")
        detail = self.reader.round_detail(row["round_id"])
        self.assertEqual(detail["round"]["rating_feedback"], "=latest feedback")
        invitation = next(message for message in detail["messages"] if message["message_id"] == 500)
        self.assertEqual((invitation["visitor_rating"]["rating"], invitation["visitor_rating"]["feedback"]), (5, "=latest feedback"))
        stored = self.rows("ops.rating_index")[0]
        self.assertEqual((stored["rating"], stored["feedback"], stored["round_id"]), (5, "=latest feedback", row["round_id"]))

    def test_customer_round_sequence_spans_conversations_while_round_no_stays_per_conversation(self):
        # 同一用户三次咨询：会话12首轮、会话22首轮、会话12超过6小时后的第二轮。
        self.receive(event(1, 0, message_id=100), event(2, 10, message_id=200, conversation_id=22),
                     event(3, 400, message_id=101))
        self.drain()
        filters = ArchiveFilters(source_key=self.source, account_id=1)
        expected = {100: (12, 1, 1), 200: (22, 1, 2), 101: (12, 2, 3)}
        rounds = self.reader.list_rounds(filters)["items"]
        self.assertEqual(sorted((r["conversation_id"], r["round_no"], r["customer_round_seq"]) for r in rounds),
                         sorted(expected.values()))
        # 列表筛选只选中一轮时，累计次序仍按该用户全部轮次计算。
        only = self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1, conversation_id=22))["items"]
        self.assertEqual([(r["round_no"], r["customer_round_seq"]) for r in only], [(1, 2)])
        customer = self.reader.list_customers(filters)["items"][0]
        self.assertEqual(customer["latest_round"]["customer_round_seq"], 3)
        directory = self.reader.customer_rounds(customer["customer_id"])["items"]
        self.assertEqual([r["customer_round_seq"] for r in directory], [3, 2, 1])
        detail = self.reader.round_detail(only[0]["round_id"])
        self.assertEqual(detail["round"]["customer_round_seq"], 2)
        history = self.reader.customer_history(customer["customer_id"])["messages"]
        self.assertEqual({m["message_id"]: (m["conversation_id"], m["round_no"], m["customer_round_seq"]) for m in history},
                         expected)
        from quality.offline.repository import messages
        exported = list(messages({"mode": "customers", "filters": {"source_key": self.source, "account_id": 1}}, lambda: None))
        # 导出流还包含搜索用的历史正文分块，轮次序号仅属于普通消息。
        versions = [m for m in exported if m.get("_type") == "search_version"]
        self.assertEqual({m["message_id"] for m in versions}, {"100", "101", "200"})
        exported = [m for m in exported if m.get("_type") != "search_version"]
        self.assertEqual([(m["message_id"], m["customer_round_seq"]) for m in exported], [("100", 1), ("101", 3), ("200", 2)])
        self.assertTrue(all("T" in m["sent_at"] for m in exported))

    def test_precomputed_customer_fields_blacklist_state_and_fact_queue_stay_consistent(self):
        """R2（迁移 020）：预存字段始终等于它们汇总的明细，客户重归属、删除消息后也一样。"""
        self.receive(event(1, 0, message_id=100), event(2, 10, message_id=200, conversation_id=22),
                     event(3, 400, message_id=101), event(4, 401, message_id=102, conversation_id=22))
        self.drain()

        def check():
            for row in self.admin.execute("""
                SELECT c.message_total,c.round_total,c.message_since,
                    (SELECT count(*) FROM ops.message_index m WHERE m.customer_id=c.customer_id) AS messages,
                    (SELECT count(*) FROM ops.round_index r WHERE r.customer_id=c.customer_id) AS rounds,
                    (SELECT min(COALESCE(m.sent_at,'1970-01-01 00:00:00+00')) FROM ops.message_index m
                        WHERE m.customer_id=c.customer_id) AS since
                FROM ops.customer_index c WHERE c.source_key=%s""", (self.source,)).fetchall():
                self.assertEqual((row["message_total"], row["round_total"], row["message_since"]),
                                 (row["messages"], row["rounds"], row["since"]))
            seq = self.admin.execute("""
                SELECT s.seq,s.customer_id=r.customer_id AS same,
                    row_number() OVER (PARTITION BY r.customer_id ORDER BY r.started_at,r.round_id) AS expected
                FROM ops.round_index r LEFT JOIN ops.customer_round_seq s USING(round_id)
                WHERE r.source_key=%s""", (self.source,)).fetchall()
            self.assertTrue(seq and all(row["seq"] == row["expected"] and row["same"] for row in seq))
            # Every round has stored facts or is queued for them.
            self.assertEqual(self.admin.execute("""SELECT count(*) AS n FROM ops.round_index r WHERE r.source_key=%s
                AND NOT EXISTS (SELECT 1 FROM ops.round_service_facts f WHERE f.round_id=r.round_id)
                AND NOT EXISTS (SELECT 1 FROM ops.round_fact_pending p WHERE p.round_id=r.round_id)""",
                (self.source,)).fetchone()["n"], 0)

        check()
        customer = self.admin.execute("SELECT customer_id,contact_id FROM ops.customer_index WHERE source_key=%s",
                                      (self.source,)).fetchone()
        other = f"customer:{self.source}:merged"
        self.admin.execute("""INSERT INTO ops.customer_index (customer_id,source_key,account_id,contact_id,display_name)
            VALUES (%s,%s,1,'777','另一位')""", (other, self.source))
        for table in ("round_index", "message_index"):
            self.admin.execute(f"UPDATE ops.{table} SET customer_id=%s WHERE source_key=%s AND conversation_id=22",
                               (other, self.source))
        check()
        self.admin.execute("DELETE FROM ops.message_index WHERE source_key=%s AND message_id=101", (self.source,))
        self.admin.execute("DELETE FROM ops.round_service_facts WHERE source_key=%s", (self.source,))
        check()
        # A deleted round leaves no queue entry behind.
        gone = self.admin.execute("""SELECT round_id FROM ops.round_index WHERE source_key=%s AND conversation_id=22
            ORDER BY started_at DESC LIMIT 1""", (self.source,)).fetchone()["round_id"]
        self.admin.execute("DELETE FROM ops.message_index WHERE round_id=%s", (gone,))
        self.admin.execute("DELETE FROM ops.round_index WHERE round_id=%s", (gone,))
        self.assertIsNone(self.admin.execute("SELECT 1 FROM ops.round_fact_pending WHERE round_id=%s", (gone,)).fetchone())
        check()
        now = datetime.now(timezone.utc)
        listed = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1, as_of=now))
        self.assertEqual(sorted((c["customer_id"], c["round_count"], c["message_count"]) for c in listed["items"]),
                         sorted([(customer["customer_id"], 2, 1), (other, 1, 1)]))  # message 101 deleted (round stays); a round of 22 deleted
        # Locked before the second round: counts and the customer list only see earlier rows.
        early = START + timedelta(minutes=5)
        listed = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1, as_of=early))
        self.assertEqual([(c["customer_id"], c["round_count"], c["message_count"]) for c in listed["items"]],
                         [(customer["customer_id"], 1, 1)])
        # Blacklist: the latest event per contact decides "currently blocked".
        rows = [("1", customer["contact_id"], "block", False, now + timedelta(hours=2)),
                ("2", customer["contact_id"], "unblock", False, None),
                ("3", "777", "block", True, None)]
        for seq, contact, action, permanent, until in rows:
            self.admin.execute("""INSERT INTO archive.contact_block_events (source_key,account_id,event_key,source_seq,
                contact_id,action,source,blocked_until,permanent,occurred_at,payload)
                VALUES (%s,1,%s,%s,%s,%s,'client',%s,%s,%s,'{}')""",
                (self.source, f"r2-{seq}", seq, contact, action, until, permanent, now))
        state = {row["contact_id"]: row["action"] for row in self.admin.execute(
            "SELECT contact_id,action FROM ops.contact_block_state WHERE source_key=%s", (self.source,))}
        self.assertEqual(state, {customer["contact_id"]: "unblock", "777": "block"})
        self.reader.ping()
        for mode, expected in (("ever", [customer["customer_id"], other]), ("active", [other])):
            items = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1, blacklist=mode))
            self.assertEqual(sorted(c["customer_id"] for c in items["items"]), sorted(expected), mode)
        flags = {c["customer_id"]: c.get("blacklist_status") for c in
                 self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))["items"]}
        self.assertEqual(flags, {customer["customer_id"]: "past", other: "active"})

    def test_reader_customer_history_spans_conversations_and_contains_unassigned_messages(self):
        missing = event(1, message_id=100)
        missing["payload"].pop("created_at")
        self.receive(missing, event(2, 1, message_id=101), event(3, 2, message_id=102, conversation_id=22))
        self.drain()
        customers = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))
        self.assertEqual(customers["total"], 1)
        customer = customers["items"][0]
        self.assertEqual(customer["round_count"], 2)
        first = self.reader.customer_history(customer["customer_id"], page=1, page_size=2)
        second = self.reader.customer_history(customer["customer_id"], page=2, page_size=2)
        self.assertEqual(first["total"], 3)
        messages = first["messages"] + second["messages"]
        self.assertEqual({item["conversation_id"] for item in messages}, {12, 22})
        self.assertEqual({item["message_id"] for item in messages}, {100, 101, 102})
        unassigned = next(item for item in messages if item["message_id"] == 100)
        self.assertIsNone(unassigned["round_id"])
        self.assertIsNone(unassigned["sent_at"])

    def test_customer_history_exposes_chatwoot_contact_profile_and_backfills_old_projection(self):
        incoming = event(1)
        profile = {"id": 42, "type": "contact", "name": "客户甲", "identifier": "h5-user-42",
                   "phone_number": "+86 13800000000",
                   "additional_attributes": {"description": "VIP 联系人备注"}}
        incoming["payload"]["sender"] = dict(profile)
        incoming["payload"]["conversation"]["meta"]["sender"] = dict(profile)
        self.receive(incoming)
        self.drain()
        customer = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))["items"][0]
        history = self.reader.customer_history(customer["customer_id"])
        expected = {"h5_user_id": "h5-user-42", "chatwoot_user_id": "42", "phone_number": "+86 13800000000", "contact_note": "VIP 联系人备注"}
        for key, value in expected.items():
            self.assertEqual(history["customer"][key], value)
        round_id = self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1))["items"][0]["round_id"]
        detail = self.reader.round_detail(round_id)
        for key, value in expected.items():
            self.assertEqual(detail["round"][key], value)
        # Simulate a pre-profile projection. The read path can recover contact
        # information from immutable snapshots without a migration or replay.
        self.admin.execute("UPDATE ops.customer_index SET data=data-'h5_user_id'-'chatwoot_user_id'-'phone_number'-'contact_note' WHERE customer_id=%s",
                           (customer["customer_id"],))
        recovered = self.reader.customer_history(customer["customer_id"])["customer"]
        for key, value in expected.items():
            self.assertEqual(recovered[key], value)

    def test_user_search_matches_h5_id_without_verified_business_identity(self):
        for index, identity in enumerate((
            {"identifier": "85704084"}, {"userId": 85704084},
            {"custom_attributes": {"userId": "85704084"}},
        )):
            incoming = event(index + 1, conversation_id=100 + index)
            contact = {"id": 42 + index, "name": "H5 客户", "type": "contact", **identity}
            incoming["payload"]["sender"] = dict(contact)
            incoming["payload"]["conversation"]["meta"]["sender"] = dict(contact)
            self.receive(incoming)
        other = event(4, conversation_id=200)
        other_contact = {"id": 99, "name": "其他客户", "type": "contact", "identifier": "99999999"}
        other["payload"]["sender"] = dict(other_contact)
        other["payload"]["conversation"]["meta"]["sender"] = dict(other_contact)
        self.receive(other)
        self.drain()

        base = {"source_key": self.source, "account_id": 1}
        for query, expected in (("85704084", {"42", "43", "44"}), ("7040", {"42", "43", "44"}),
                                ("H5 客户", {"42", "43", "44"}), ("42", {"42"}), ("no-such-user", set())):
            with self.subTest(query=query):
                filters = ArchiveFilters(**base, customer_query=query)
                customers = self.reader.list_customers(filters)
                rounds = self.reader.list_rounds(filters)
                self.assertEqual({item["contact_id"] for item in customers["items"]}, expected)
                self.assertEqual({item["contact_id"] for item in rounds["items"]}, expected)
                self.assertEqual(customers["total"], len(expected))
                self.assertEqual(rounds["total"], len(expected))
                self.assertEqual(self.reader.overview(filters)["total_rounds"], len(expected))
                self.assertTrue(all(item["business_user_id"] is None for item in customers["items"]))

    def test_user_search_recovers_h5_id_from_pre_profile_customer_index(self):
        incoming = event(1)
        incoming["payload"]["conversation"]["meta"]["sender"]["identifier"] = "85704084"
        self.receive(incoming)
        self.drain()
        self.admin.execute("UPDATE ops.customer_index SET data=data-'h5_user_id' WHERE source_key=%s", (self.source,))

        filters = ArchiveFilters(source_key=self.source, account_id=1, customer_query="85704084")
        self.assertEqual(self.reader.list_customers(filters)["total"], 1)
        self.assertEqual(self.reader.list_rounds(filters)["total"], 1)
        self.assertEqual(self.reader.overview(filters)["total_rounds"], 1)

    def test_user_search_treats_h5_id_wildcards_as_literal_text(self):
        for index, identifier in enumerate(("app_100%ID", "appX100ZID")):
            incoming = event(index + 1, conversation_id=100 + index)
            contact = {"id": 42 + index, "name": "客户", "type": "contact", "identifier": identifier}
            incoming["payload"]["sender"] = dict(contact)
            incoming["payload"]["conversation"]["meta"]["sender"] = dict(contact)
            self.receive(incoming)
        self.drain()

        for query in ("app_100%id", "100%", "app_"):
            with self.subTest(query=query):
                filters = ArchiveFilters(source_key=self.source, account_id=1, customer_query=query)
                self.assertEqual(self.reader.list_customers(filters)["total"], 1)
                rounds = self.reader.list_rounds(filters)
                self.assertEqual([item["contact_id"] for item in rounds["items"]], ["42"])
                self.assertEqual(self.reader.overview(filters)["total_rounds"], 1)

    def test_customer_card_metrics_use_logical_messages_handoff_and_original_round_rating(self):
        attrs = {"ics_round_id": "metrics-round", "ics_closed_at": (START + timedelta(minutes=12, microseconds=1)).isoformat()}
        self.receive(event(1), event(2, .5, sender="agent_bot", direction="outgoing"),
            event(3, 1, event_type="conversation_updated", custom_attributes={"handoff_requested": True}),
            event(4, 1.5), event(5, 2), agent_reply(6, 3), agent_reply(7, 3.5),
            event(8, 4), event(9, 5), event(10, 5.5, sender="agent_bot", direction="outgoing"),
            agent_reply(11, 7), event(12, 8), agent_reply(13, 9, private=True),
            event(14, 12, event_type="conversation_status_changed", status="resolved"),
            event(15, 13, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat", content_attributes=attrs),
            event(16, 14, event_type="message_updated", message_id=500, content_type="input_csat",
                updated_at=(START + timedelta(minutes=14)).isoformat(),
                content_attributes={**attrs, "submitted_values": {"csat_survey_response": {
                    "rating": 5, "feedback_message": "helpful service"}}}),
            event(17, 15, event_type="message_updated", message_id=6, sender="user", direction="outgoing",
                created_at=(START + timedelta(minutes=3)).isoformat(),
                updated_at=(START + timedelta(minutes=15)).isoformat(), content="edited response"))
        self.receive_assignment("7", minute=1)
        self.drain()
        customer = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))["items"][0]
        latest = customer["latest_round"]
        metrics = latest["service_metrics"]
        self.assertEqual(customer["round_count"], 1)
        self.assertEqual((latest["conversation_id"], latest["round_no"], latest["rating"], latest["rating_feedback"]),
                         (12, 1, 5, "helpful service"))
        # Timings are the statistics figures (R3): every wait starts at the last
        # message of a customer burst after the transfer (2 -> 3, 5 -> 7).
        self.assertEqual(metrics["first_response_seconds"], 60)
        self.assertEqual((metrics["average_response_seconds"], metrics["response_sample_count"]), (90, 2))
        self.assertEqual(latest["statistics"]["responsible_agent_id"], "7")
        self.assertEqual((metrics["human_phase_message_count"], metrics["human_phase_customer_count"], metrics["human_phase_agent_count"]), (8, 5, 3))
        self.assertIs(metrics["last_message_by_agent"], False)
        self.assertEqual(metrics["last_message_sender_role"], "contact")
        self.assertEqual(metrics["duration_seconds"], 720)

    def test_filtered_customer_card_counts_and_metrics_follow_matching_rounds(self):
        self.receive(event(1, content="old-round-keyword"), agent_reply(2, 1),
                     event(3, 2, event_type="conversation_status_changed", status="resolved"),
                     event(4, 3, conversation_id=22),
                     event(5, 4, conversation_id=22, sender="agent_bot", direction="outgoing"))
        self.drain()
        selected = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1, keyword="old-round-keyword"))
        self.assertEqual(selected["total"], 1)
        customer = selected["items"][0]
        self.assertTrue(customer["filter_scoped"])
        self.assertEqual((customer["round_count"], customer["message_count"], customer["total_round_count"]), (1, 2, 2))
        self.assertEqual((customer["latest_round"]["conversation_id"], customer["latest_round"]["round_no"]), (12, 1))
        # 不带轮次级条件时仍是完整历史，卡片指标覆盖最近一轮。
        selected = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))
        customer = selected["items"][0]
        self.assertFalse(customer["filter_scoped"])
        self.assertEqual((customer["round_count"], customer["message_count"]), (2, 4))
        latest = customer["latest_round"]
        self.assertEqual((latest["conversation_id"], latest["round_no"]), (22, 1))
        metrics = latest["service_metrics"]
        self.assertEqual(metrics["first_response_status"], "not_handed_off")
        self.assertEqual(metrics["human_phase_message_count"], 0)
        self.assertEqual(metrics["last_message_sender_role"], "bot")
        self.assertEqual(metrics["duration_seconds"], 60)

    def test_customer_card_metrics_cover_messages_beyond_detail_pagination(self):
        self.receive(event(1), event(2, 1, event_type="conversation_updated", custom_attributes={"handoff_requested": True}),
                     *(agent_reply(index + 3, 2 + index/1000) for index in range(105)))
        self.receive_assignment("7", minute=1)
        self.drain()
        customer = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))["items"][0]
        metrics = customer["latest_round"]["service_metrics"]
        self.assertEqual(metrics["human_phase_agent_count"], 105)
        self.assertEqual(metrics["human_phase_message_count"], 105)
        # No customer message after the transfer: the seat spoke first, no response sample.
        self.assertEqual(metrics["response_sample_count"], 0)
        self.assertIsNone(metrics["first_response_seconds"])
        self.assertEqual(metrics["first_response_status"], "not_counted")
        self.assertIs(metrics["last_message_by_agent"], True)
        self.assertEqual(customer["message_count"], 106)

    def test_customer_round_directory_is_complete_paginated_and_scoped(self):
        expected = {self.overview_round(f"directory-{index:03}") for index in range(55)}
        self.overview_round("other-customer", customer="99")
        self.overview_round("other-source", source=self.source + "-other")
        customer_id = f"overview-customer:{self.source}:1:42"
        first = self.reader.customer_rounds(customer_id, page=1, page_size=50)
        second = self.reader.customer_rounds(customer_id, page=2, page_size=50)
        self.assertEqual((first["total"], second["total"]), (55, 55))
        self.assertEqual((len(first["items"]), len(second["items"])), (50, 5))
        actual = [row["round_id"] for row in first["items"] + second["items"]]
        self.assertEqual(set(actual), expected)
        self.assertEqual(actual, sorted(expected, reverse=True))
        self.assertTrue(all(row["customer_id"] == customer_id for row in first["items"] + second["items"]))
        self.assertEqual(self.reader.customer_rounds(customer_id, page=3)["items"], [])
        self.assertIsNone(self.reader.customer_rounds("nonexistent-customer"))

    def test_reader_keyword_matches_original_edit_delete_versions_and_literal_wildcards(self):
        self.receive(event(1, message_id=100, content="原文 100%_literal"),
            event(2, 1, event_type="message_updated", message_id=100, content="改后的正文", updated_at=(START+timedelta(minutes=1)).isoformat()),
            event(3, 2, event_type="message_updated", message_id=100, content=None, content_attributes={"deleted": True},
                  updated_at=(START+timedelta(minutes=2)).isoformat()))
        self.drain()
        for keyword in ("原文", "改后的正文", "100%_literal"):
            result = self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1, keyword=keyword))
            self.assertEqual(result["total"], 1, keyword)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, keyword="100ZZliteral"))["total"], 0)
        versions = self.reader.message_versions(self.source, 1, 100, page=1, page_size=2)
        self.assertEqual(versions["total"], 3)
        self.assertEqual(versions["items"][0]["content"], "原文 100%_literal")
        self.assertEqual(self.reader.message_versions("other-source", 1, 100)["total"], 0)

    def test_customer_mode_seat_filter_hides_other_seats_rounds_in_directory_history_and_export(self):
        self.receive(event(1, content="seat seven"), agent_reply(2, 1, agent_id=7, agent_name="七号"),
                     event(3, 2, event_type="conversation_status_changed", status="resolved"),
                     event(4, 3, conversation_id=22, content="seat nine"),
                     agent_reply(5, 4, conversation_id=22, agent_id=9, agent_name="九号"),
                     event(6, 5, conversation_id=32, content="seat seven again"),
                     agent_reply(7, 6, conversation_id=32, agent_id=7, agent_name="七号"))
        self.drain()
        filters = ArchiveFilters(source_key=self.source, account_id=1, agent_id="7", agent_scope="replied")
        listed = self.reader.list_customers(filters)
        self.assertEqual(listed["total"], 1)
        customer = listed["items"][0]
        self.assertEqual((customer["round_count"], customer["message_count"], customer["total_round_count"]), (2, 4, 3))
        self.assertEqual(customer["latest_round"]["conversation_id"], 32)
        directory = self.reader.customer_rounds(customer["customer_id"], filters=filters)
        self.assertTrue(directory["filter_scoped"])
        self.assertEqual(directory["total"], 2)
        self.assertEqual({item["conversation_id"] for item in directory["items"]}, {12, 32})
        history = self.reader.customer_history(customer["customer_id"], filters=filters)
        self.assertTrue(history["filter_scoped"])
        self.assertEqual(history["total"], 4)
        self.assertEqual({message["conversation_id"] for message in history["messages"]}, {12, 32})
        self.assertEqual(history["customer"]["round_count"], 2)
        paged = self.reader.customer_history(customer["customer_id"], page=2, page_size=3, filters=filters)
        self.assertEqual((paged["total"], len(paged["messages"])), (4, 1))
        # 未带筛选（按轮次视图的“用户全史”）仍是完整历史。
        self.assertEqual(self.reader.customer_rounds(customer["customer_id"])["total"], 3)
        self.assertEqual(self.reader.customer_history(customer["customer_id"])["total"], 6)
        from quality.query.config import QuerySettings
        from quality.query.export import ExportRepository
        from openpyxl import load_workbook
        exporter = ExportRepository(QuerySettings(_env_file=None, query_enabled=True,
            reader_password=os.environ["ARCHIVE_READER_PASSWORD"], postgres_host=self.settings.postgres_host,
            postgres_port=self.settings.postgres_port, postgres_db=self.settings.postgres_db,
            postgres_sslmode=self.settings.postgres_sslmode))
        exporter.open()
        self.addCleanup(exporter.close)
        for scope in ({}, {"customer_id": customer["customer_id"]}):
            artifact = exporter.create_export("customers", filters, **scope)
            self.addCleanup(artifact.cleanup)
            workbook = load_workbook(artifact.path)
            try:
                values = list(workbook.active.values)
                rows = [dict(zip(values[0], row)) for row in values[1:]]
                self.assertEqual({row["会话ID"] for row in rows}, {"12", "32"}, scope)
            finally:
                workbook.close()

    def test_customer_keyword_filter_still_finds_unrounded_evidence_without_other_rounds(self):
        self.receive(event(1, content="ordinary round"), agent_reply(2, 1))
        unknown = event(3, 2, conversation_id=22, content="partial-only evidence")
        unknown["payload"].pop("created_at")
        self.receive(unknown)
        self.drain()
        filters = ArchiveFilters(source_key=self.source, account_id=1, keyword="partial-only")
        listed = self.reader.list_customers(filters)
        self.assertEqual(listed["total"], 1)
        customer_id = listed["items"][0]["customer_id"]
        history = self.reader.customer_history(customer_id, filters=filters)
        self.assertEqual([message["content"] for message in history["messages"]], ["partial-only evidence"])
        self.assertEqual(self.reader.customer_rounds(customer_id, filters=filters)["total"], 0)

    def test_reader_seat_filters_distinguish_reply_assignment_and_current_with_source_scope(self):
        self.receive(event(1), event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "负责人"}),
                     event(3, 2, sender="user", direction="outgoing"))
        other_source = self.source + "-other"
        self.capture.source_key = other_source
        try:
            self.receive(event(1), event(2, 1, sender="user", direction="outgoing"))
        finally:
            self.capture.source_key = self.source
        account_two = [event(1), event(2, 1, sender="user", direction="outgoing")]
        for item in account_two:
            item["payload"]["account"]["id"] = 2
            item["payload"]["conversation"]["account_id"] = 2
        self.receive(*account_two)
        self.drain()
        def seats(agent_id, scope, source=None):
            return self.reader.list_rounds(ArchiveFilters(source_key=source or self.source, account_id=1,
                agent_id=agent_id, agent_scope=scope))
        self.assertEqual(seats("7", "replied")["total"], 1)
        self.assertEqual(seats("7", "assigned")["total"], 0)
        self.assertEqual(seats("8", "assigned")["total"], 1)
        self.assertEqual(seats("8", "current")["total"], 1)
        self.assertEqual(seats("8", "current", other_source)["total"], 0)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=2,
            agent_id="7", agent_scope="replied"))["total"], 1)
        self.assertTrue(all(item["source_key"] == self.source for item in seats("7", "replied")["items"]))
        with self.assertRaises(ValueError):
            self.reader.list_rounds(ArchiveFilters(agent_id="7"))

    def test_legacy_reply_based_responsibility_is_excluded_until_reprojected(self):
        self.receive(event(1), agent_reply(2, 1, agent_id=9, agent_name="actual responder"))
        self.drain()
        original_events, original_snapshots = self.rows("archive.events"), self.rows("archive.message_snapshots")
        round_row = self.rows("ops.round_index")[0]
        legacy = {**round_row["data"], "responsibility_basis": "last_public_agent_reply",
            "responsible_agent_id": "9", "responsible_agent_name": "legacy reply owner",
            "responsibility_event_id": 123, "responsibility_external_round_id": "stale"}
        self.admin.execute("UPDATE ops.round_index SET data=%s WHERE round_id=%s", (Jsonb(legacy), round_row["round_id"]))
        base = {"source_key": self.source, "account_id": 1}
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, agent_id="9"))["total"], 0)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, agent_id="9"))["total"], 0)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, agent_id="9", agent_scope="replied"))["total"], 1)
        for result in (self.reader.list_rounds(ArchiveFilters(**base))["items"][0],
                self.reader.round_detail(round_row["round_id"])["round"]):
            self.assertEqual(result["responsibility_basis"], "assignment_unobserved")
            self.assertIsNone(result["responsible_agent_id"])
            self.assertIsNone(result["responsibility_event_id"])
            self.assertEqual(result["last_public_agent_id"], "9")
        self.assertEqual(self.rows("ops.round_index")[0]["data"], legacy)
        self.assertEqual(self.rows("archive.events"), original_events)
        self.assertEqual(self.rows("archive.message_snapshots"), original_snapshots)

    def test_reader_attachment_role_time_and_pagination_filters(self):
        self.receive(event(1, attachments=[{"id": 90, "file_name": "proof.png", "data_url": "https://example.invalid/proof"}]),
            event(2, 1, sender="user", direction="outgoing", private=True),
            event(3, 2, event_type="conversation_status_changed", status="resolved"), event(4, 3))
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        attached = self.reader.list_rounds(ArchiveFilters(**base, has_attachment=True, message_scope="private"))
        self.assertEqual(attached["total"], 0)
        attached = self.reader.list_rounds(ArchiveFilters(**base, has_attachment=True, message_scope="contact"))
        self.assertEqual(attached["total"], 1)
        detail = self.reader.round_detail(attached["items"][0]["round_id"], page_size=1)
        self.assertEqual(detail["total"], 2)
        self.assertEqual(detail["messages"][0]["attachments"][0]["filename"], "proof.png")
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, has_attachment=False))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, has_attachment=False, message_scope="private"))["total"], 1)
        resolved = self.reader.list_rounds(ArchiveFilters(**base, time_field="resolved_at", start=START+timedelta(minutes=2), end=START+timedelta(minutes=3)))
        self.assertEqual(resolved["total"], 1)
        outside = self.reader.list_rounds(ArchiveFilters(**base, time_field="message_at", start=START+timedelta(minutes=2), end=START+timedelta(minutes=3)))
        self.assertEqual(outside["total"], 0)
        page1 = self.reader.list_rounds(ArchiveFilters(**base), page=1, page_size=1)
        page2 = self.reader.list_rounds(ArchiveFilters(**base), page=2, page_size=1)
        self.assertEqual(page1["total"], 2)
        self.assertNotEqual(page1["items"][0]["round_id"], page2["items"][0]["round_id"])

    def test_responsible_seat_stays_with_ics_assignment_and_rebuild_preserves_real_replies(self):
        self.receive(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "assigned A"}),
            agent_reply(3, 2, agent_id=7, agent_name="reply B"))
        assignment_id = self.receive_assignment()
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        def selected(agent_id, **values):
            return self.reader.list_rounds(ArchiveFilters(**base, agent_id=agent_id, **values))
        first = selected("8", agent_scope="current")["items"][0]
        self.assertEqual((first["responsible_agent_id"], first["responsibility_event_id"], first["current_assignee_id"]), ("8", assignment_id, "8"))
        # Seat 7 answered the round ICS assigned to seat 8: assisted, no assessed seat.
        self.assertEqual((first["statistics"]["assigned_agent_id"], first["statistics"]["responsible_agent_id"],
                          first["statistics"]["assisted"]), ("8", None, True))
        self.assertEqual(selected("8")["total"], 0)
        self.assertEqual(selected("7")["total"], 0)
        self.assertEqual(selected("8", agent_scope="current")["total"], 1)
        self.receive(agent_reply(4, 3, agent_id=9, agent_name="reply C"),
            agent_reply(5, 4, agent_id=8, private=True),
            event(6, 5, sender="agent_bot", direction="outgoing"),
            event(7, 6, event_type="conversation_updated", assignee={"id": 10, "name": "assigned D"}),
            agent_reply(8, 7, message_id=3, event_type="message_updated", agent_id=7,
                        created_at=(START+timedelta(minutes=2)).isoformat(),
                        updated_at=(START+timedelta(minutes=7)).isoformat(), content="edited older reply"))
        self.drain()
        final = selected("7", agent_scope="replied")["items"][0]
        self.assertEqual(final["round_id"], first["round_id"])
        self.assertEqual((final["responsible_agent_id"], final["responsibility_event_id"]), ("8", assignment_id))
        self.assertEqual((final["last_public_agent_id"], final["last_public_agent_name"], final["last_public_message_id"]), ("9", "reply C", 4))
        self.assertEqual(final["responsibility_basis"], "ics_assignment")
        self.assertEqual(selected("7")["total"], 0)
        self.assertEqual(selected("7", agent_scope="replied")["total"], 1)
        self.assertEqual(selected("10")["total"], 0)
        self.assertEqual(selected("10", agent_scope="current")["total"], 1)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, agent_id="8"))["total"], 0)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, agent_id="9"))["total"], 0)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, agent_id="7"))["total"], 0)
        detail = self.reader.round_detail(final["round_id"])
        self.assertEqual(detail["round"]["responsibility_event_id"], assignment_id)
        actual = {row["message_id"]: row for row in detail["messages"]}
        self.assertEqual((actual[4]["sender_id"], actual[4]["content"]), ("9", "message 4"))
        self.assertEqual((actual[3]["sender_id"], actual[3]["content"]), ("7", "edited older reply"))
        original_events = self.rows("archive.events")
        original_snapshots = self.rows("archive.message_snapshots")
        self.assertEqual(len(self.processor.rebuild(self.source).completed), 1)
        self.assertEqual(selected("7", agent_scope="replied")["items"][0]["responsibility_event_id"], assignment_id)
        self.assertEqual(self.rows("archive.events"), original_events)
        self.assertEqual(self.rows("archive.message_snapshots"), original_snapshots)

    def test_responsible_seat_uses_ics_evidence_per_round_even_without_public_reply(self):
        self.receive(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "assigned"}),
            agent_reply(3, 2, agent_id=7),
            event(4, 3, event_type="conversation_status_changed", status="resolved"),
            event(5, 4, content="second round, no human reply"))
        self.receive_assignment()
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        rounds = sorted(self.reader.list_rounds(ArchiveFilters(**base))["items"], key=lambda row: row["round_no"])
        self.assertEqual([row["responsible_agent_id"] for row in rounds], ["8", None])
        self.assertEqual(rounds[1]["responsibility_basis"], "assignment_unobserved")
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, agent_id="8", agent_scope="current"))["total"], 2)
        for method in (self.reader.list_rounds, self.reader.list_customers):
            self.assertEqual(method(ArchiveFilters(**base, agent_id="8", round_no=2))["total"], 0)
        # Delayed source evidence binds to the trigger's original round.
        second_assignment = self.receive_assignment("9", trigger_message_id=5, minute=4.5,
            business_round="business-round-2", session_minute=4)
        self.drain()
        second = self.reader.list_rounds(ArchiveFilters(**base, agent_id="9"))["items"][0]
        self.assertEqual(second["round_no"], 2)
        self.assertEqual(second["responsibility_event_id"], second_assignment)
        self.assertEqual(second["reply_composition"], "none")
        self.receive(agent_reply(6, 5, agent_id=None, agent_name="unknown responder"))
        self.drain()
        unknown = self.reader.list_rounds(ArchiveFilters(**base, round_no=2))["items"][0]
        self.assertEqual(unknown["responsible_agent_id"], "9")
        self.assertEqual(unknown["responsibility_basis"], "ics_assignment")
        self.assertIsNone(unknown["last_public_agent_id"])
        self.assertEqual(unknown["last_public_message_id"], 6)

    def test_reader_current_seat_follows_conversation_reassignment_across_rounds(self):
        self.receive(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "previous owner"}),
            event(3, 2, sender="user", direction="outgoing"),
            event(4, 3, event_type="conversation_status_changed", status="resolved"), event(5, 4),
            event(6, 5, event_type="conversation_updated", assignee={"id": 9, "name": "current owner"}),
            event(7, 6, sender="user", direction="outgoing"))
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        rounds = self.reader.list_rounds(ArchiveFilters(**base, agent_id="9", agent_scope="current"))
        self.assertEqual(rounds["total"], 2)
        self.assertEqual({item["round_no"] for item in rounds["items"]}, {1, 2})
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, agent_id="8", agent_scope="current"))["total"], 0)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, agent_id="9", agent_scope="assigned"))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, agent_id="7", agent_scope="replied"))["total"], 2)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, agent_id="9", agent_scope="current", round_no=1))["total"], 1)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, agent_id="8", agent_scope="current"))["total"], 0)
        # Historical round detail retains its own last assignee for evidence.
        first = next(item for item in self.rows("ops.round_index") if item["round_no"] == 1)
        self.assertEqual(first["data"]["current_assignee_id"], "8")

    def test_reader_message_filters_intersect_within_the_same_message_conversation_and_round(self):
        attachment = [{"id": 90, "file_name": "proof.png", "data_url": "https://example.invalid/proof"}]
        self.receive(event(1, content="first proof", attachments=attachment),
            event(2, 1, sender="user", direction="outgoing", private=True, content="secret proof"),
            event(3, 2, event_type="conversation_status_changed", status="resolved"),
            event(4, 3, content="later proof"),
            event(5, 4, conversation_id=22, content="other proof", attachments=attachment))
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        for method in (self.reader.list_rounds, self.reader.list_customers):
            with self.subTest(query=method.__name__):
                self.assertEqual(method(ArchiveFilters(**base, keyword="secret", message_scope="private", has_attachment=True))["total"], 0)
                self.assertEqual(method(ArchiveFilters(**base, keyword="secret", message_scope="private", has_attachment=False))["total"], 1)
                # Absence applies to the eligible messages and still requires a match.
                self.assertEqual(method(ArchiveFilters(**base, message_scope="agent", has_attachment=False))["total"], 0)
                self.assertEqual(method(ArchiveFilters(**base, conversation_id=12, time_field="message_at",
                    start=START+timedelta(minutes=3), end=START+timedelta(minutes=4),
                    keyword="proof", message_scope="contact", has_attachment=True))["total"], 0)
                self.assertEqual(method(ArchiveFilters(**base, conversation_id=12, time_field="message_at",
                    start=START+timedelta(minutes=3), end=START+timedelta(minutes=4),
                    keyword="proof", message_scope="contact", has_attachment=False))["total"], 1)
                self.assertEqual(method(ArchiveFilters(**base, conversation_id=22, keyword="later", has_attachment=False))["total"], 0)
                self.assertEqual(method(ArchiveFilters(**base, round_no=1, keyword="first"))["total"], 1)
                self.assertEqual(method(ArchiveFilters(**base, round_no=2, keyword="first"))["total"], 0)
                self.assertEqual(method(ArchiveFilters(**base, conversation_id=12, round_no=2,
                    time_field="started_at", start=START+timedelta(minutes=3), end=START+timedelta(minutes=4)))["total"], 1)
                self.assertEqual(method(ArchiveFilters(**base, conversation_id=12, round_no=2,
                    time_field="started_at", end=START+timedelta(minutes=3)))["total"], 0)

    def test_reader_inboxes_are_scoped_and_keep_unassigned_customer_evidence(self):
        first = event(1, content="inbox three")
        second = event(2, 1, conversation_id=22, content="inbox four")
        second["payload"]["conversation"]["inbox_id"] = 4
        unknown = event(3, 2, conversation_id=32, content="partial inbox five")
        unknown["payload"]["conversation"]["inbox_id"] = 5
        unknown["payload"]["conversation"]["meta"]["assignee"] = {"id": 8, "name": "partial owner"}
        unknown["payload"].pop("created_at")
        self.receive(first, second, unknown)
        other_source = self.source + "-other"
        self.capture.source_key = other_source
        try:
            self.receive(event(1))
        finally:
            self.capture.source_key = self.source
        account_two = event(4, 3)
        account_two["payload"]["account"]["id"] = 2
        account_two["payload"]["conversation"]["account_id"] = 2
        self.receive(account_two)
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        rounds = self.reader.list_rounds(ArchiveFilters(**base, inbox_id=3))
        self.assertEqual(rounds["total"], 1)
        self.assertEqual(rounds["items"][0]["conversation_id"], 12)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, inbox_id=4))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, inbox_id=5))["total"], 0)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, inbox_id=5, keyword="partial"))["total"], 1)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, inbox_id=5, keyword="partial", agent_id="8", agent_scope="current"))["total"], 1)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, inbox_id=3, keyword="partial"))["total"], 0)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, inbox_id=4, channel_key="xingliao"))["total"], 0)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, inbox_id=5, channel_key="unmapped"))["total"], 1)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, inbox_id=3, channel_key="xingliao"))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=other_source, account_id=1, inbox_id=3))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=2, inbox_id=3))["total"], 1)
        options = self.reader.filter_options()
        inboxes = {(item["source_key"], item["account_id"], item["inbox_id"]): item for item in options["inboxes"]}
        self.assertEqual(inboxes[(self.source, 1, 3)]["channel_key"], "xingliao")
        self.assertEqual(inboxes[(self.source, 1, 5)]["name"], "#5")

    def test_reader_directory_lists_sources_before_projection_and_assignment_without_rounds(self):
        self.receive(event(1))
        self.assertIn({"source_key": self.source, "account_id": 1}, self.reader.filter_options()["sources"])
        self.receive(event(2, 1, conversation_id=22, event_type="conversation_updated", assignee={"id": 8, "name": "assigned"}))
        self.drain()
        options = self.reader.filter_options()
        agents = [item for item in options["agents"] if item["source_key"] == self.source]
        self.assertTrue(any(item["account_id"] == 1 and item["agent_id"] == "8" for item in agents))

    def test_reader_raw_event_returns_exact_bytes_with_scoped_access(self):
        raw = ('{ "event":"message_created", "id":1, "account":{"id":1}, '
               '"conversation":{"id":12,"account_id":1}, "created_at":"2026-10-01T08:00:00Z", '
               '"sender":{"id":42,"type":"contact"}, "message_type":"incoming", '
               '"precise":12345678901234567890.1234567890 }\n').encode()
        receipt = self.capture.receive(CaptureEnvelope.parse(raw))
        self.drain()
        self.assertEqual(self.reader.raw_event(receipt.event_id, self.source, 1), raw)
        self.assertIsNone(self.reader.raw_event(receipt.event_id, self.source, 2))
        self.assertIsNone(self.reader.raw_event(receipt.event_id, "another-source", 1))
        with self.reader.pool.connection() as connection:
            with self.assertRaises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
                with connection.transaction():
                    connection.execute("UPDATE ops.message_index SET content='changed' WHERE source_key=%s", (self.source,))

    def test_reader_pure_ai_filter_includes_active_rounds_and_unknown_stays_distinct(self):
        self.receive(event(1), event(2, 1, sender="agent_bot", direction="outgoing"),
            event(3, 2, conversation_id=22), event(4, 3, conversation_id=22, sender="agent_bot", direction="outgoing"),
            event(5, 4, conversation_id=22, event_type="conversation_status_changed", status="resolved"))
        self.drain()
        base = {"source_key": self.source, "account_id": 1}
        ai = self.reader.list_rounds(ArchiveFilters(**base, reception_type="ai_only"))
        unknown = self.reader.list_rounds(ArchiveFilters(**base, reception_type="unknown"))
        self.assertEqual(ai["total"], 2)
        self.assertEqual({item["conversation_id"] for item in ai["items"]}, {12, 22})
        self.assertEqual(unknown["total"], 0)
        active = self.reader.list_rounds(ArchiveFilters(**base, reception_type="ai_only", status="active"))
        self.assertEqual(active["total"], 1)
        self.assertEqual(active["items"][0]["conversation_id"], 12)
        self.assertFalse(active["items"][0]["classification_finalized"])
        self.assertTrue(next(item for item in ai["items"] if item["conversation_id"] == 22)["classification_finalized"])
        for conversation_id in (12, 22):
            self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, conversation_id=conversation_id, reception_type="ai_only"))["total"], 1)
            self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, conversation_id=conversation_id, reception_type="unknown"))["total"], 0)

    def test_reader_reception_changes_from_first_customer_to_human_without_duplicate_round_or_evidence_mutation(self):
        scenarios = ("explicit_request", "request_attribute", "confirmed_takeover", "public_reply")
        evidence_tables = ("archive.events", "archive.message_snapshots", "archive.conversation_snapshots", "archive.rating_snapshots")
        for index, scenario in enumerate(scenarios):
            with self.subTest(trigger=scenario):
                conversation_id = 1000 + index
                message_id = 100 + index * 10
                base = {"source_key": self.source, "account_id": 1, "conversation_id": conversation_id}
                self.receive(event(message_id, conversation_id=conversation_id, content="首次咨询，还没有任何回复"))
                self.drain()
                first = self.reader.list_rounds(ArchiveFilters(**base, reception_type="ai_only"))
                self.assertEqual(first["total"], 1)
                round_id = first["items"][0]["round_id"]
                self.assertEqual(first["items"][0]["reply_composition"], "none")
                self.assertEqual(self.reader.list_customers(ArchiveFilters(**base, reception_type="ai_only"))["total"], 1)
                self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, reception_type="human"))["total"], 0)
                self.assertEqual(self.reader.list_rounds(ArchiveFilters(**base, reception_type="unknown"))["total"], 0)
                if scenario == "explicit_request":
                    trigger = event(message_id+1, 1, conversation_id=conversation_id, content="转人工")
                elif scenario == "request_attribute":
                    trigger = event(message_id+1, 1, conversation_id=conversation_id, event_type="conversation_updated",
                                    custom_attributes={"handoff_requested": True})
                elif scenario == "confirmed_takeover":
                    trigger = event(message_id+1, 1, conversation_id=conversation_id, event_type="conversation_updated",
                                    custom_attributes={"handoff_accepted": True})
                else:
                    trigger = event(message_id+1, 1, conversation_id=conversation_id, sender="user", direction="outgoing", content="人工回复")
                self.receive(trigger)
                self.drain()

                def assert_human():
                    for method in (self.reader.list_rounds, self.reader.list_customers):
                        self.assertEqual(method(ArchiveFilters(**base, reception_type="ai_only"))["total"], 0)
                        self.assertEqual(method(ArchiveFilters(**base, reception_type="human"))["total"], 1)
                        self.assertEqual(method(ArchiveFilters(**base, reception_type="unknown"))["total"], 0)
                    rounds = self.reader.list_rounds(ArchiveFilters(**base))
                    self.assertEqual(rounds["total"], 1)
                    self.assertEqual(rounds["items"][0]["round_id"], round_id)
                    self.assertEqual(rounds["items"][0]["round_no"], 1)

                assert_human()
                # Later AI/customer activity must not reverse the classification.
                self.receive(event(message_id+2, 2, conversation_id=conversation_id, sender="agent_bot", direction="outgoing"),
                             event(message_id+3, 3, conversation_id=conversation_id, content="补充问题"))
                self.drain()
                assert_human()
                evidence_before = {table: self.rows(table) for table in evidence_tables}
                self.assertEqual(len(self.processor.rebuild(self.source).completed), index+1)
                assert_human()
                for table in evidence_tables:
                    self.assertEqual(self.rows(table), evidence_before[table], table)

    def test_reader_partial_history_search_and_empty_old_customer_suppression(self):
        unknown = event(1, content="partial-only evidence")
        unknown["payload"].pop("created_at")
        unknown["payload"]["sender"].pop("id")
        unknown["payload"]["conversation"].pop("meta")
        self.receive(unknown)
        self.drain()
        partial = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1, keyword="partial-only"))
        self.assertEqual(partial["total"], 1)
        old_id = partial["items"][0]["customer_id"]
        self.assertEqual(self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1,
            customer_query=old_id, keyword="partial-only"))["total"], 1)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=2,
            keyword="partial-only"))["total"], 0)
        self.receive(event(2, 1))
        self.drain()
        customers = self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1))
        self.assertEqual(customers["total"], 1)
        self.assertNotEqual(customers["items"][0]["customer_id"], old_id)
        self.assertEqual(self.reader.list_customers(ArchiveFilters(source_key=self.source, account_id=1,
            customer_query="客户甲", keyword="partial-only"))["total"], 1)
        self.assertEqual(self.reader.customer_history(customers["items"][0]["customer_id"])["total"], 2)

    def test_reader_unlinked_ratings_remain_visible_without_guessing_a_round(self):
        before = self.reader.processing_status()["unlinked_rating_count"]
        self.receive(event(1), event(2, 1, content_type="input_csat", sender="agent_bot", direction="outgoing",
            content_attributes={"ics_round_id": "unobserved-ics-round", "rating": 3}))
        self.drain()
        after = self.reader.processing_status()["unlinked_rating_count"]
        self.assertEqual(after, before + 1)
        ratings = self.reader.unlinked_ratings(page_size=1000)
        item = next(item for item in ratings["items"] if item["source_key"] == self.source)
        self.assertEqual(item["rating"], 3)
        self.assertIsNone(item["round_id"])
        self.assertEqual(self.rows("ops.round_index")[0]["rating_status"], "not_invited")
        customer_id = self.rows("ops.customer_index")[0]["customer_id"]
        invitation = next(message for message in self.reader.customer_history(customer_id)["messages"]
                          if message["visitor_rating"] is not None)
        self.assertEqual(invitation["visitor_rating"]["rating"], 3)
        self.assertIsNone(invitation["visitor_rating"]["round_id"])

    def test_byte_budget_rejects_projection_without_losing_captured_event(self):
        receipt = self.receive(event(1, content="x" * (1100 * 1024)))[0]
        limited = ProcessingRepository(self.settings.model_copy(update={
            "processor_max_conversation_bytes": 1024 * 1024, "processor_max_attempts": 1}))
        limited.open()
        self.addCleanup(limited.close)
        claim = limited.claim("limited-worker")
        self.assertEqual(claim["event_id"], receipt.event_id)
        with self.assertRaisesRegex(RuntimeError, "byte bound"):
            limited.process_claim(claim)
        limited._retry(claim, "RuntimeError")
        self.assertEqual(len(self.rows("archive.events")), 1)
        self.assertEqual(self.rows("archive.message_snapshots"), [])
        self.assertEqual(self.admin.execute("SELECT status FROM ops.event_jobs WHERE event_id=%s",
            (receipt.event_id,)).fetchone()["status"], "failed")

    def test_repeated_expired_lease_stops_at_max_attempts(self):
        receipt = self.receive(event(1))[0]
        claim = self.processor.claim("crashing-worker")
        self.assertEqual(claim["event_id"], receipt.event_id)
        self.admin.execute("""UPDATE ops.event_jobs SET attempts=%s,
            locked_until=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE event_id=%s""",
            (self.settings.processor_max_attempts, receipt.event_id))
        self.assertIsNone(self.processor.claim("replacement-worker"))
        row = self.admin.execute("SELECT status,locked_by FROM ops.event_jobs WHERE event_id=%s", (receipt.event_id,)).fetchone()
        self.assertEqual(row, {"status": "failed", "locked_by": None})
        self.assertEqual(len(self.processor.rebuild(self.source).completed), 1)
        self.assertEqual(self.admin.execute("SELECT status FROM ops.event_jobs WHERE event_id=%s",
            (receipt.event_id,)).fetchone()["status"], "completed")

    def test_parallel_conversations_preserve_shared_contact_global_first_and_last(self):
        self.receive(event(1), event(2, 50, conversation_id=22))
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(self.processor.process_next, ("contact-1", "contact-2")))
        self.drain()
        customers = self.rows("ops.customer_index")
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["last_message_at"], START + timedelta(minutes=50))
        self.assertEqual(customers[0]["data"]["first_message_at"], START.isoformat())
        self.assertEqual(len(self.rows("ops.round_index")), 2)

    def test_rebuild_repairs_untyped_customer_round_without_changing_existing_evidence(self):
        incoming = event(1, content="customer original")
        incoming["payload"]["sender"].pop("type")
        self.receive(incoming, event(2, 1, sender="agent_bot", direction="outgoing"))
        # Reproduce the old parser's completed jobs and immutable unknown-role snapshots.
        with patch("quality.processing.projector.sender_role", return_value="unknown"):
            self.drain()
        before_events = self.rows("archive.events")
        before_snapshots = self.rows("archive.message_snapshots")
        self.assertEqual(self.rows("ops.round_index"), [])
        self.assertEqual(len(self.processor.rebuild(self.source).completed), 1)
        self.assertEqual(self.rows("archive.events"), before_events)
        self.assertEqual(self.rows("archive.message_snapshots"), before_snapshots)
        filters = ArchiveFilters(source_key=self.source, account_id=1, inbox_id=3, round_no=1,
                                 keyword="customer original", message_scope="contact")
        self.assertEqual(self.reader.list_rounds(filters)["total"], 1)
        messages = self.rows("ops.message_index")
        self.assertEqual({item["sender_role"] for item in messages}, {"contact", "bot"})
        self.assertTrue(all(item["round_id"] for item in messages))

    def test_bot_assignment_is_not_a_human_seat_and_clears_current_human_filter(self):
        incoming = event(1)
        incoming["payload"]["conversation"]["meta"]["assignee"] = {"id": 7, "type": "user", "name": "Human"}
        self.receive(incoming, event(2, 1, sender="user", direction="outgoing"),
                     event(3, 2, event_type="conversation_updated", assignee={"id": 6, "type": "agent_bot", "name": "Bot"}))
        self.drain()
        filters = dict(source_key=self.source, account_id=1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**filters, agent_id="7", agent_scope="current"))["total"], 0)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**filters, agent_id="7", agent_scope="assigned"))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**filters, agent_id="7", agent_scope="replied"))["total"], 1)
        self.assertEqual(self.reader.list_rounds(ArchiveFilters(**filters, agent_id="6", agent_scope="assigned"))["total"], 0)
        self.assertEqual(self.rows("ops.round_index")[0]["reception_type"], "human")
        options = self.reader.filter_options()
        own = [row["agent_id"] for row in options["agents"] if row["source_key"] == self.source]
        self.assertEqual(own, ["7"])

    def test_update_only_agent_reply_is_queryable_and_counted_once_after_late_creation(self):
        self.receive(event(1), event(2, 1, event_type="message_updated", message_id=100,
            sender="user", direction="outgoing", content="first observed edited reply",
            updated_at=(START+timedelta(minutes=2)).isoformat()))
        self.drain()
        filters = ArchiveFilters(source_key=self.source, account_id=1, agent_id="7", agent_scope="replied")
        first = self.reader.list_rounds(filters)
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["items"][0]["reception_type"], "human")
        self.assertEqual(first["items"][0]["counts"]["agent"], 1)
        observed = next(item for item in self.rows("ops.message_index") if item["message_id"] == 100)
        self.assertFalse(observed["data"]["has_creation_event"])
        self.receive(event(3, 1, message_id=100, sender="user", direction="outgoing", content="actual creation body"))
        self.drain()
        latest = self.reader.list_rounds(filters)
        self.assertEqual(latest["items"][0]["counts"]["agent"], 1)
        self.assertEqual(latest["items"][0]["agents"][0]["reply_count"], 1)
        observed = next(item for item in self.rows("ops.message_index") if item["message_id"] == 100)
        self.assertTrue(observed["data"]["has_creation_event"])
        versions = self.reader.message_versions(self.source, 1, 100)
        self.assertEqual(versions["total"], 2)
        self.assertEqual({item["content"] for item in versions["items"]}, {"first observed edited reply", "actual creation body"})


    def test_recent_fifty_rounds_keep_older_history_and_original_ordinals(self):
        self.receive_bulk(self.rounds_events(62))
        with patch.object(self.processor, "_write_projection", wraps=self.processor._write_projection) as writes:
            self.drain()
        self.assertEqual([call.kwargs["round_no_offset"] for call in writes.call_args_list], [0, 50])
        original = sorted(self.rows("ops.round_index"), key=lambda row: row["round_no"])
        self.assertEqual([row["round_no"] for row in original], list(range(1, 63)))
        self.receive(event(1000, 620, content="newest round"))
        with patch.object(self.processor, "_write_projection", wraps=self.processor._write_projection) as writes:
            self.drain()
        self.assertEqual([call.kwargs["round_no_offset"] for call in writes.call_args_list], [13])
        latest = sorted(self.rows("ops.round_index"), key=lambda row: row["round_no"])
        self.assertEqual(latest[:13], original[:13])
        self.assertEqual(len(latest), 63)
        self.assertEqual(len(self.rows("ops.message_index")), 63)
        customer_id = self.rows("ops.customer_index")[0]["customer_id"]
        self.assertEqual(self.reader.customer_history(customer_id)["total"], 63)

    def test_old_edit_deletion_and_late_rating_repair_history_outside_recent_window(self):
        self.receive_bulk(self.rounds_events(62))
        attrs = {"ics_round_id": "historical-business-round", "ics_closed_at": (START+timedelta(minutes=2)).isoformat()}
        self.receive(event(1000, 3, sender="agent_bot", direction="outgoing", content_type="input_csat",
                           content_attributes=attrs))
        self.drain()
        before_raw = self.rows("archive.events")
        round_id = next(row["round_id"] for row in self.rows("ops.round_index") if row["round_no"] == 1)
        self.receive(event(1001, 620, event_type="message_updated", message_id=1, content="old edited",
                           created_at=START.isoformat(), updated_at=(START+timedelta(minutes=620)).isoformat()),
                     event(1002, 621, event_type="message_deleted", message_id=3,
                           created_at=(START+timedelta(minutes=10)).isoformat(), content=None),
                     event(1003, 622, event_type="message_updated", message_id=1000, content_type="input_csat",
                           created_at=(START+timedelta(minutes=3)).isoformat(),
                           csat_survey_response={"message_id": 1000, "rating": 2, "feedback_message": "late feedback"}))
        self.drain()
        messages = {row["message_id"]: row for row in self.rows("ops.message_index")}
        self.assertEqual(messages[1]["content"], "old edited")
        self.assertTrue(messages[3]["is_deleted"])
        first = next(row for row in self.rows("ops.round_index") if row["round_id"] == round_id)
        self.assertEqual((first["rating"], first["data"]["rating_feedback"]), (2, "late feedback"))
        self.assertEqual(self.rows("archive.events")[:len(before_raw)], before_raw)
        self.assertEqual(len(self.rows("ops.round_index")), 62)

    def test_bounded_progress_survives_restart_without_using_failure_attempts(self):
        self.receive_bulk(self.rounds_events(62))
        limited = ProcessingRepository(self.settings.model_copy(update={"processor_index_batch_size": 5}))
        limited.open()
        try:
            self.assertTrue(limited.process_next("interrupted-worker"))
        finally:
            limited.close()
        plan = self.rows("ops.conversation_replay_state")[0]["plan"]
        self.assertFalse(plan["indexed"])
        self.assertEqual(len(self.rows("ops.event_replay_index")), 5)
        pending = self.admin.execute("""SELECT max(j.attempts) AS n FROM ops.event_jobs j
            JOIN archive.events e ON e.id=j.event_id WHERE e.source_key=%s""", (self.source,)).fetchone()
        self.assertEqual(pending["n"], 0)
        self.drain()
        self.assertEqual(self.rows("ops.conversation_replay_state")[0]["plan"], {})
        self.assertEqual(len(self.rows("ops.round_index")), 62)
        self.assertTrue(self.admin.execute("""SELECT bool_and(j.status='completed') AS done FROM ops.event_jobs j
            JOIN archive.events e ON e.id=j.event_id WHERE e.source_key=%s""", (self.source,)).fetchone()["done"])

    def test_late_customer_message_merges_old_boundary_and_renumbers_without_losing_messages(self):
        self.receive(event(1), event(2, 360), event(3, 720))
        self.drain()
        third = next(row["round_id"] for row in self.rows("ops.round_index") if row["round_no"] == 3)
        self.receive(event(4, 180, content="late bridging message"))
        self.drain()
        rounds = sorted(self.rows("ops.round_index"), key=lambda row: row["round_no"])
        self.assertEqual([row["round_no"] for row in rounds], [1, 2])
        self.assertEqual(rounds[1]["round_id"], third)
        self.assertEqual(len(self.rows("ops.message_index")), 4)
        self.assertTrue(all(row["round_id"] for row in self.rows("ops.message_index")))

    def test_recent_window_is_shared_by_customer_but_conversation_state_is_independent(self):
        self.receive_bulk(self.rounds_events(40))
        self.receive_bulk(self.rounds_events(30, conversation_id=22, start=1000, first_id=1000))
        self.drain()
        self.receive(event(2000, 1300, conversation_id=22))
        with patch.object(self.processor, "_write_projection", wraps=self.processor._write_projection) as writes:
            self.drain()
        self.assertEqual([call.kwargs["round_no_offset"] for call in writes.call_args_list], [0])
        self.receive(event(2001, 399, event_type="conversation_updated"))
        with patch.object(self.processor, "_write_projection", wraps=self.processor._write_projection) as writes:
            self.drain()
        self.assertEqual([call.kwargs["round_no_offset"] for call in writes.call_args_list], [21])
        self.assertEqual(len(self.rows("ops.round_index")), 71)

    def test_replay_query_settings_are_restored_after_success_and_rollback(self):
        """重放的局部规划设置不能泄漏到同一连接的后续查询。"""
        with self.processor.pool.connection() as connection:
            for nestloop, jit in (("on", "on"), ("on", "off"), ("off", "on"), ("off", "off")):
                with self.subTest(nestloop=nestloop, jit=jit):
                    connection.execute("""SELECT set_config('enable_nestloop',%s,true),
                        set_config('jit',%s,true)""", (nestloop, jit))
                    result = self.processor.replay.execute_facts(connection, "SELECT 42 AS value", ())
                    self.assertEqual(result.fetchone()["value"], 42)
                    settings_sql = """SELECT current_setting('enable_nestloop') AS nestloop,
                        current_setting('jit') AS jit"""
                    self.assertEqual(connection.execute(settings_sql).fetchone(), {"nestloop": nestloop, "jit": jit})
                    with self.assertRaises(psycopg.errors.DivisionByZero):
                        with connection.transaction():
                            self.processor.replay.execute_facts(connection, "SELECT 1/0", ())
                    self.assertEqual(connection.execute(settings_sql).fetchone(), {"nestloop": nestloop, "jit": jit})

    def test_lifetime_above_twenty_thousand_events_and_sixty_four_mib_uses_bounded_windows(self):
        # 即使后续重试碰巧成功，大会话也不应在首次处理时触发查询超时。
        retry_patch = patch.object(self.processor, "_retry", wraps=self.processor._retry)
        retries = retry_patch.start()
        self.addCleanup(retry_patch.stop)
        def history():
            for index in range(60):
                for message in range(350):
                    yield event(index*351+message+1, index*10+message/60, content="x"*3500)
                yield event(index*351+351, index*10+6, event_type="conversation_status_changed", status="resolved")
        self.receive_bulk(history())
        total = self.admin.execute("""SELECT count(*) AS n,sum(octet_length(payload::text)) AS bytes
            FROM archive.events WHERE source_key=%s""", (self.source,)).fetchone()
        self.assertGreater(total["n"], 20000)
        self.assertGreater(total["bytes"], 64*1024*1024)
        with patch.object(self.processor, "_write_projection", wraps=self.processor._write_projection) as writes:
            self.drain()
        self.assertGreater(len(writes.call_args_list), 1)
        self.assertTrue(all(len(call.args[1]) <= 20000 for call in writes.call_args_list))
        self.assertEqual(len(self.rows("ops.round_index")), 60)
        oldest = next(row for row in self.rows("ops.round_index") if row["round_no"] == 1)
        self.receive(event(30000, 600, content="still accepts new rounds"))
        self.drain()
        self.assertEqual(len(self.rows("ops.round_index")), 61)
        self.assertEqual(len(self.rows("ops.message_index")), 21001)
        self.assertEqual(next(row for row in self.rows("ops.round_index") if row["round_no"] == 1), oldest)
        self.assertTrue(self.admin.execute("""SELECT bool_and(j.status='completed') AS done FROM ops.event_jobs j
            JOIN archive.events e ON e.id=j.event_id WHERE e.source_key=%s""", (self.source,)).fetchone()["done"])
        retries.assert_not_called()

    def test_assignment_key_conflict_across_window_boundary_repairs_both_rounds(self):
        self.receive_bulk(self.rounds_events(62))
        self.receive_assignment("8", trigger_message_id=1, minute=1, session_minute=0)
        self.drain()
        self.receive_assignment("9", trigger_message_id=121, minute=601, session_minute=600)
        self.drain()
        affected = [row for row in self.rows("ops.round_index") if row["round_no"] in (1, 61)]
        self.assertEqual(len(affected), 2)
        self.assertTrue(all(row["data"]["responsibility_basis"] == "assignment_conflict" for row in affected))
        self.assertTrue(all(row["data"]["responsible_agent_id"] is None for row in affected))

    def test_assignment_timestamp_tolerance_at_window_boundary_does_not_conflict_with_prior_round(self):
        self.receive_bulk(event(index+1, index*360) for index in range(52))
        self.receive_assignment("8", trigger_message_id=51, minute=18001, session_minute=18000-0.00001)
        self.drain()
        rounds = {row["round_no"]: row for row in self.rows("ops.round_index")}
        self.assertEqual(rounds[50]["data"]["responsibility_basis"], "assignment_unobserved")
        self.assertEqual(rounds[51]["data"]["responsibility_basis"], "ics_assignment")

    def test_restart_between_replay_windows_keeps_human_takeover_and_new_arrivals(self):
        self.receive_bulk(event(index+1, index*360) for index in range(62))
        self.receive(agent_reply(1000, 1))
        self.processor.process_next("first-window")
        self.assertEqual(self.rows("ops.conversation_replay_state")[0]["plan"]["cursor"], 51)
        self.assertEqual(len(self.rows("ops.round_index")), 50)
        self.receive(event(2000, 62*360))
        replacement = ProcessingRepository(self.settings)
        replacement.open()
        self.addCleanup(replacement.close)
        self.assertTrue(replacement.process_next("replacement"))
        # The in-flight plan must not acknowledge the newer unindexed event.
        latest = self.admin.execute("""SELECT j.status FROM ops.event_jobs j JOIN archive.events e ON e.id=j.event_id
            WHERE e.source_key=%s AND e.message_id=2000""", (self.source,)).fetchone()
        self.assertEqual(latest["status"], "queued")
        self.drain()
        rounds = sorted(self.rows("ops.round_index"), key=lambda row: row["round_no"])
        self.assertEqual(len(rounds), 63)
        self.assertTrue(all(row["reception_type"] == "human" for row in rounds))
        self.assertTrue(all(row["data"]["takeover_inherited"] for row in rounds[1:]))

    def test_single_round_event_budget_still_protects_memory_and_keeps_raw_evidence(self):
        self.receive_bulk(event(index+1, index/60) for index in range(101))
        limited = ProcessingRepository(self.settings.model_copy(update={
            "processor_max_conversation_events": 100, "processor_max_attempts": 1}))
        limited.open()
        self.addCleanup(limited.close)
        claim = limited.claim("single-round-limit")
        with self.assertRaisesRegex(RuntimeError, "event bound"):
            limited.process_claim(claim)
        limited._retry(claim, "RuntimeError")
        self.assertEqual(len(self.rows("archive.events")), 101)
        self.assertEqual(self.rows("ops.round_index"), [])
        self.assertEqual(self.rows("archive.message_snapshots"), [])
        self.drain()

    def test_new_generation_claim_is_released_when_it_finishes_the_previous_plan(self):
        self.receive_bulk(self.rounds_events(62))
        self.processor.process_next("start-plan")
        receipt = self.receive(event(2000, 620))[0]
        self.admin.execute("""UPDATE ops.event_jobs j SET available_at=CURRENT_TIMESTAMP+INTERVAL '1 day'
            FROM archive.events e WHERE e.id=j.event_id AND e.source_key=%s AND j.event_id<>%s""",
            (self.source, receipt.event_id))
        claim = self.processor.claim("new-generation")
        self.assertEqual(claim["event_id"], receipt.event_id)
        self.processor.process_claim(claim)
        job = self.admin.execute("SELECT status,attempts,locked_by FROM ops.event_jobs WHERE event_id=%s",
                                 (receipt.event_id,)).fetchone()
        self.assertEqual(job, {"status": "retry", "attempts": 0, "locked_by": None})
        self.drain()
        self.assertEqual(len(self.rows("ops.round_index")), 63)

    def test_rating_alias_conflict_across_window_boundary_stays_unlinked(self):
        self.receive_bulk(self.rounds_events(62))
        self.receive(event(1000, 3, sender="agent_bot", direction="outgoing", content_type="input_csat",
            content_attributes={"ics_round_id": "shared-alias", "ics_closed_at": (START+timedelta(minutes=2)).isoformat()}))
        self.drain()
        self.receive(event(1001, 603, sender="agent_bot", direction="outgoing", content_type="input_csat",
            content_attributes={"ics_round_id": "shared-alias", "ics_closed_at": (START+timedelta(minutes=602)).isoformat()}))
        self.drain()
        self.assertTrue(all(row["status"] == "unlinked" and row["round_id"] is None for row in self.rows("ops.rating_index")))
        affected = [row for row in self.rows("ops.round_index") if row["round_no"] in (1, 61)]
        self.assertTrue(all(row["rating_status"] == "not_invited" for row in affected))

    def test_rating_by_old_alias_without_invitation_id_updates_the_original_round(self):
        self.receive_bulk(self.rounds_events(62))
        self.receive(event(1000, 3, sender="agent_bot", direction="outgoing", content_type="input_csat",
            content_attributes={"ics_round_id": "old-alias", "ics_closed_at": (START+timedelta(minutes=2)).isoformat()}))
        self.drain()
        self.receive(event(1001, 630, event_type="csat_survey_response", ics_round_id="old-alias", rating=4,
                           conversation={"id": 12, "account_id": 1}))
        self.drain()
        first = next(row for row in self.rows("ops.round_index") if row["round_no"] == 1)
        self.assertEqual(first["rating"], 4)

    def test_rating_invitation_disambiguates_close_timestamps_across_small_windows(self):
        from quality.processing.projector import stable_id
        round_id = stable_id("round:", self.source, 1, 12, 1)
        self.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
                     event(3, 1.00001), event(4, 1.00002, event_type="conversation_status_changed", status="resolved"),
                     event(1000, 2, sender="agent_bot", direction="outgoing", content_type="input_csat",
                           content_attributes={"ics_round_id": round_id, "ics_closed_at": (START+timedelta(minutes=1)).isoformat()}),
                     event(1001, 3, event_type="message_updated", message_id=1000, content_type="input_csat",
                           created_at=(START+timedelta(minutes=2)).isoformat(),
                           content_attributes={"ics_closed_at": (START+timedelta(minutes=1.00001)).isoformat()},
                           csat_survey_response={"message_id": 1000, "rating": 4}))
        small = ProcessingRepository(self.settings.model_copy(update={"processor_recent_rounds": 1}))
        small.open()
        self.addCleanup(small.close)
        for _ in range(10):
            if not small.process_next("small-window"):
                break
        rounds = {row["round_no"]: row for row in self.rows("ops.round_index")}
        self.assertEqual(rounds[1]["rating"], 4)
        self.assertEqual(rounds[2]["rating_status"], "not_invited")
        self.assertEqual(self.rows("ops.rating_index")[0]["round_id"], round_id)




if __name__ == "__main__":
    unittest.main()
