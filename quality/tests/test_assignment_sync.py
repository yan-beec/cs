"""Assignment delivery durability, configuration and bounded-resource checks."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
import pytest

from ics_agent.app.chatwoot.state import ASSIGNMENT_OUTBOX_TABLE, ConversationControlRepository
from quality.assignment.config import AssignmentSyncSettings
from quality.assignment.repository import AssignmentSyncRepository, BATCH_SIZE, assignment_event
from quality.assignment.runtime import AssignmentSyncRuntime


NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)


def settings(**changes):
    values = dict(_env_file=None, assignment_sync_enabled=True, source_key="test-source",
                  allowed_account_ids="1,2", assignment_password="assignment-secret")
    return AssignmentSyncSettings(**{**values, **changes})


def assignment(**changes):
    return {
        "account_id": "1", "conversation_id": "12", "business_round_id": "chatwoot:1:12:round:one",
        "agent_id": "7", "trigger_message_id": "101", "session_started_at": NOW,
        "assigned_at": NOW, "recorded_at": NOW, "evidence_kind": "assignment_applied", **changes,
    }


def test_assignment_sync_is_disabled_without_credentials_by_default(monkeypatch):
    monkeypatch.delenv("ARCHIVE_ASSIGNMENT_SYNC_ENABLED", raising=False)
    configured = AssignmentSyncSettings(_env_file=None)
    assert not configured.assignment_sync_enabled
    assert configured.assignment_user == "archive_assignment"


@pytest.mark.parametrize("change", [
    {"source_key": ""}, {"source_key": " spaced "}, {"allowed_account_ids": ""},
    {"allowed_account_ids": "0"}, {"allowed_account_ids": "1,abc"},
    {"allowed_account_ids": str(2**63)}, {"assignment_password": ""},
])
def test_enabled_sync_requires_explicit_source_accounts_and_its_own_credential(change):
    with pytest.raises(ValidationError):
        settings(**change)


def test_sync_never_uses_the_ingestion_password(monkeypatch):
    monkeypatch.setenv("ARCHIVE_INGEST_PASSWORD", "ingest-must-stay-private")
    monkeypatch.delenv("ARCHIVE_ASSIGNMENT_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        AssignmentSyncSettings(_env_file=None, assignment_sync_enabled=True, source_key="s", allowed_account_ids="1")
    assert settings().connection_kwargs["password"] == "assignment-secret"


def test_assignment_payload_has_exact_scope_and_stable_retry_identity():
    configured = settings()
    row = assignment()
    event = assignment_event(configured, row)
    payload = json.loads(event.raw_body)
    assert payload["event"] == "ics_assignment_recorded"
    assert payload["account"] == {"id": 1}
    assert payload["conversation"] == {"id": 12, "account_id": 1}
    assert payload["assignment"] == {
        "schema_version": 1, "origin": "ics", "assignment_key": row["business_round_id"],
        "business_round_id": row["business_round_id"], "agent_id": "7", "trigger_message_id": "101",
        "session_started_at": NOW.isoformat(), "assigned_at": NOW.isoformat(),
        "recorded_at": NOW.isoformat(), "evidence_kind": "assignment_applied", "assigned_at_unknown": False,
    }
    assert event == assignment_event(configured, deepcopy(row))
    assert event.event_key.startswith("ics_assignment:")
    for change in ({"account_id": "2"}, {"conversation_id": "13"}, {"business_round_id": "new-round"}):
        assert assignment_event(configured, assignment(**change)).event_key != event.event_key
    assert assignment_event(settings(source_key="other"), row).event_key != event.event_key


def test_current_control_backfill_preserves_unknown_assignment_time():
    event = assignment_event(settings(), assignment(assigned_at=None, evidence_kind="current_control_backfill"))
    payload = json.loads(event.raw_body)
    assert payload["assignment"]["assigned_at"] is None
    assert payload["assignment"]["assigned_at_unknown"] is True
    assert payload["assignment"]["recorded_at"] == payload["updated_at"] == NOW.isoformat()
    assert event.source_time == NOW


@pytest.mark.parametrize("change", [
    {"account_id": "3"}, {"agent_id": "0"}, {"agent_id": "bot"}, {"trigger_message_id": "bad"},
    {"session_started_at": NOW.replace(tzinfo=None)}, {"business_round_id": ""},
    {"assigned_at": None}, {"evidence_kind": "current_control_backfill"},
])
def test_invalid_assignment_evidence_cannot_cross_the_archive_boundary(change):
    with pytest.raises((ValueError, TypeError)):
        assignment_event(settings(), assignment(**change))


def test_source_and_target_pools_are_independent_and_bounded():
    app = SimpleNamespace(postgres_dsn="", postgres_host="ics-db", postgres_port=5432,
        postgres_database="ics", postgres_user="ics-app", postgres_password="existing-ics-password", postgres_sslmode="disable")
    with patch("quality.assignment.repository.ConnectionPool") as pool:
        repository = AssignmentSyncRepository(settings(), app)
    assert pool.call_count == 2
    source, target = [call.kwargs for call in pool.call_args_list]
    for configuration in (source, target):
        assert configuration["min_size"] == 0 and configuration["max_size"] == 1
        assert configuration["max_waiting"] == 1 and configuration["num_workers"] == 1
        assert configuration["timeout"] == 1 and configuration["open"] is False
        assert configuration["kwargs"]["connect_timeout"] == 3
        assert "statement_timeout=2000" in configuration["kwargs"]["options"]
        assert "lock_timeout=1000" in configuration["kwargs"]["options"]
    assert source["kwargs"]["user"] == "ics-app"
    assert target["kwargs"]["user"] == "archive_assignment"
    assert target["kwargs"]["password"] == "assignment-secret"
    repository.close()


def test_source_dsn_reuses_existing_ics_connection_settings_with_bounded_overrides():
    with patch("quality.assignment.repository.ConnectionPool") as pool:
        repository = AssignmentSyncRepository(settings(), SimpleNamespace(postgres_dsn="postgresql://ics-db/ics"))
    source = pool.call_args_list[0].kwargs
    assert source["conninfo"] == "postgresql://ics-db/ics"
    assert source["kwargs"]["connect_timeout"] == 3
    repository.close()


class TargetPool:
    """A transactional target double that exposes actual commit/rollback order."""
    def __init__(self, trace):
        self.trace, self.events, self.jobs = trace, {}, set()
        self.fail_job = False
        self.fail_commit = False

    @contextmanager
    def connection(self):
        pending_events, pending_jobs = deepcopy(self.events), set(self.jobs)
        pool = self

        class Connection:
            def execute(self, sql, values):
                if "INSERT INTO archive.events" in sql:
                    source, account, key, conversation, at, raw, matching_raw = values
                    assert raw == matching_raw
                    assert "'ics_assignment_recorded','backfill'" in sql
                    identity = (source, account, key)
                    row = None
                    if identity not in pending_events:
                        row = {"id": len(pending_events)+1, "raw_body": raw}
                        pending_events[identity] = row
                    return SimpleNamespace(fetchone=lambda: row)
                if "SELECT id,raw_body" in sql:
                    return SimpleNamespace(fetchone=lambda: pending_events.get(tuple(values)))
                assert "INSERT INTO ops.event_jobs" in sql
                if pool.fail_job:
                    raise OSError("target job unavailable")
                pending_jobs.add(values[0])

        try:
            yield Connection()
            if self.fail_commit:
                raise OSError("target commit unavailable")
        except BaseException:
            self.trace.append("target_rollback")
            raise
        else:
            self.events, self.jobs = pending_events, pending_jobs
            self.trace.append("target_commit")


class SourcePool:
    def __init__(self, rows, trace):
        self.rows, self.trace, self.delivered = rows, trace, set()
        self.blocked = {}
        self.fail_ack = False
        self.fail_quarantine = False
        self.selected_limits = []
        self.count_queries = 0

    @contextmanager
    def connection(self):
        pending_ack = set()
        pending_blocked = {}
        pool = self

        class Connection:
            def execute(self, sql, values):
                if "SELECT COUNT(*)" in sql:
                    pool.count_queries += 1
                    accounts, = values
                    remaining = [row for row in pool.rows if row["business_round_id"] not in pool.delivered and row["account_id"] in accounts]
                    blocked_count = sum(row["business_round_id"] in pool.blocked for row in remaining)
                    row = {"pending_count": len(remaining) - blocked_count, "blocked_count": blocked_count}
                    return SimpleNamespace(fetchone=lambda: row)
                if "SELECT account_id" in sql:
                    accounts, limit = values
                    assert limit == BATCH_SIZE and accounts == ["1", "2"]
                    assert "delivered_at IS NULL AND delivery_error_code IS NULL AND account_id=ANY(%s)" in sql
                    assert "OFFSET" not in sql.upper()
                    pool.selected_limits.append(limit)
                    rows = [row for row in pool.rows if row["business_round_id"] not in pool.delivered and row["business_round_id"] not in pool.blocked and row["account_id"] in accounts][:limit]
                    return SimpleNamespace(fetchall=lambda: rows)
                assert f"UPDATE {ASSIGNMENT_OUTBOX_TABLE}" in sql
                if "SET delivery_error_code" in sql:
                    code, account, conversation, business_round = values
                    assert code in {"evidence_conflict", "invalid_evidence"}
                    assert "delivery_failed_at=CURRENT_TIMESTAMP" in sql
                    assert "AND delivered_at IS NULL AND delivery_error_code IS NULL" in sql
                    pool.trace.append("source_quarantine")
                    if pool.fail_quarantine:
                        raise OSError("source isolation unavailable")
                    changed = business_round not in pool.delivered and business_round not in pool.blocked
                    if changed:
                        pending_blocked[business_round] = code
                    return SimpleNamespace(rowcount=int(changed))
                assert "business_round_id=%s AND delivered_at IS NULL" in sql
                pool.trace.append("source_ack")
                if pool.fail_ack:
                    raise OSError("source ack unavailable")
                pending_ack.add(values[2])

        yield Connection()
        self.delivered.update(pending_ack)
        self.blocked.update(pending_blocked)
        if pending_ack or pending_blocked:
            self.trace.append("source_commit")


def delivery_repository(rows=None):
    trace = []
    repository = object.__new__(AssignmentSyncRepository)
    repository.settings = settings()
    repository.source_pool = SourcePool(rows or [assignment()], trace)
    repository.target_pool = TargetPool(trace)
    return repository, trace


def test_evidence_and_processing_job_commit_before_source_acknowledgement():
    repository, trace = delivery_repository()
    assert repository.sync_once() == 1
    assert trace == ["target_commit", "source_ack", "source_commit"]
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1
    assert repository.sync_once() == 0


@pytest.mark.parametrize("failure", ["fail_job", "fail_commit"])
def test_target_failure_keeps_source_pending_and_retries_atomically(failure):
    repository, trace = delivery_repository()
    setattr(repository.target_pool, failure, True)
    with pytest.raises(OSError):
        repository.sync_once()
    assert trace == ["target_rollback"]
    assert not repository.source_pool.delivered
    assert not repository.source_pool.blocked
    assert not repository.target_pool.events and not repository.target_pool.jobs
    setattr(repository.target_pool, failure, False)
    assert repository.sync_once() == 1
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1


def test_committed_target_and_failed_ack_retry_without_duplicate_events_or_jobs():
    repository, trace = delivery_repository()
    repository.source_pool.fail_ack = True
    with pytest.raises(OSError):
        repository.sync_once()
    assert trace[:2] == ["target_commit", "source_ack"]
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1
    assert not repository.source_pool.delivered
    assert not repository.source_pool.blocked
    repository.source_pool.fail_ack = False
    assert repository.sync_once() == 1
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1


@pytest.mark.parametrize("backfill", [False, True])
def test_failed_ack_retry_has_identical_bytes_after_database_timezone_changes(backfill):
    row = assignment(**({"assigned_at": None, "evidence_kind": "current_control_backfill"} if backfill else {}))
    repository, _ = delivery_repository([row])
    repository.source_pool.fail_ack = True
    with pytest.raises(OSError):
        repository.sync_once()
    original = assignment_event(repository.settings, row)
    plus_eight = timezone(timedelta(hours=8))
    for field in ("session_started_at", "assigned_at", "recorded_at"):
        if row[field] is not None:
            row[field] = row[field].astimezone(plus_eight)
    retried = assignment_event(repository.settings, row)
    assert retried.raw_body == original.raw_body
    assert retried.event_key == original.event_key
    assert retried.source_time.tzinfo == timezone.utc
    repository.source_pool.fail_ack = False
    assert repository.sync_once() == 1
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1
    assert not repository.source_pool.blocked


def conflicting_repository(rows=None):
    repository, trace = delivery_repository(rows)
    row = repository.source_pool.rows[0]
    event = assignment_event(repository.settings, row)
    identity = (repository.settings.source_key, event.account_id, event.event_key)
    repository.target_pool.events[identity] = {"id": 1, "raw_body": b'{"different":"evidence"}'}
    return repository, identity, trace


def test_conflicting_duplicate_keeps_original_evidence_and_does_not_block_valid_rows_or_restart():
    rows = [assignment(business_round_id=f"round-{index}") for index in range(25)]
    repository, identity, _ = conflicting_repository(rows)
    original_source = deepcopy(rows[0])
    original_target = deepcopy(repository.target_pool.events[identity])
    assert repository.sync_once() == 19
    assert repository.last_batch_blocked == 1
    assert repository.source_pool.blocked == {"round-0": "evidence_conflict"}
    assert "round-0" not in repository.source_pool.delivered
    assert repository.target_pool.events[identity] == original_target
    assert rows[0] == original_source
    restarted, _ = delivery_repository()
    restarted.source_pool = repository.source_pool
    restarted.target_pool = repository.target_pool
    assert restarted.sync_once() == 5
    assert restarted.last_batch_blocked == 0
    assert restarted.sync_once() == 0
    assert restarted.source_pool.selected_limits == [20, 20, 20]
    assert len(restarted.target_pool.jobs) == 24
    assert restarted.queue_counts() == {"pending_count": 0, "blocked_count": 1}


def test_invalid_evidence_is_isolated_without_rewriting_source_or_stopping_other_rows():
    rows = [assignment(agent_id="0"), assignment(business_round_id="valid-round")]
    repository, _ = delivery_repository(rows)
    before = deepcopy(rows)
    assert repository.sync_once() == 1
    assert repository.source_pool.blocked == {rows[0]["business_round_id"]: "invalid_evidence"}
    assert repository.source_pool.delivered == {"valid-round"}
    assert rows == before
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1


def test_failed_source_isolation_is_not_acknowledged_and_keeps_retrying():
    rows = [assignment(), assignment(business_round_id="valid-round")]
    repository, identity, _ = conflicting_repository(rows)
    original = deepcopy(repository.target_pool.events)
    repository.source_pool.fail_quarantine = True
    with pytest.raises(OSError):
        repository.sync_once()
    assert repository.target_pool.events == original
    assert not repository.source_pool.blocked and not repository.source_pool.delivered
    assert not repository.target_pool.jobs
    repository.source_pool.fail_quarantine = False
    assert repository.sync_once() == 1
    assert repository.source_pool.blocked == {rows[0]["business_round_id"]: "evidence_conflict"}
    assert repository.target_pool.events[identity] == original[identity]


def test_delivery_has_a_twenty_record_batch_and_stops_between_rows():
    rows = [assignment(business_round_id=f"round-{index}") for index in range(25)]
    repository, _ = delivery_repository(rows)
    assert repository.sync_once() == 20
    assert repository.sync_once() == 5
    stop = threading.Event()
    stop.set()
    repository, trace = delivery_repository()
    assert repository.sync_once(stop) == 0 and not trace


def test_outbox_write_is_in_the_same_local_transaction_as_success_mark():
    trace = []
    cursor = Mock()
    cursor.execute.side_effect = lambda query, values: trace.append("journal" if "INSERT INTO" in query else "mark")
    connection = Mock()
    connection.cursor.return_value = cursor

    @contextmanager
    def transaction():
        trace.append("begin")
        yield
        trace.append("commit")

    connection.transaction.side_effect = transaction
    repository = ConversationControlRepository(SimpleNamespace(acquire=lambda: connection))
    assert repository.mark_handoff_action(account_id="1", conversation_id="12", action="assignment_applied")
    assert trace == ["begin", "mark", "journal", "commit"]
    query, values = cursor.execute.call_args.args
    assert "handoff_assignment_applied=TRUE AND handoff_required=TRUE" in query
    assert "session_closed_at < session_started_at" in query
    assert "<> 'resolved'" in query
    assert "ON CONFLICT (account_id, conversation_id, business_round_id) DO NOTHING" in query
    assert values == ("1", "12")


def test_assignment_round_or_agent_mismatch_never_creates_a_journal_record():
    connection = Mock()
    connection.transaction.return_value.__enter__ = Mock()
    connection.transaction.return_value.__exit__ = Mock(return_value=False)
    cursor = connection.cursor.return_value
    cursor.rowcount = 0
    repository = ConversationControlRepository(SimpleNamespace(acquire=lambda: connection))
    with patch.object(repository, "_record_assignment_snapshot") as journal:
        assert repository.mark_handoff_action(account_id="1", conversation_id="12", action="assignment_applied",
            expected_round_id="old-round", expected_agent_id="7") is False
        journal.assert_not_called()
    query, values = cursor.execute.call_args.args
    assert "active_thread_id=%s AND assigned_agent_id=%s" in query
    assert values == (None, "1", "12", "old-round", "7")


def test_upgrade_snapshot_requires_durable_handoff_proof_for_the_current_round():
    cursor = Mock()
    ConversationControlRepository._record_assignment_snapshot(cursor, backfill=True)
    sql, values = cursor.execute.call_args.args
    assert "handoff_round_id = active_thread_id" in sql
    assert "handoff_at" not in sql
    assert "'current_control_backfill'" in sql and values == ()
    ConversationControlRepository._record_assignment_snapshot(cursor, "1", "12")
    normal_sql, _ = cursor.execute.call_args.args
    assert "handoff_round_id" not in normal_sql


def test_ensure_table_upgrades_early_outbox_metadata_without_assigning_fake_times():
    connection = Mock()
    connection.transaction.return_value.__enter__ = Mock()
    connection.transaction.return_value.__exit__ = Mock(return_value=False)
    repository = ConversationControlRepository(SimpleNamespace(acquire=lambda: connection))
    repository.ensure_table()
    statements = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
    alter = next(sql for sql in statements if "ADD COLUMN IF NOT EXISTS recorded_at" in sql)
    assert "ADD COLUMN IF NOT EXISTS evidence_kind" in alter
    assert "ADD COLUMN IF NOT EXISTS delivery_error_code TEXT" in alter
    assert "ADD COLUMN IF NOT EXISTS delivery_failed_at TIMESTAMPTZ" in alter
    assert "ALTER COLUMN assigned_at DROP NOT NULL" in alter
    assert "ALTER COLUMN assigned_at DROP DEFAULT" in alter
    recording = next(sql for sql in statements if "SET recorded_at = COALESCE" in sql)
    assert "to_jsonb(snapshot) ->> 'created_at'" in recording
    assert "assigned_at, CURRENT_TIMESTAMP" in recording
    assert "WHERE recorded_at IS NULL" in recording
    assert not any("SET assigned_at" in sql for sql in statements)
    assert any("ADD COLUMN IF NOT EXISTS handoff_round_id TEXT" in sql for sql in statements)
    assert any("WHERE delivered_at IS NULL AND delivery_error_code IS NULL" in sql for sql in statements)


@pytest.mark.parametrize("action", ["label_applied", "note_created", "conversation_updated", "notice_sent"])
def test_other_handoff_marks_never_create_successful_assignment_evidence(action):
    connection = Mock()
    connection.transaction.return_value.__enter__ = Mock()
    connection.transaction.return_value.__exit__ = Mock(return_value=False)
    repository = ConversationControlRepository(SimpleNamespace(acquire=lambda: connection))
    with patch.object(repository, "_record_assignment_snapshot") as journal:
        assert repository.mark_handoff_action(account_id="1", conversation_id="12", action=action)
        journal.assert_not_called()


def test_disabled_runtime_allocates_no_thread_or_database_pool():
    async def check():
        runtime = AssignmentSyncRuntime()
        with patch("quality.assignment.runtime.AssignmentSyncSettings", return_value=AssignmentSyncSettings(_env_file=None, assignment_sync_enabled=False)), patch("quality.assignment.runtime.AssignmentSyncRepository") as repository:
            await runtime.start()
            assert runtime.task is None and runtime.executor is None
            repository.assert_not_called()
            await runtime.close()
    asyncio.run(check())


def test_bad_configuration_is_isolated_and_logged_without_credential_values(caplog):
    async def check():
        runtime = AssignmentSyncRuntime()
        with patch("quality.assignment.runtime.AssignmentSyncSettings", side_effect=ValueError("secret-must-not-appear")):
            await runtime.start()
            assert runtime.status == "configuration_error" and runtime.executor is None
            await runtime.close()
    asyncio.run(check())
    assert "secret-must-not-appear" not in caplog.text


def runtime_repository():
    repository = Mock(last_batch_blocked=0, last_batch_delivered=0)
    repository.queue_counts.return_value = {"pending_count": 0, "blocked_count": 0}
    return repository


def test_background_delivery_retries_with_backoff_and_recovers_without_touching_ics(caplog):
    async def check():
        runtime = AssignmentSyncRuntime()
        repository = runtime_repository()
        repository.sync_once.side_effect = [OSError("secret-failure-detail"), 1]
        recovered = asyncio.Event()
        delays = []

        async def wait_interval(seconds):
            delays.append(seconds)
            if len(delays) == 1:
                assert runtime.status == "unavailable"
                return
            assert runtime.status == "ready"
            recovered.set()
            await asyncio.Event().wait()

        with patch("quality.assignment.runtime.AssignmentSyncSettings", return_value=settings()), patch("quality.assignment.runtime.AssignmentSyncRepository", return_value=repository), patch("quality.assignment.runtime.asyncio.sleep", side_effect=wait_interval):
            await runtime.start()
            await asyncio.wait_for(recovered.wait(), timeout=2)
            assert delays == [10, 5]
            assert repository.sync_once.call_count == 2
            await runtime.close()
            repository.close.assert_called_once()
    asyncio.run(check())
    assert "secret-failure-detail" not in caplog.text


def test_sync_connect_failure_cannot_block_startup_and_closes_its_pools(caplog):
    async def check():
        runtime = AssignmentSyncRuntime()
        entered, release = threading.Event(), threading.Event()
        repository = runtime_repository()

        def blocked_open():
            entered.set()
            release.wait(timeout=2)
            raise OSError("secret-connection-string")

        repository.open.side_effect = blocked_open
        with patch("quality.assignment.runtime.AssignmentSyncSettings", return_value=settings()), patch("quality.assignment.runtime.AssignmentSyncRepository", return_value=repository):
            await asyncio.wait_for(runtime.start(), timeout=0.2)
            assert runtime.executor._max_workers == 1
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert runtime.status == "connecting"
            finally:
                release.set()
                await runtime.close()
            repository.close.assert_called_once()
            assert runtime.executor is None and runtime.task is None
    asyncio.run(check())
    assert "secret-connection-string" not in caplog.text


def test_shutdown_waits_for_the_inflight_commit_before_closing_pools():
    async def check():
        runtime = AssignmentSyncRuntime()
        entered, release = threading.Event(), threading.Event()
        repository = runtime_repository()

        def blocked_sync(stopping):
            entered.set()
            release.wait(timeout=2)
            assert stopping.is_set()

        repository.sync_once.side_effect = blocked_sync
        with patch("quality.assignment.runtime.AssignmentSyncSettings", return_value=settings()), patch("quality.assignment.runtime.AssignmentSyncRepository", return_value=repository):
            await runtime.start()
            assert await asyncio.to_thread(entered.wait, 1)
            shutdown = asyncio.create_task(runtime.close())
            try:
                await asyncio.sleep(0)
                repository.close.assert_not_called()
                assert not shutdown.done()
            finally:
                release.set()
                await asyncio.wait_for(shutdown, timeout=2)
            repository.close.assert_called_once()
    asyncio.run(check())


def test_runtime_samples_queue_counts_once_a_minute_and_tracks_new_isolation(monkeypatch):
    rows = [assignment(business_round_id=f"round-{index}") for index in range(25)]
    repository, _, _ = conflicting_repository(rows)
    runtime = AssignmentSyncRuntime()
    runtime.repository = repository
    clock = Mock(return_value=100)
    monkeypatch.setattr("quality.assignment.runtime.time.monotonic", clock)
    assert runtime._work(settings()) == 19
    assert (runtime.pending_count, runtime.blocked_count) == (5, 1)
    sampled_at = runtime.counts_sampled_at
    assert datetime.fromisoformat(sampled_at).tzinfo == timezone.utc
    assert runtime._work(settings()) == 5
    assert (runtime.pending_count, runtime.blocked_count) == (0, 1)
    repository.source_pool.rows.append(assignment(agent_id="0", business_round_id="invalid-later"))
    assert runtime._work(settings()) == 0
    assert runtime.blocked_count == 2
    assert repository.source_pool.count_queries == 1
    assert runtime.counts_sampled_at == sampled_at
    clock.return_value = 161
    runtime._work(settings())
    assert repository.source_pool.count_queries == 2
    assert (runtime.pending_count, runtime.blocked_count) == (0, 2)


def test_runtime_retains_isolation_counts_when_a_later_row_has_a_transient_failure():
    rows = [assignment(agent_id="0"), assignment(business_round_id="valid-round")]
    repository, _ = delivery_repository(rows)
    repository.target_pool.fail_job = True
    runtime = AssignmentSyncRuntime()
    runtime.repository = repository
    with pytest.raises(OSError):
        runtime._work(settings())
    assert (runtime.pending_count, runtime.blocked_count) == (None, 1)
    assert repository.source_pool.blocked == {rows[0]["business_round_id"]: "invalid_evidence"}
    assert not repository.source_pool.delivered
    repository.target_pool.fail_job = False
    assert runtime._work(settings()) == 1
    assert (runtime.pending_count, runtime.blocked_count) == (0, 1)


def test_failed_queue_health_sample_does_not_prevent_source_acknowledgement():
    repository, _ = delivery_repository()
    repository.queue_counts = Mock(side_effect=[OSError("source statistics unavailable"),
        {"pending_count": 0, "blocked_count": 0}])
    runtime = AssignmentSyncRuntime()
    runtime.repository = repository
    with pytest.raises(OSError):
        runtime._work(settings())
    assert repository.source_pool.delivered == {assignment()["business_round_id"]}
    assert len(repository.target_pool.events) == len(repository.target_pool.jobs) == 1
    assert runtime.pending_count is None and runtime.counts_sampled_at is None
    assert runtime._work(settings()) == 0
    assert (runtime.pending_count, runtime.blocked_count) == (0, 0)


def test_runtime_reports_degraded_after_restart_when_source_contains_isolated_rows():
    async def check():
        repository, _, _ = conflicting_repository()
        assert repository.sync_once() == 0
        repository.open = Mock()
        repository.close = Mock()
        runtime = AssignmentSyncRuntime()
        sampled = asyncio.Event()

        async def wait_interval(seconds):
            assert seconds == 5
            assert runtime.status == "degraded"
            assert (runtime.pending_count, runtime.blocked_count) == (0, 1)
            sampled.set()
            await asyncio.Event().wait()

        with patch("quality.assignment.runtime.AssignmentSyncSettings", return_value=settings()), patch("quality.assignment.runtime.AssignmentSyncRepository", return_value=repository), patch("quality.assignment.runtime.asyncio.sleep", side_effect=wait_interval):
            await runtime.start()
            await asyncio.wait_for(sampled.wait(), timeout=2)
            await runtime.close()
            repository.close.assert_called_once()
    asyncio.run(check())
