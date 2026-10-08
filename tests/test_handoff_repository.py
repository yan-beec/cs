"""Run the handoff SQL against PostgreSQL in a disposable, isolated schema."""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from ics_agent.app.chatwoot.state import (
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


if __name__ == "__main__":
    unittest.main()
