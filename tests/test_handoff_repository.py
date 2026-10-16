"""Run the handoff SQL against PostgreSQL in a disposable, isolated schema."""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ics_agent.app.chatwoot.state import (
    ASSIGNMENT_OUTBOX_TABLE,
    CONVERSATION_CONTROL_TABLE,
    HANDOFF_CURSOR_TABLE,
    HANDOFF_RESERVATION_TABLE,
    ConversationControlRepository,
)
from ics_agent.app.core.config import get_settings
from ics_agent.faq.repositories import _postgres_kwargs

try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None
    sql = None


def _connect():
    settings = get_settings()
    if settings.postgres_dsn:
        return psycopg.connect(
            settings.postgres_dsn,
            autocommit=True,
            connect_timeout=2,
        )
    kwargs = _postgres_kwargs()
    kwargs["connect_timeout"] = 2
    return psycopg.connect(autocommit=True, **kwargs)


class _SchemaConnections:
    """Open a fresh connection whose unqualified tables stay in one schema."""

    def __init__(self, schema_name: str) -> None:
        self.schema_name = schema_name

    def acquire(self):
        connection = _connect()
        try:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(self.schema_name)
                )
            )
        except BaseException:
            connection.close()
            raise
        return connection


class HandoffRepositoryPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if psycopg is None:
            raise unittest.SkipTest("PostgreSQL integration test needs psycopg.")
        try:
            with _connect() as connection:
                allowed = connection.execute(
                    "SELECT has_database_privilege("
                    "current_user, current_database(), 'CREATE')"
                ).fetchone()[0]
        except psycopg.Error as exc:
            raise unittest.SkipTest(
                f"Local PostgreSQL is unavailable ({type(exc).__name__})."
            ) from None
        if not allowed:
            raise unittest.SkipTest("PostgreSQL user cannot create a test schema.")

    def setUp(self) -> None:
        self.schema_name = "test_handoff_" + uuid4().hex
        self.account_id = "test-account-" + uuid4().hex
        with _connect() as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(self.schema_name)
                )
            )
        self.addCleanup(self._drop_test_schema)
        self.connections = _SchemaConnections(self.schema_name)
        self.repository = ConversationControlRepository(self.connections)

    def _drop_test_schema(self) -> None:
        # Only this test's random schema is eligible for recursive cleanup.
        self.assertRegex(self.schema_name, r"\Atest_handoff_[a-f0-9]{32}\Z")
        with _connect() as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(self.schema_name)
                )
            )

    def _initialize(self) -> None:
        self.repository.ensure_table()

    def _prepare(self, conversation_id: str) -> None:
        self.repository.record_user_message(
            account_id=self.account_id,
            conversation_id=conversation_id,
            thread_id=f"chatwoot:{self.account_id}:{conversation_id}",
            message_id="message-" + conversation_id,
            chatwoot_status="pending",
        )

    def _reserve(
        self,
        conversation_id: str,
        *,
        agents: list[str] | None = None,
        opened: dict[str, set[str]] | None = None,
        scope: str = "inbox-1",
        loader=None,
        capacity: int = 5,
    ) -> str | None:
        return self.repository.reserve_handoff_agent(
            account_id=self.account_id,
            conversation_id=conversation_id,
            scope_key=f"{self.account_id}:{scope}",
            candidate_agent_ids=agents or ["10", "11", "12"],
            load_open_assignments=(
                loader if loader is not None else lambda: opened or {}
            ),
            max_active_conversations=capacity,
            assigned_team_id="7",
        )

    def _active_reservations(self) -> list[tuple[str, str]]:
        with self.connections.acquire() as connection:
            return connection.execute(
                f"SELECT conversation_id, agent_id FROM {HANDOFF_RESERVATION_TABLE} "
                "WHERE account_id = %s AND active = TRUE "
                "ORDER BY conversation_id",
                (self.account_id,),
            ).fetchall()

    def test_first_start_and_upgrade_preserve_existing_conversation(self):
        self._initialize()
        self._prepare("1")
        with self.connections.acquire() as connection:
            tables = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                (self.schema_name,),
            ).fetchall()
            self.assertTrue(
                {CONVERSATION_CONTROL_TABLE, HANDOFF_CURSOR_TABLE, HANDOFF_RESERVATION_TABLE}
                <= {row[0] for row in tables}
            )
            # Simulate an existing deployment before the new handoff columns
            # and reservation tables were introduced, within the test schema.
            connection.execute(
                f"ALTER TABLE {CONVERSATION_CONTROL_TABLE} "
                "DROP COLUMN handoff_trigger_message_id"
            )
            connection.execute(f"DROP TABLE {HANDOFF_CURSOR_TABLE}")
            connection.execute(f"DROP TABLE {HANDOFF_RESERVATION_TABLE}")
        self.repository.ensure_table()
        self.repository.ensure_table()
        control = self.repository.get(
            account_id=self.account_id, conversation_id="1"
        )
        self.assertIsNotNone(control)
        self.assertEqual(control.last_user_message_id, "message-1")
        self.assertIsNone(control.handoff_trigger_message_id)
        self.assertEqual(self._reserve("1"), "10")

    def test_round_robin_is_persistent_and_retry_does_not_advance_cursor(self):
        self._initialize()
        for conversation in ("1", "2", "3", "4"):
            self._prepare(conversation)
        self.assertEqual(self._reserve("1"), "10")
        self.assertEqual(self._reserve("1"), "10")
        self.repository = ConversationControlRepository(self.connections)
        self.assertEqual(self._reserve("2"), "11")
        self.assertEqual(self._reserve("3"), "12")
        self.assertEqual(self._reserve("4"), "10")
        self.assertEqual(len(self._active_reservations()), 4)

    def test_capacity_five_counts_open_and_pending_across_scopes(self):
        self._initialize()
        for conversation in ("1", "2", "3"):
            self._prepare(conversation)
        opened = {"10": {"old-1", "old-2", "old-3", "old-4"}}
        self.assertEqual(
            self._reserve("1", agents=["10"], opened=opened, scope="inbox-1"),
            "10",
        )
        self.assertIsNone(
            self._reserve("2", agents=["10"], opened=opened, scope="inbox-2")
        )
        self.assertEqual(
            self._reserve("3", agents=["10", "11"], opened=opened, scope="inbox-2"),
            "11",
        )

    def test_open_conversations_and_reservations_are_deduplicated(self):
        self._initialize()
        for conversation in ("1", "2", "3"):
            self._prepare(conversation)
        self.assertEqual(self._reserve("1", agents=["10"]), "10")
        opened = {"10": {"1", "old-1", "old-2", "old-3"}}
        self.assertEqual(self._reserve("2", agents=["10"], opened=opened), "10")
        self.assertIsNone(self._reserve("3", agents=["10"], opened=opened))

    def test_load_failure_rolls_back_without_advancing_round_robin(self):
        self._initialize()
        self._prepare("1")

        def unavailable():
            raise RuntimeError("Chatwoot load lookup failed")

        with self.assertRaisesRegex(RuntimeError, "load lookup failed"):
            self._reserve("1", loader=unavailable)
        self.assertEqual(self._active_reservations(), [])
        self.assertEqual(self._reserve("1"), "10")

    def test_concurrent_cross_scope_requests_never_exceed_five(self):
        self._initialize()
        for index in range(12):
            self._prepare(str(index))
        gate = threading.Barrier(12)
        observed_lock = threading.Lock()
        callbacks_active = 0
        callbacks_peak = 0

        def live_load():
            nonlocal callbacks_active, callbacks_peak
            with observed_lock:
                callbacks_active += 1
                callbacks_peak = max(callbacks_peak, callbacks_active)
            try:
                time.sleep(0.02)
                return {}
            finally:
                with observed_lock:
                    callbacks_active -= 1

        def reserve(index: int):
            gate.wait(timeout=10)
            return self._reserve(
                str(index),
                agents=["10"],
                scope=f"inbox-{index % 3}",
                loader=live_load,
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(reserve, range(12)))
        self.assertEqual(results.count("10"), 5)
        self.assertEqual(results.count(None), 7)
        self.assertEqual(len(self._active_reservations()), 5)
        self.assertEqual(callbacks_peak, 1, "Load refresh must run under the account lock.")

    def test_open_release_preserves_assignment_and_resolved_clears_round(self):
        self._initialize()
        self._prepare("1")
        self.assertEqual(self._reserve("1", agents=["10"]), "10")
        control, transitioned = self.repository.request_handoff(
            account_id=self.account_id,
            conversation_id="1",
            thread_id=self.repository.get(
                account_id=self.account_id, conversation_id="1"
            ).active_thread_id,
            reason="explicit_keyword",
            message_id="message-1",
            chatwoot_status="pending",
        )
        self.assertTrue(transitioned)
        self.assertEqual(control.assigned_agent_id, "10")
        self.assertEqual(control.handoff_trigger_message_id, "message-1")
        self.repository.record_conversation_status(
            account_id=self.account_id, conversation_id="1", status="open"
        )
        self.repository.release_handoff_reservation(
            account_id=self.account_id, conversation_id="1"
        )
        self.assertEqual(self._active_reservations(), [])
        control = self.repository.get(
            account_id=self.account_id, conversation_id="1"
        )
        self.assertEqual(control.assigned_agent_id, "10")
        self.assertTrue(control.handoff_required)
        control = self.repository.record_conversation_status(
            account_id=self.account_id, conversation_id="1", status="resolved"
        )
        self.assertIsNone(control.assigned_agent_id)
        self.assertIsNone(control.assigned_team_id)
        self.assertIsNone(control.handoff_trigger_message_id)
        self.assertFalse(control.handoff_required)
        self.assertTrue(control.ai_enabled)

    def test_resolved_releases_pending_reservation(self):
        self._initialize()
        self._prepare("1")
        self._prepare("2")
        self.assertEqual(self._reserve("1", agents=["10"], capacity=1), "10")
        self.repository.request_handoff(
            account_id=self.account_id,
            conversation_id="1",
            thread_id=self.repository.get(
                account_id=self.account_id, conversation_id="1"
            ).active_thread_id,
            reason="explicit_keyword",
            message_id="message-1",
            chatwoot_status="pending",
        )
        self.repository.record_conversation_status(
            account_id=self.account_id, conversation_id="1", status="resolved"
        )
        self.assertEqual(self._active_reservations(), [])
        self.assertEqual(self._reserve("2", agents=["10"], capacity=1), "10")

    def _assignment_rows(self):
        with self.connections.acquire() as connection:
            return connection.execute(
                f"SELECT business_round_id,agent_id,trigger_message_id,assigned_at,evidence_kind,delivered_at "
                f"FROM {ASSIGNMENT_OUTBOX_TABLE} WHERE account_id=%s ORDER BY recorded_at,business_round_id",
                (self.account_id,),
            ).fetchall()

    def _handoff_round(self, conversation_id="1"):
        with self.connections.acquire() as connection:
            return connection.execute(
                f"SELECT handoff_round_id FROM {CONVERSATION_CONTROL_TABLE} "
                "WHERE account_id=%s AND conversation_id=%s",
                (self.account_id, conversation_id),
            ).fetchone()[0]

    def _prepare_assignment_round(self, *, trigger="101", agent="10", at=None):
        at = at or datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
        control = self.repository.record_user_message(
            account_id=self.account_id, conversation_id="1", thread_id="unused",
            message_id=trigger, chatwoot_status="pending", message_at=at,
        )
        self.assertEqual(self._reserve("1", agents=[agent]), agent)
        control, _ = self.repository.request_handoff(
            account_id=self.account_id, conversation_id="1", thread_id=control.active_thread_id,
            reason="explicit_keyword", message_id=trigger, chatwoot_status="pending",
        )
        self.assertEqual(self._handoff_round(), control.active_thread_id)
        return control

    def test_first_handoff_insert_records_its_round_and_release_clears_proof(self):
        self._initialize()
        control, transitioned = self.repository.request_handoff(
            account_id=self.account_id, conversation_id="1", thread_id="initial-round",
            reason="explicit_keyword", message_id="101", chatwoot_status="pending",
        )
        self.assertTrue(transitioned)
        self.assertEqual(self._handoff_round(), control.active_thread_id)
        self.repository.release_handoff(account_id=self.account_id, conversation_id="1", status="resolved")
        self.assertIsNone(self._handoff_round())

    def test_successful_assignment_journal_is_fixed_per_business_round_and_survives_release(self):
        self._initialize()
        at = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
        first = self._prepare_assignment_round(at=at)
        self.assertEqual(self._assignment_rows(), [], "Reservation alone is not a successful assignment")
        self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="note_created")
        self.assertEqual(self._assignment_rows(), [])
        self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="assignment_applied")
        original = self._assignment_rows()
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0][:3], (first.active_thread_id, "10", "101"))
        self.assertIsNotNone(original[0][3])
        self.assertEqual(original[0][4:], ("assignment_applied", None))
        # Even a later local assignment correction/retry cannot rewrite the first snapshot.
        with self.connections.acquire() as connection:
            connection.execute(f"UPDATE {CONVERSATION_CONTROL_TABLE} SET assigned_agent_id='11' WHERE account_id=%s AND conversation_id='1'", (self.account_id,))
        self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="assignment_applied")
        self.assertEqual(self._assignment_rows(), original)
        self.repository.record_conversation_status(account_id=self.account_id, conversation_id="1", status="resolved", event_at=at+timedelta(minutes=1))
        self.assertEqual(self._assignment_rows(), original)
        second = self._prepare_assignment_round(trigger="201", agent="12", at=at+timedelta(minutes=2))
        self.assertNotEqual(second.active_thread_id, first.active_thread_id)
        self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="assignment_applied")
        rows = self._assignment_rows()
        self.assertEqual(len(rows), 2)
        by_round = {row[0]: row for row in rows}
        self.assertEqual(by_round[first.active_thread_id], original[0])
        self.assertEqual(by_round[second.active_thread_id][1:3], ("12", "201"))

    def test_upgrade_only_backfills_proven_active_assignment_with_unknown_assignment_time(self):
        self._initialize()
        current = self._prepare_assignment_round(at=datetime.now(timezone.utc)-timedelta(minutes=1))
        with self.connections.acquire() as connection:
            # Simulate the old successful mark before the journal was deployed.
            connection.execute(f"UPDATE {CONVERSATION_CONTROL_TABLE} SET handoff_assignment_applied=TRUE WHERE account_id=%s AND conversation_id='1'", (self.account_id,))
            for conversation, status, trigger, agent, closed in (
                ("2", "resolved", "202", "10", None),
                ("3", "open", None, "10", None),
                ("4", "open", "404", "0", None),
                ("5", "open", "505", "10", datetime(2026, 10, 18, 9, tzinfo=timezone.utc)),
                ("6", "open", "606", "10", None),
            ):
                connection.execute(f"""INSERT INTO {CONVERSATION_CONTROL_TABLE}
                    (account_id,conversation_id,active_thread_id,handoff_required,handoff_assignment_applied,
                     assigned_agent_id,handoff_trigger_message_id,session_started_at,session_closed_at,last_chatwoot_status,handoff_round_id)
                    VALUES (%s,%s,%s,TRUE,TRUE,%s,%s,%s,%s,%s,%s)""",
                    (self.account_id, conversation, "old-round-"+conversation, agent, trigger,
                     datetime(2026, 10, 18, 8, tzinfo=timezone.utc), closed, status,
                     None if conversation == "6" else "old-round-"+conversation))
        self.repository.ensure_table()
        self.repository.ensure_table()
        rows = self._assignment_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], (current.active_thread_id, "10", "101"))
        self.assertIsNone(rows[0][3], "Upgrade must not invent the original assignment time")
        self.assertEqual(rows[0][4], "current_control_backfill")

    def test_upgrade_does_not_attach_an_old_handoff_to_a_new_idle_round(self):
        self._initialize()
        at = datetime.now(timezone.utc)-timedelta(hours=12)
        original = self._prepare_assignment_round(at=at)
        with self.connections.acquire() as connection:
            connection.execute(f"""UPDATE {CONVERSATION_CONTROL_TABLE}
                SET handoff_assignment_applied=TRUE
                WHERE account_id=%s AND conversation_id='1'""", (self.account_id,))
        current = self.repository.record_user_message(
            account_id=self.account_id, conversation_id="1", thread_id="unused",
            message_id="202", chatwoot_status="pending", message_at=at+timedelta(hours=6),
        )
        self.assertNotEqual(current.active_thread_id, original.active_thread_id)
        self.assertEqual(self._handoff_round(), original.active_thread_id)
        self.assertGreater(datetime.fromisoformat(current.handoff_at), current.session_started_at)
        # Processing time is later than the new source session; clocks provide no proof.
        repeated, transitioned = self.repository.request_handoff(
            account_id=self.account_id, conversation_id="1", thread_id=current.active_thread_id,
            reason="explicit_keyword", message_id="202", chatwoot_status="pending",
        )
        self.assertFalse(transitioned)
        self.assertEqual(repeated.active_thread_id, current.active_thread_id)
        self.assertEqual(self._handoff_round(), original.active_thread_id)
        self.repository.ensure_table()
        self.assertEqual(self._assignment_rows(), [])
        control = self.repository.get(account_id=self.account_id, conversation_id="1")
        self.assertEqual(control.active_thread_id, current.active_thread_id)
        self.assertEqual(control.assigned_agent_id, "10")
        self.assertTrue(control.handoff_required and control.handoff_assignment_applied)
        # Old controls without durable proof also stay unknown, regardless of clocks.
        with self.connections.acquire() as connection:
            connection.execute(f"UPDATE {CONVERSATION_CONTROL_TABLE} SET handoff_round_id=NULL WHERE account_id=%s AND conversation_id='1'", (self.account_id,))
        self.repository.ensure_table()
        self.assertEqual(self._assignment_rows(), [])
        # A direct, successful new assignment can still establish evidence without proof.
        self.assertTrue(self.repository.mark_handoff_action(
            account_id=self.account_id, conversation_id="1", action="assignment_applied",
            expected_round_id=current.active_thread_id, expected_agent_id="10",
        ))
        rows = self._assignment_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], current.active_thread_id)
        self.assertEqual(rows[0][4], "assignment_applied")
        self.assertIsNotNone(rows[0][3])

    def test_upgrade_backfill_uses_round_proof_instead_of_processing_clock(self):
        self._initialize()
        current = self._prepare_assignment_round(at=datetime.now(timezone.utc)+timedelta(hours=1))
        with self.connections.acquire() as connection:
            connection.execute(f"UPDATE {CONVERSATION_CONTROL_TABLE} SET handoff_assignment_applied=TRUE WHERE account_id=%s AND conversation_id='1'", (self.account_id,))
        self.repository.ensure_table()
        rows = self._assignment_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], current.active_thread_id)
        self.assertIsNone(rows[0][3])
        self.assertEqual(rows[0][4], "current_control_backfill")

    def test_failed_journal_insert_rolls_back_local_success_mark_for_safe_retry(self):
        self._initialize()
        self._prepare_assignment_round()
        from unittest.mock import patch
        with patch.object(self.repository, "_record_assignment_snapshot", side_effect=RuntimeError("test journal failure")):
            with self.assertRaises(RuntimeError):
                self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="assignment_applied")
        control = self.repository.get(account_id=self.account_id, conversation_id="1")
        self.assertFalse(control.handoff_assignment_applied)
        self.assertEqual(control.assigned_agent_id, "10")
        self.assertEqual(self._assignment_rows(), [])
        self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="assignment_applied")
        self.assertEqual(len(self._assignment_rows()), 1)

    def test_old_assignment_response_cannot_mark_or_journal_the_next_business_round(self):
        self._initialize()
        at = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
        old = self._prepare_assignment_round(at=at)
        self.repository.record_conversation_status(account_id=self.account_id, conversation_id="1", status="resolved", event_at=at+timedelta(minutes=1))
        new = self._prepare_assignment_round(trigger="201", agent="12", at=at+timedelta(minutes=2))
        self.assertFalse(self.repository.mark_handoff_action(
            account_id=self.account_id, conversation_id="1", action="assignment_applied",
            expected_round_id=old.active_thread_id, expected_agent_id="10"))
        self.assertEqual(self._assignment_rows(), [])
        self.assertFalse(self.repository.get(account_id=self.account_id, conversation_id="1").handoff_assignment_applied)
        self.assertFalse(self.repository.mark_handoff_action(
            account_id=self.account_id, conversation_id="1", action="assignment_applied",
            expected_round_id=new.active_thread_id, expected_agent_id="10"))
        self.assertTrue(self.repository.mark_handoff_action(
            account_id=self.account_id, conversation_id="1", action="assignment_applied",
            expected_round_id=new.active_thread_id, expected_agent_id="12"))
        rows = self._assignment_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], (new.active_thread_id, "12", "201"))

    def _assert_early_assignment_outbox_upgrade(self, *, has_created_at=False):
        self._initialize()
        control = self._prepare_assignment_round(at=datetime.now(timezone.utc)-timedelta(minutes=1))
        self.repository.mark_handoff_action(account_id=self.account_id, conversation_id="1", action="assignment_applied")
        with self.connections.acquire() as connection:
            original = connection.execute(f"""SELECT account_id,conversation_id,business_round_id,
                agent_id,trigger_message_id,session_started_at,assigned_at,delivered_at
                FROM {ASSIGNMENT_OUTBOX_TABLE}""").fetchone()
            # Reproduce the deployed eight-column source journal. Dropping its
            # new metadata also drops the metadata-dependent index/check.
            connection.execute(f"""ALTER TABLE {ASSIGNMENT_OUTBOX_TABLE}
                DROP COLUMN recorded_at, DROP COLUMN evidence_kind,
                DROP COLUMN delivery_error_code, DROP COLUMN delivery_failed_at,
                ALTER COLUMN assigned_at SET NOT NULL,
                ALTER COLUMN assigned_at SET DEFAULT CURRENT_TIMESTAMP""")
            recorded_at = original[6]
            if has_created_at:
                connection.execute(f"ALTER TABLE {ASSIGNMENT_OUTBOX_TABLE} ADD COLUMN created_at TIMESTAMPTZ")
                recorded_at -= timedelta(seconds=1)
                connection.execute(f"UPDATE {ASSIGNMENT_OUTBOX_TABLE} SET created_at=%s", (recorded_at,))
        self.repository.ensure_table()
        self.repository.ensure_table()
        with self.connections.acquire() as connection:
            upgraded = connection.execute(f"""SELECT account_id,conversation_id,business_round_id,
                agent_id,trigger_message_id,session_started_at,assigned_at,delivered_at,
                recorded_at,evidence_kind FROM {ASSIGNMENT_OUTBOX_TABLE}""").fetchone()
            self.assertEqual(upgraded[:8], original, "Upgrade must preserve identities and actual assignment time")
            self.assertEqual(upgraded[8:], (recorded_at, "assignment_applied"))
            # Future safe backfills retain unknown assignment times even when
            # the legacy table required assigned_at and defaulted it to now.
            connection.execute(f"""INSERT INTO {ASSIGNMENT_OUTBOX_TABLE}
                (account_id,conversation_id,business_round_id,agent_id,trigger_message_id,
                 session_started_at,assigned_at,evidence_kind)
                VALUES (%s,'2','unknown-backfill','11','202',%s,NULL,'current_control_backfill')""",
                (self.account_id, control.session_started_at))
            unknown = connection.execute(f"SELECT assigned_at,recorded_at FROM {ASSIGNMENT_OUTBOX_TABLE} WHERE business_round_id='unknown-backfill'").fetchone()
            self.assertIsNone(unknown[0])
            self.assertIsNotNone(unknown[1])

    def test_early_eight_column_assignment_outbox_is_upgraded_without_rewriting_evidence(self):
        self._assert_early_assignment_outbox_upgrade()

    def test_legacy_assignment_outbox_recording_time_uses_existing_created_at_when_available(self):
        self._assert_early_assignment_outbox_upgrade(has_created_at=True)


if __name__ == "__main__":
    unittest.main()
