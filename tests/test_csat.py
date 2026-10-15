"""CSAT delivery contracts and real PostgreSQL consultation/outbox boundaries."""

from __future__ import annotations

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import Mock, patch
from uuid import uuid4

from ics_agent.app.chatwoot.client import ChatwootAPIError, ChatwootClient
from ics_agent.app.chatwoot.csat import CSAT_TABLE, CsatInvitationRepository, run_csat_worker
from ics_agent.app.chatwoot.state import CONVERSATION_CONTROL_TABLE, ConversationControlRepository
from ics_agent.app.core.config import AppSettings
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

    def test_failed_or_nonadvancing_lookup_is_not_treated_as_missing_card(self):
        for response in ({}, {"payload": {}}, {"payload": [{"id": 100}]}):
            with self.subTest(response=response), patch.object(self.client, "_request_json", return_value=response):
                with self.assertRaises(ChatwootAPIError):
                    self.client.find_csat_invitation(
                        account_id="1", conversation_id="2", round_id="r", after_message_id="100",
                    )


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

    def close(self, at=None, assignee=None):
        return self.controls.record_conversation_status(
            account_id="1", conversation_id="2", status="resolved",
            event_at=at or self.started + timedelta(minutes=1),
            on_session_closed=partial(self.repository.capture, inbox_id="3", assignee=assignee or {}),
        )

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

    def test_capture_is_atomic_and_precedes_assignment_release(self):
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

    def test_failed_capture_rolls_back_end_marker_and_outbox_together(self):
        self.message()

        def failed_capture(cursor, control, closed_at):
            self.repository.capture(cursor, control, closed_at, inbox_id="3", assignee={})
            raise RuntimeError("interrupted transaction")

        with self.assertRaises(RuntimeError):
            self.controls.record_conversation_status(
                account_id="1", conversation_id="2", status="resolved",
                event_at=self.started + timedelta(minutes=1), on_session_closed=failed_capture,
            )
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.controls.get(account_id="1", conversation_id="2").session_closed_at)
        self.close()
        self.assertEqual(len(self.rows()), 1)

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
            self.assertEqual(self.rows()[0][:2], (first.active_thread_id, "11"))
            self.client.send_csat_invitation.assert_not_called()
            job = queue.enqueue_nowait.call_args.args[0]
            await routes._process_webhook_job(job)
            self.assertEqual(len(self.rows()), 1)

        with patch.object(routes, "settings", self.settings), \
                patch.object(routes, "conversation_control_repository", self.controls), \
                patch.object(routes, "get_csat_repository", return_value=self.repository), \
                patch.object(routes, "webhook_repository", webhook_repository), \
                patch.object(routes, "webhook_queue", queue):
            asyncio.run(receive_and_process())

    def test_disabled_feature_and_activity_snapshot_do_not_capture(self):
        from ics_agent.app.chatwoot import routes
        from tests import test_session_routes as fixtures

        self.message()
        with patch.object(routes, "settings", AppSettings(_env_file=None)), \
                patch.object(routes, "get_csat_repository") as get_repository:
            self.assertEqual(routes._csat_close_kwargs(fixtures._event(fixtures._status_payload())), {})
            get_repository.assert_not_called()
        snapshot = fixtures._conversation_update_payload(blocked=False)
        with patch.object(routes, "settings", self.settings), patch.object(routes, "get_csat_repository") as get_repository:
            self.assertEqual(routes._csat_close_kwargs(fixtures._event(snapshot)), {})
            get_repository.assert_not_called()
        self.assertEqual(self.rows(), [])


class CsatWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_inflight_delivery_to_finish(self):
        entered, release = threading.Event(), threading.Event()
        repository = Mock()

        def deliver(*args):
            entered.set()
            release.wait(5)
            return True

        repository.deliver_one.side_effect = deliver
        worker = asyncio.create_task(run_csat_worker(repository, Mock(), AppSettings(_env_file=None)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            worker.cancel()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await worker
