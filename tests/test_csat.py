"""CSAT delivery contracts and real PostgreSQL consultation/outbox boundaries."""

from __future__ import annotations

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from ics_agent.app.chatwoot.client import ChatwootAPIError, ChatwootClient
from ics_agent.app.chatwoot import csat
from ics_agent.app.chatwoot.csat import CSAT_TABLE, CsatInvitationRepository, run_csat_worker
from ics_agent.app.chatwoot.state import CONVERSATION_CONTROL_TABLE, ConversationControlRepository
from ics_agent.app.core.config import AppSettings
from ics_agent.faq.repositories import PostgreSQLConnectionPool
from tests import test_handoff_repository as handoff_tests

_SchemaConnections = handoff_tests._SchemaConnections
_connect = handoff_tests._connect
sql = handoff_tests.sql


class CsatClientTest(unittest.TestCase):
    def setUp(self):
        self.client = ChatwootClient(AppSettings(
            _env_file=None, chatwoot_api_token="user-token",
            chatwoot_agent_bot_access_token="bot-token", chatwoot_agent_bot_id="7",
        ))

    def test_native_card_preserves_round_and_agent_snapshot(self):
        with patch.object(self.client, "_request_json", return_value={"id": 8}) as request:
            result = self.client.send_csat_invitation(
                account_id="1", conversation_id="2", round_id="round-1",
                assigned_agent_id="11", closed_at="2026-10-15T08:00:00+00:00",
                content="请评价本次服务", display_type="emoji",
            )
        self.assertEqual(result["id"], 8)
        method, path, payload = request.call_args.args
        self.assertEqual((method, path), ("POST", "/api/v1/accounts/1/conversations/2/messages"))
        self.assertEqual(payload["content_type"], "input_csat")
        self.assertFalse(payload["private"])
        self.assertEqual(payload["sender_type"], "AgentBot")
        self.assertEqual(payload["sender_id"], "7")
        self.assertEqual(payload["content_attributes"]["ics_round_id"], "round-1")
        self.assertEqual(payload["content_attributes"]["ics_assigned_agent_id"], "11")
        self.assertEqual(request.call_args.kwargs["timeout_seconds"], 5)

    def test_reconciliation_paginates_and_does_not_match_another_round(self):
        card = {"id": 102, "content_type": "input_csat", "content_attributes": {"ics_round_id": "wanted"}}
        with patch.object(self.client, "_request_json", side_effect=[
            {"payload": [{"id": 101, "content_type": "input_csat", "content_attributes": {"ics_round_id": "old"}}]},
            {"payload": [card]},
        ]) as request:
            found = self.client.find_csat_invitation(
                account_id="1", conversation_id="2", round_id="wanted", after_message_id="100",
            )
        self.assertEqual(found, card)
        self.assertTrue(request.call_args_list[0].args[1].endswith("?after=100"))
        self.assertTrue(request.call_args_list[1].args[1].endswith("?after=101"))
        self.assertEqual(request.call_args.kwargs["access_token"], "user-token")
        self.assertGreater(request.call_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(request.call_args.kwargs["timeout_seconds"], 5)

    def test_pagination_has_a_shared_deadline(self):
        with patch.object(self.client, "_request_json", return_value={"payload": [{"id": 101}]}), \
                patch("ics_agent.app.chatwoot.client.time.monotonic", side_effect=[0, 0, 6]):
            with self.assertRaisesRegex(ChatwootAPIError, "timed out"):
                self.client.find_csat_invitation(
                    account_id="1", conversation_id="2", round_id="r", after_message_id="100",
                )

    def test_failed_or_nonadvancing_lookup_is_not_treated_as_missing_card(self):
        for response in ({}, {"payload": {}}, {"payload": [{"id": 100}]}):
            with self.subTest(response=response), patch.object(self.client, "_request_json", return_value=response):
                with self.assertRaises(ChatwootAPIError):
                    self.client.find_csat_invitation(
                        account_id="1", conversation_id="2", round_id="r", after_message_id="100",
                    )


class CsatIsolationTest(unittest.TestCase):
    def test_evaluation_uses_a_separate_database_pool(self):
        business_pool, evaluation_pool = Mock(), Mock()
        evaluation_pool.getconn.return_value = Mock(broken=False, closed=False)
        with patch.object(PostgreSQLConnectionPool, "_shared_pool", business_pool), \
                patch.object(csat._CsatConnectionPool, "_shared_pool", evaluation_pool):
            connection = CsatInvitationRepository().connections.acquire()
            connection.close()
        business_pool.getconn.assert_not_called()
        evaluation_pool.getconn.assert_called_once()
        evaluation_pool.putconn.assert_called_once()

    def test_context_capture_is_disabled_for_feature_off_or_activity_snapshot(self):
        from ics_agent.app.chatwoot import routes
        from tests import test_session_routes as fixtures

        with patch.object(routes, "settings", AppSettings(_env_file=None)), \
                patch.object(csat, "get_csat_repository") as get_repository:
            self.assertEqual(routes._csat_close_kwargs(fixtures._event(fixtures._status_payload())), {})
            get_repository.assert_not_called()
        with patch.object(routes, "settings", AppSettings(_env_file=None, chatwoot_csat_enabled=True)), \
                patch.object(csat, "get_csat_repository") as get_repository:
            snapshot = fixtures._conversation_update_payload(blocked=False)
            self.assertEqual(routes._csat_close_kwargs(fixtures._event(snapshot)), {})
            payload = fixtures._status_payload()
            payload["meta"] = {"assignee": {"id": 11}}
            context = routes._csat_close_kwargs(fixtures._event(payload))
            self.assertEqual(context["csat_context"]["assignee"], {"id": 11})
            get_repository.assert_not_called()


class CsatRepositoryPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handoff_tests.HandoffRepositoryPostgresTest.setUpClass.__func__(cls)

    def setUp(self):
        self.schema_name = "test_csat_" + uuid4().hex
        with _connect() as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema_name)))
        self.addCleanup(self.drop_schema)
        self.connections = _SchemaConnections(self.schema_name)
        self.controls = ConversationControlRepository(self.connections)
        self.controls.ensure_table()
        self.repository = CsatInvitationRepository(self.connections)
        self.repository.ensure_table()
        self.settings = AppSettings(_env_file=None, chatwoot_csat_enabled=True)
        self.started = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
        self.client = Mock(spec=ChatwootClient)
        self.client.find_csat_invitation.return_value = None
        self.client.send_csat_invitation.side_effect = [{"id": 101}, {"id": 202}]

    def drop_schema(self):
        self.assertRegex(self.schema_name, r"\Atest_csat_[a-f0-9]{32}\Z")
        with _connect() as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema_name)))

    def message(self, message_id="1", at=None):
        return self.controls.record_user_message(
            account_id="1", conversation_id="2", thread_id="chatwoot:1:2",
            message_id=message_id, chatwoot_status="open", message_at=at or self.started,
        )

    def close(self, at=None, assignee=None, *, stage=True):
        result = self.controls.record_conversation_status(
            account_id="1", conversation_id="2", status="resolved",
            event_at=at or self.started + timedelta(minutes=1),
            csat_context={"inbox_id": "3", "assignee": assignee or {}},
        )
        if stage:
            self.repository.stage_one()
        return result

    def pending(self):
        connection = self.connections.acquire()
        try:
            row = connection.execute(
                f"SELECT csat_pending_rounds FROM {CONVERSATION_CONTROL_TABLE} "
                "WHERE account_id='1' AND conversation_id='2'"
            ).fetchone()
            return row[0] if row else []
        finally:
            connection.close()

    def rows(self):
        connection = self.connections.acquire()
        try:
            return connection.execute(
                f"SELECT round_id, assigned_agent_id, message_id, attempts, last_error "
                f"FROM {CSAT_TABLE} ORDER BY closed_at"
            ).fetchall()
        finally:
            connection.close()

    def retry_now(self):
        connection = self.connections.acquire()
        try:
            connection.execute(f"UPDATE {CSAT_TABLE} SET next_attempt_at = CURRENT_TIMESTAMP")
        finally:
            connection.close()

    def test_two_rounds_in_same_conversation_each_send_once_even_after_restart(self):
        first = self.message()
        self.close(assignee={"id": "11"})
        self.close()
        self.close(at=self.started + timedelta(minutes=2))
        self.repository = CsatInvitationRepository(self.connections)
        self.repository.ensure_table()
        self.assertTrue(self.repository.deliver_one(self.client, self.settings))
        self.assertFalse(self.repository.deliver_one(self.client, self.settings))
        second = self.message("150", self.started + timedelta(minutes=3))
        # Late status from the first round must not invite/close the second round.
        self.close(at=self.started + timedelta(minutes=1))
        self.assertEqual(len(self.rows()), 1)
        self.close(at=self.started + timedelta(minutes=4), assignee={"id": "22"})
        self.assertTrue(self.repository.deliver_one(self.client, self.settings))
        self.assertFalse(self.repository.deliver_one(self.client, self.settings))
        rows = self.rows()
        self.assertEqual([r[:3] for r in rows], [
            (first.active_thread_id, "11", "101"), (second.active_thread_id, "22", "202"),
        ])
        self.assertEqual(self.client.send_csat_invitation.call_count, 2)
        self.assertNotEqual(first.active_thread_id, second.active_thread_id)

    def test_snapshot_keeps_original_agent_after_assignment_release(self):
        self.message()
        connection = self.connections.acquire()
        try:
            connection.execute(f"UPDATE {CONVERSATION_CONTROL_TABLE} SET assigned_agent_id='11', handoff_required=TRUE")
        finally:
            connection.close()
        closed = self.close()
        self.assertIsNone(closed.assigned_agent_id)
        self.assertFalse(closed.handoff_required)
        self.assertEqual(self.rows()[0][1], "11")

    def test_missing_outbox_does_not_block_two_rounds_and_restart_recovers_snapshots(self):
        with self.connections.acquire() as connection:
            connection.execute(f"DROP TABLE {CSAT_TABLE}")
        first = self.message()
        self.controls.request_handoff(
            account_id="1", conversation_id="2", thread_id=first.active_thread_id,
            reason="explicit_keyword", message_id="1", chatwoot_status="open",
        )
        closed = self.close(assignee={"id": "11"}, stage=False)
        self.assertFalse(closed.handoff_required)
        self.assertIsNotNone(closed.session_closed_at)
        second = self.message("150", self.started + timedelta(minutes=3))
        self.assertNotEqual(first.active_thread_id, second.active_thread_id)
        self.close(at=self.started + timedelta(minutes=4), assignee={"id": "22"}, stage=False)
        with self.assertRaises(handoff_tests.psycopg.errors.UndefinedTable):
            self.repository.stage_one()
        self.assertEqual([item["round_id"] for item in self.pending()], [first.active_thread_id, second.active_thread_id])
        self.repository = CsatInvitationRepository(self.connections)
        self.repository.ensure_table()
        self.assertTrue(self.repository.stage_one())
        self.assertEqual([item["round_id"] for item in self.pending()], [second.active_thread_id])
        self.assertTrue(self.repository.stage_one())
        self.assertFalse(self.repository.stage_one())
        self.assertEqual(self.pending(), [])
        self.assertEqual([row[:2] for row in self.rows()], [(first.active_thread_id, "11"), (second.active_thread_id, "22")])
        self.assertTrue(self.repository.deliver_one(self.client, self.settings))
        self.assertTrue(self.repository.deliver_one(self.client, self.settings))
        self.assertEqual(self.client.send_csat_invitation.call_count, 2)

    def test_ack_lock_timeout_rolls_back_transfer_but_keeps_ended_session(self):
        self.message()
        self.close(stage=False)
        with self.connections.acquire() as blocker, blocker.transaction():
            blocker.execute(
                f"SELECT 1 FROM {CONVERSATION_CONTROL_TABLE} "
                "WHERE account_id='1' AND conversation_id='2' FOR UPDATE"
            )
            with self.assertRaises(handoff_tests.psycopg.errors.LockNotAvailable):
                self.repository.stage_one()
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.pending()), 1)
        self.assertIsNotNone(self.controls.get(account_id="1", conversation_id="2").session_closed_at)
        self.repository.stage_one()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.pending(), [])

    def test_locked_outbox_does_not_block_session_close_or_new_consultation(self):
        self.message()
        with self.connections.acquire() as blocker, blocker.transaction():
            blocker.execute(f"LOCK TABLE {CSAT_TABLE} IN ACCESS EXCLUSIVE MODE")
            self.close(stage=False)
            with self.assertRaises(handoff_tests.psycopg.errors.LockNotAvailable):
                self.repository.stage_one()
            second = self.message("150", self.started + timedelta(minutes=3))
            self.assertTrue(second.ai_enabled)
            self.close(at=self.started + timedelta(minutes=4), stage=False)
            self.assertEqual(len(self.pending()), 2)
        self.repository.stage_one()
        self.repository.stage_one()
        self.assertEqual(len(self.rows()), 2)

    def test_parallel_stagers_do_not_lose_or_duplicate_rounds(self):
        self.message()
        self.close(stage=False)
        self.message("150", self.started + timedelta(minutes=3))
        self.close(at=self.started + timedelta(minutes=4), stage=False)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(self.repository.stage_one) for _ in range(2)]
            for result in results:
                result.result(timeout=5)
        while self.repository.stage_one():
            pass
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.pending(), [])

    def test_old_snapshot_transfer_preserves_current_round_and_business_timestamp(self):
        self.message()
        self.close(stage=False)
        current = self.message("150", self.started + timedelta(minutes=3))
        with self.connections.acquire() as connection:
            before = connection.execute(
                f"SELECT updated_at FROM {CONVERSATION_CONTROL_TABLE} "
                "WHERE account_id='1' AND conversation_id='2'"
            ).fetchone()[0]
        self.assertTrue(self.repository.stage_one())
        self.assertEqual(self.controls.get(account_id="1", conversation_id="2"), current)
        with self.connections.acquire() as connection:
            after = connection.execute(
                f"SELECT updated_at FROM {CONVERSATION_CONTROL_TABLE} "
                "WHERE account_id='1' AND conversation_id='2'"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(self.pending(), [])

    def test_schema_initialization_keeps_pending_snapshots_and_disabled_closes_add_none(self):
        self.message()
        self.close(stage=False)
        pending = self.pending()
        self.controls.ensure_table()
        self.assertEqual(self.pending(), pending)
        self.message("150", self.started + timedelta(minutes=3))
        self.controls.record_conversation_status(
            account_id="1", conversation_id="2", status="resolved",
            event_at=self.started + timedelta(minutes=4),
        )
        self.assertEqual(self.pending(), pending)

    def test_status_without_user_consultation_does_not_invite(self):
        self.close()
        self.assertEqual(self.rows(), [])

    def test_timeout_after_remote_create_recovers_existing_card_without_resending(self):
        self.message()
        self.close()
        self.client.send_csat_invitation.side_effect = ChatwootAPIError("request timed out")
        self.assertTrue(self.repository.deliver_one(self.client, self.settings))
        self.assertIsNone(self.rows()[0][2])
        self.assertEqual(self.rows()[0][3], 1)
        self.assertFalse(self.repository.deliver_one(self.client, self.settings))
        self.client.find_csat_invitation.return_value = {"id": 987}
        self.retry_now()
        self.repository = CsatInvitationRepository(self.connections)
        self.assertTrue(self.repository.deliver_one(self.client, self.settings))
        self.assertEqual(self.rows()[0][2:], ("987", 2, None))
        self.client.send_csat_invitation.assert_called_once()

    def test_parallel_workers_do_not_send_same_invitation(self):
        self.message()
        self.close()
        started, release = threading.Event(), threading.Event()

        def send(**kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return {"id": 101}

        self.client.send_csat_invitation.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(self.repository.deliver_one, self.client, self.settings)
            try:
                self.assertTrue(started.wait(5))
                self.assertFalse(self.repository.deliver_one(self.client, self.settings))
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5))
        self.client.send_csat_invitation.assert_called_once()

    def test_http_ingress_captures_before_queue_and_worker_does_not_duplicate(self):
        from ics_agent.app.chatwoot import routes
        from ics_agent.app.chatwoot.jobs import WebhookClaim
        from tests import test_session_routes as fixtures

        first = self.message()
        payload = fixtures._status_payload()
        payload.update(id=2, updated_at=(self.started + timedelta(minutes=1)).timestamp())
        payload["meta"] = {"assignee": {"id": 11}}
        queue, webhook_repository = Mock(), Mock()
        webhook_repository.claim.return_value = WebhookClaim(True, "queued", 1)

        async def receive_and_process():
            response = await routes.chatwoot_webhook(
                fixtures._request(payload), x_chatwoot_signature=None, x_chatwoot_timestamp=None,
            )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.pending()[0]["round_id"], first.active_thread_id)
            self.assertEqual(self.pending()[0]["assigned_agent_id"], "11")
            self.assertEqual(self.rows(), [])
            self.client.send_csat_invitation.assert_not_called()
            job = queue.enqueue_nowait.call_args.args[0]
            await routes._process_webhook_job(job)
            self.assertEqual(len(self.pending()), 1)
            self.repository.stage_one()
            self.assertEqual(len(self.rows()), 1)

        with patch.object(routes, "settings", self.settings), \
                patch.object(routes, "conversation_control_repository", self.controls), \
                patch.object(routes, "webhook_repository", webhook_repository), \
                patch.object(routes, "webhook_queue", queue):
            asyncio.run(receive_and_process())

class CsatWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_inflight_delivery_to_finish(self):
        entered, release = threading.Event(), threading.Event()
        repository = Mock()
        repository.stage_one.return_value = False

        def deliver(*args):
            entered.set()
            release.wait(5)
            return True

        repository.deliver_one.side_effect = deliver
        worker = asyncio.create_task(run_csat_worker(AppSettings(_env_file=None), repository, Mock()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            worker.cancel()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_failed_initialization_is_retried_without_stopping_worker(self):
        repository = Mock()
        repository.ensure_table.side_effect = [RuntimeError("outbox unavailable"), None]
        recovered = threading.Event()
        repository.stage_one.return_value = False

        def deliver(*args):
            recovered.set()
            return False

        repository.deliver_one.side_effect = deliver
        with self.assertLogs(csat.logger, level="ERROR"):
            worker = asyncio.create_task(run_csat_worker(AppSettings(_env_file=None), repository, Mock()))
            try:
                self.assertTrue(await asyncio.to_thread(recovered.wait, 8))
                self.assertFalse(worker.done())
                self.assertEqual(repository.ensure_table.call_count, 2)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

    async def test_evaluation_init_failure_does_not_block_business_workers_start(self):
        from ics_agent.app.chatwoot import routes

        repository = Mock()
        attempted = threading.Event()

        def fail():
            attempted.set()
            raise RuntimeError("outbox unavailable")

        repository.ensure_table.side_effect = fail
        queue = Mock(start=AsyncMock(), stop=AsyncMock())
        with patch.object(routes, "settings", AppSettings(_env_file=None, chatwoot_csat_enabled=True)), \
                patch.object(routes, "WebhookJobQueue", return_value=queue), \
                patch.object(routes, "_contact_block_reaper_loop", new=AsyncMock()), \
                patch.object(csat, "get_csat_repository", return_value=repository), \
                patch.object(csat.logger, "exception"):
            try:
                await routes.start_chatwoot_webhook_workers()
                self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
                queue.start.assert_awaited_once()
                self.assertFalse(routes.csat_worker_task.done())
            finally:
                await routes.stop_chatwoot_webhook_workers()
            queue.stop.assert_awaited_once()

    async def test_snapshot_transfer_failure_still_allows_existing_invitation_delivery(self):
        repository = Mock()
        repository.stage_one.side_effect = RuntimeError("snapshot confirmation locked")
        delivered = threading.Event()

        def deliver(*args):
            delivered.set()
            return False

        repository.deliver_one.side_effect = deliver
        with patch.object(csat.logger, "exception"):
            worker = asyncio.create_task(run_csat_worker(AppSettings(_env_file=None), repository, Mock()))
            try:
                self.assertTrue(await asyncio.to_thread(delivered.wait, 3))
                self.assertFalse(worker.done())
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

    async def test_busy_business_thread_does_not_delay_evaluation_worker(self):
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        release, delivered = threading.Event(), threading.Event()
        business = loop.run_in_executor(None, release.wait, 5)
        repository = Mock()
        repository.stage_one.return_value = False

        def deliver(*args):
            delivered.set()
            return False

        repository.deliver_one.side_effect = deliver
        worker = asyncio.create_task(run_csat_worker(AppSettings(_env_file=None), repository, Mock()))
        try:
            deadline = loop.time() + 3
            while not delivered.is_set() and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(delivered.is_set())
            self.assertFalse(business.done())
        finally:
            release.set()
            await business
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
