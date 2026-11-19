"""Full assignment journal/delivery/query checks in the disposable query DB."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import psycopg

from ics_agent.app.chatwoot.state import ConversationControlRepository
from quality.assignment.config import AssignmentSyncSettings
from quality.assignment.repository import AssignmentSyncRepository
from quality.query.repository import ArchiveFilters
from quality.tests import test_query_database as fixture
from quality.tests.test_processing import START, agent_reply, event


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated query test database")
class AssignmentDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)
        cls.admin.execute("ALTER ROLE archive_assignment LOGIN PASSWORD 'isolated-assignment'")

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    rows = fixture.QueryDatabaseTest.rows

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        kwargs = {**self.settings.connection_kwargs, "user": "archive_admin",
            "password": os.environ["PROCESSING_TEST_ADMIN_PASSWORD"]}
        connections = SimpleNamespace(acquire=lambda: psycopg.connect(**kwargs, autocommit=True))
        self.control = ConversationControlRepository(connections)
        self.control.ensure_table()
        self.admin.execute("DELETE FROM public.chatwoot_assignment_outbox WHERE account_id='1' AND conversation_id='12'")
        self.admin.execute("DELETE FROM public.chatwoot_conversation_control WHERE account_id='1' AND conversation_id='12'")
        self.business_round = "chatwoot:1:12:round:" + uuid.uuid4().hex
        self.admin.execute("""INSERT INTO public.chatwoot_conversation_control
            (account_id,conversation_id,active_thread_id,last_chatwoot_status,ai_enabled,handoff_required,
             assigned_agent_id,handoff_trigger_message_id,session_started_at)
            VALUES ('1','12',%s,'open',FALSE,TRUE,'8','1',%s)""", (self.business_round, START))
        settings = AssignmentSyncSettings(_env_file=None, assignment_sync_enabled=True,
            source_key=self.source, allowed_account_ids="1", assignment_password="isolated-assignment",
            postgres_host=self.settings.postgres_host, postgres_port=self.settings.postgres_port,
            postgres_db=self.settings.postgres_db, postgres_sslmode=self.settings.postgres_sslmode)
        app = SimpleNamespace(postgres_dsn="", postgres_host=self.settings.postgres_host,
            postgres_port=self.settings.postgres_port, postgres_database=self.settings.postgres_db,
            postgres_user="archive_admin", postgres_password=os.environ["PROCESSING_TEST_ADMIN_PASSWORD"],
            postgres_sslmode=self.settings.postgres_sslmode)
        self.sync = AssignmentSyncRepository(settings, app)
        self.sync.open()
        self.addCleanup(self.sync.close)

    def test_commit_then_failed_ack_retries_without_losing_fixed_owner_or_actual_messages(self):
        self.receive(event(1), agent_reply(2, 1, agent_id=9, agent_name="actual responder"),
            event(3, 2, event_type="conversation_updated", assignee={"id": 10, "name": "reassigned"}))
        self.control.mark_handoff_action(account_id="1", conversation_id="12", action="assignment_applied")
        self.assertEqual(len(self.sync.pending()), 1)
        with self.sync.source_pool.connection() as connection:
            connection.execute("SET TIME ZONE 'UTC'")
        with patch.object(self.sync, "acknowledge", side_effect=RuntimeError("interrupted after target commit")):
            with self.assertRaises(RuntimeError):
                self.sync.sync_once()
        evidence = [row for row in self.rows("archive.events") if row["event_type"] == "ics_assignment_recorded"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["ingestion_kind"], "backfill")
        # A recovered session can report the same timestamptz using another
        # offset; retry must still reproduce exactly the committed raw bytes.
        with self.sync.source_pool.connection() as connection:
            connection.execute("SET TIME ZONE 'Asia/Shanghai'")
        self.assertEqual(len(self.sync.pending()), 1)
        self.assertEqual(self.sync.pending()[0]["recorded_at"].utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(self.sync.sync_once(), 1)
        self.assertEqual(self.sync.pending(), [])
        self.assertEqual(self.sync.sync_once(), 0)
        self.drain()
        result = self.reader.list_rounds(ArchiveFilters(source_key=self.source, account_id=1, agent_id="8"))
        self.assertEqual(result["total"], 1)
        row = result["items"][0]
        self.assertEqual(row["responsible_agent_id"], "8")
        self.assertEqual(row["responsibility_basis"], "ics_assignment")
        self.assertEqual(row["last_public_agent_id"], "9")
        self.assertEqual(row["current_assignee_id"], "10")
        # A responsible agent remains selectable even without a captured
        # Chatwoot assignment webhook or any reply by that agent.
        agents = self.reader.filter_options()["agents"]
        self.assertTrue(any(item["source_key"] == self.source and item["account_id"] == 1
            and item["agent_id"] == "8" for item in agents))
        detail = self.reader.round_detail(row["round_id"])
        actual = next(message for message in detail["messages"] if message["message_id"] == 2)
        self.assertEqual((actual["sender_id"], actual["sender_name"], actual["content"]), ("9", "actual responder", "message 2"))
        self.assertEqual(len([item for item in self.rows("archive.events") if item["event_type"] == "ics_assignment_recorded"]), 1)
        # The restricted delivery role cannot rewrite any projections or evidence.
        with self.sync.target_pool.connection() as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction():
                    connection.execute("UPDATE ops.round_index SET data='{}' WHERE FALSE")

