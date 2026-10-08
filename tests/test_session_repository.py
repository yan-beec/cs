"""Verify consultation boundaries against PostgreSQL in disposable schemas."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ics_agent.app.chatwoot.state import (
    CONVERSATION_CONTROL_TABLE,
    ConversationControlRepository,
)
from tests import test_handoff_repository as handoff_tests

_SchemaConnections = handoff_tests._SchemaConnections
_connect = handoff_tests._connect
sql = handoff_tests.sql


class SessionRepositoryPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        handoff_tests.HandoffRepositoryPostgresTest.setUpClass.__func__(cls)

    def setUp(self) -> None:
        self.schema_name = "test_session_" + uuid4().hex
        self.account_id = "test-account-" + uuid4().hex
        self.started_at = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
        with _connect() as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema_name))
            )
        self.addCleanup(self._drop_test_schema)
        self.connections = _SchemaConnections(self.schema_name)
        self.repository = ConversationControlRepository(self.connections)
        self.repository.ensure_table()

    def _drop_test_schema(self) -> None:
        self.assertRegex(self.schema_name, r"\Atest_session_[a-f0-9]{32}\Z")
        with _connect() as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema_name))
            )

    def message(self, message_id="1", *, at=None, status="pending"):
        return self.repository.record_user_message(
            account_id=self.account_id,
            conversation_id="1",
            thread_id=f"chatwoot:{self.account_id}:1",
            message_id=message_id,
            message_at=at or self.started_at,
            chatwoot_status=status,
        )

    def status(self, status="resolved", *, at=None, release=True):
        return self.repository.record_conversation_status(
            account_id=self.account_id,
            conversation_id="1",
            status=status,
            event_at=at,
            release_handoff=release,
        )

    def handoff(self):
        control = self.message()
        return self.repository.request_handoff(
            account_id=self.account_id,
            conversation_id="1",
            thread_id=control.active_thread_id,
            reason="explicit_keyword",
            message_id="1",
            chatwoot_status="pending",
        )[0]

    def test_first_message_and_restart_reuse_session_under_six_hours(self):
        first = self.message()
        self.assertIn(":round:", first.active_thread_id)
        self.assertEqual(first.session_started_at, self.started_at)
        self.repository = ConversationControlRepository(self.connections)
        second = self.message("2", at=self.started_at + timedelta(hours=5, minutes=59))
        self.assertEqual(first.active_thread_id, second.active_thread_id)
        self.assertEqual(second.session_started_at, self.started_at)
        self.assertEqual(second.last_user_message_at, self.started_at + timedelta(hours=5, minutes=59))

    def test_exact_six_hour_gap_rotates_once(self):
        first = self.message()
        second = self.message("2", at=self.started_at + timedelta(hours=6))
        third = self.message("3", at=self.started_at + timedelta(hours=6, minutes=1))
        self.assertNotEqual(first.active_thread_id, second.active_thread_id)
        self.assertEqual(second.active_thread_id, third.active_thread_id)
        self.assertEqual(second.session_started_at, self.started_at + timedelta(hours=6))

    def test_ai_only_resolved_closes_context_until_next_user_message(self):
        first = self.message()
        ended_at = self.started_at + timedelta(minutes=1)
        closed = self.status(at=ended_at)
        self.assertEqual(closed.session_closed_at, ended_at)
        self.assertEqual(closed.active_thread_id, first.active_thread_id)
        self.assertFalse(closed.handoff_required)
        second = self.message("2", at=ended_at + timedelta(seconds=1), status="open")
        self.assertNotEqual(first.active_thread_id, second.active_thread_id)
        self.assertEqual(second.session_closed_at, ended_at)
        self.assertGreater(second.session_started_at, second.session_closed_at)

    def test_incoming_resolved_snapshot_does_not_close_new_context(self):
        first = self.message(status="resolved")
        self.assertIsNone(first.session_closed_at)
        ended_at = self.started_at + timedelta(minutes=1)
        self.status(at=ended_at)
        reopened = self.message("2", at=ended_at + timedelta(seconds=1), status="resolved")
        self.assertEqual(reopened.session_closed_at, ended_at)
        self.assertGreater(reopened.session_started_at, reopened.session_closed_at)

    def test_six_hour_rotation_preserves_handoff_and_assignment(self):
        first = self.handoff()
        with self.connections.acquire() as connection:
            connection.execute(
                f"UPDATE {CONVERSATION_CONTROL_TABLE} "
                "SET assigned_agent_id = '10', assigned_team_id = '7'",
            )
        second = self.message("2", at=self.started_at + timedelta(hours=6))
        self.assertNotEqual(first.active_thread_id, second.active_thread_id)
        self.assertTrue(second.handoff_required)
        self.assertFalse(second.ai_enabled)
        self.assertEqual(second.assigned_agent_id, "10")
        self.assertEqual(second.assigned_team_id, "7")
        self.assertEqual(second.handoff_trigger_message_id, "1")

    def test_ingress_close_and_worker_retry_release_handoff_idempotently(self):
        self.handoff()
        ended_at = self.started_at + timedelta(minutes=1)
        closed = self.status(at=ended_at, release=False)
        self.assertTrue(closed.handoff_required)
        self.assertEqual(closed.session_closed_at, ended_at)
        released = self.status(at=ended_at)
        self.assertFalse(released.handoff_required)
        self.assertTrue(released.ai_enabled)
        retry = self.status(at=ended_at)
        self.assertEqual(retry.session_closed_at, ended_at)
        self.assertEqual(retry.active_thread_id, closed.active_thread_id)

    def test_duplicate_message_never_extends_idle_deadline(self):
        first = self.message()
        duplicate = self.message("1", at=self.started_at + timedelta(hours=7))
        self.assertEqual(duplicate.active_thread_id, first.active_thread_id)
        self.assertEqual(duplicate.last_user_message_at, self.started_at)
        second = self.message("2", at=self.started_at + timedelta(hours=6))
        self.assertNotEqual(second.active_thread_id, first.active_thread_id)

    def test_same_message_reopens_after_refresh_discovers_earlier_resolved(self):
        first = self.message()
        message_at = self.started_at + timedelta(minutes=2)
        pending = self.message("2", at=message_at)
        self.assertEqual(pending.active_thread_id, first.active_thread_id)
        ended_at = self.started_at + timedelta(minutes=1)
        self.status(at=ended_at)
        reopened = self.message("2", at=message_at)
        self.assertNotEqual(reopened.active_thread_id, first.active_thread_id)
        self.assertEqual(reopened.session_started_at, message_at)
        self.assertEqual(reopened.last_user_message_at, message_at)
        self.assertEqual(self.message("2", at=message_at), reopened)
        self.assertEqual(self.message("2", at=message_at + timedelta(hours=7)), reopened)

    def test_handoff_cannot_reopen_closed_consultation(self):
        first = self.message()
        closed = self.status(at=self.started_at + timedelta(minutes=1))
        control, transitioned = self.repository.request_handoff(
            account_id=self.account_id, conversation_id="1",
            thread_id=first.active_thread_id,
            reason="explicit_keyword", message_id="1", chatwoot_status="open",
        )
        self.assertFalse(transitioned)
        self.assertEqual(control, closed)

    def test_handoff_rejects_old_round_but_accepts_current_round(self):
        first = self.message()
        current = self.message("2", at=self.started_at + timedelta(hours=6))
        stale, transitioned = self.repository.request_handoff(
            account_id=self.account_id, conversation_id="1",
            thread_id=first.active_thread_id,
            reason="explicit_keyword", message_id="1", chatwoot_status="open",
        )
        self.assertFalse(transitioned)
        self.assertEqual(stale, current)
        valid, transitioned = self.repository.request_handoff(
            account_id=self.account_id, conversation_id="1",
            thread_id=current.active_thread_id,
            reason="explicit_keyword", message_id="2", chatwoot_status="pending",
        )
        self.assertTrue(transitioned)
        self.assertTrue(valid.handoff_required)

    def test_late_message_does_not_rewind_current_session_or_user_timestamp(self):
        self.message()
        current = self.message("3", at=self.started_at + timedelta(hours=7))
        late = self.message("2", at=self.started_at + timedelta(hours=1), status="resolved")
        self.assertEqual(late, current)
        same_round_late = self.message("4", at=self.started_at + timedelta(hours=7, minutes=5))
        latest = self.message("6", at=self.started_at + timedelta(hours=7, minutes=10))
        late = self.message("5", at=self.started_at + timedelta(hours=7, minutes=7))
        self.assertEqual(late, latest)
        self.assertEqual(same_round_late.active_thread_id, current.active_thread_id)

    def test_old_resolved_and_old_open_cannot_modify_later_session(self):
        self.message()
        ended_at = self.started_at + timedelta(minutes=1)
        self.status(at=ended_at)
        current = self.message("2", at=ended_at + timedelta(seconds=1))
        self.assertEqual(self.status(at=ended_at), current)
        self.assertEqual(self.status("open", at=self.started_at), current)

    def test_late_resolved_preserves_newer_open_status_but_closes_old_context(self):
        first = self.message()
        ended_at = self.started_at + timedelta(minutes=1)
        opened_at = ended_at + timedelta(minutes=1)
        self.status("open", at=opened_at)
        closed = self.status(at=ended_at)
        self.assertEqual(closed.session_closed_at, ended_at)
        self.assertEqual(closed.last_status_event_at, opened_at)
        self.assertEqual(closed.last_chatwoot_status, "open")
        current = self.message("2", at=opened_at + timedelta(seconds=1), status="open")
        self.assertNotEqual(first.active_thread_id, current.active_thread_id)
        self.assertEqual(current.session_closed_at, ended_at)
        self.assertEqual(self.status(at=ended_at), current)

    def test_message_before_or_at_closed_boundary_does_not_reopen(self):
        self.message()
        ended_at = self.started_at + timedelta(minutes=1)
        closed = self.status(at=ended_at)
        late = self.message("2", at=ended_at)
        self.assertEqual(late, closed)

    def test_internal_pending_does_not_advance_external_event_watermark(self):
        self.message()
        pending = self.status("pending")
        self.assertIsNone(pending.last_status_event_at)
        ended_at = self.started_at + timedelta(minutes=1)
        closed = self.status(at=ended_at)
        self.assertEqual(closed.session_closed_at, ended_at)
        self.assertEqual(self.status("pending"), closed)

    def test_concurrent_same_message_creates_one_replacement_session(self):
        first = self.message()
        gate = threading.Barrier(8)

        def replay(_):
            gate.wait(timeout=10)
            return self.message("2", at=self.started_at + timedelta(hours=6))

        with ThreadPoolExecutor(max_workers=8) as executor:
            controls = list(executor.map(replay, range(8)))
        self.assertEqual(len({control.active_thread_id for control in controls}), 1)
        self.assertNotEqual(controls[0].active_thread_id, first.active_thread_id)

    def test_upgrade_does_not_guess_user_activity_from_updated_at(self):
        first = self.handoff()
        with self.connections.acquire() as connection:
            for column in (
                "session_started_at", "session_closed_at",
                "last_user_message_at", "last_status_event_at",
            ):
                connection.execute(
                    f"ALTER TABLE {CONVERSATION_CONTROL_TABLE} DROP COLUMN {column}"
                )
        self.repository.ensure_table()
        self.repository.ensure_table()
        legacy = self.repository.get(account_id=self.account_id, conversation_id="1")
        self.assertIsNone(legacy.last_user_message_at)
        self.assertIsNone(legacy.session_started_at)
        self.assertTrue(legacy.handoff_required)
        self.assertEqual(legacy.active_thread_id, first.active_thread_id)
        new = self.message("2", at=self.started_at + timedelta(minutes=1))
        self.assertNotEqual(new.active_thread_id, first.active_thread_id)
        self.assertTrue(new.handoff_required)


    def test_webhook_redelivery_preserves_first_receipt_timestamp(self):
        from ics_agent.app.chatwoot.jobs import WebhookIdempotencyRepository

        events = WebhookIdempotencyRepository(self.connections)
        events.ensure_table()
        args = dict(
            event_key="event-without-source-time", event_id="9",
            event_type="conversation_status_changed", body_hash="test-hash",
            stale_after_seconds=300,
        )
        first = events.claim(**args)
        self.assertTrue(first.accepted)
        self.assertIsNotNone(first.first_seen_at)
        events.mark_failed(args["event_key"], "queue unavailable")
        retry = events.claim(**args)
        self.assertTrue(retry.accepted)
        self.assertEqual(retry.attempts, 2)
        self.assertEqual(retry.first_seen_at, first.first_seen_at)
        duplicate = events.claim(**args)
        self.assertFalse(duplicate.accepted)
        self.assertEqual(duplicate.first_seen_at, first.first_seen_at)


if __name__ == "__main__":
    unittest.main()
