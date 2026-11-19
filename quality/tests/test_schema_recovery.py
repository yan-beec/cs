"""Control-schema recovery uses no real database or application services."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ics_agent.app.chatwoot import schema_recovery
from ics_agent.app.chatwoot.schema_recovery import ConversationControlSchemaRecovery


def source_settings(**changes):
    return SimpleNamespace(**{
        "postgres_dsn": "", "postgres_host": "ics-db", "postgres_port": 5432,
        "postgres_database": "ics", "postgres_user": "ics-user",
        "postgres_password": "ics-secret", "postgres_sslmode": "disable", **changes,
    })


def test_successful_startup_needs_no_recovery_resources():
    async def check():
        recovery = ConversationControlSchemaRecovery()
        with patch.object(schema_recovery, "ConnectionPool") as pool, patch.object(schema_recovery, "ThreadPoolExecutor") as executor:
            await recovery.close()
            assert recovery.pool is None and recovery.executor is None and recovery.task is None
            pool.assert_not_called()
            executor.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize("dsn", ["", "postgresql://ics-db/ics"])
def test_recovery_has_an_independent_bounded_source_pool(dsn):
    with patch.object(schema_recovery, "ConnectionPool") as factory:
        schema_recovery._recovery_pool(source_settings(postgres_dsn=dsn))
    kwargs = factory.call_args.kwargs
    assert {key: kwargs[key] for key in ("min_size", "max_size", "timeout", "max_waiting", "num_workers", "open")} == {
        "min_size": 0, "max_size": 1, "timeout": 1, "max_waiting": 1, "num_workers": 1, "open": False,
    }
    assert kwargs["kwargs"]["connect_timeout"] == 3
    assert kwargs["kwargs"]["options"] == "-c statement_timeout=2000 -c lock_timeout=1000"
    assert kwargs["kwargs"]["application_name"] == "ics-control-schema-recovery"
    if dsn:
        assert kwargs["conninfo"] == dsn
    else:
        assert kwargs["kwargs"]["password"] == "ics-secret"


def test_recovery_provider_returns_its_connection_to_the_temporary_pool():
    pool = Mock()
    pool.getconn.return_value.broken = False
    pool.getconn.return_value.closed = False
    connection = schema_recovery._RecoveryConnections(pool).acquire()
    pool.getconn.assert_called_once_with(timeout=1)
    connection.close()
    connection.close()
    pool.getconn.return_value.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(pool.getconn.return_value)


@pytest.mark.parametrize("failures,expected_delays", [(1, [5, 10]), (6, [5, 10, 20, 40, 60, 60, 60])])
def test_recovery_retries_failed_schema_then_releases_all_resources(caplog, failures, expected_delays):
    async def check():
        recovery = ConversationControlSchemaRecovery()
        pool = Mock()
        repository = Mock()
        threads, delays = [], []

        def initialize():
            threads.append(threading.current_thread().name)
            if len(threads) <= failures:
                raise OSError("secret-database-detail")

        async def interval(seconds):
            delays.append(seconds)

        repository.ensure_table.side_effect = initialize
        with patch.object(schema_recovery, "_recovery_pool", return_value=pool) as factory, patch.object(schema_recovery, "ConversationControlRepository", return_value=repository), patch.object(schema_recovery.asyncio, "sleep", side_effect=interval):
            await recovery.start(source_settings())
            assert recovery.executor._max_workers == 1
            await asyncio.wait_for(recovery.task, timeout=2)
            assert delays == expected_delays
            assert recovery.status == "ready"
            assert all(name.startswith("ics-schema-recovery") for name in threads)
            factory.assert_called_once()
            pool.open.assert_called_once_with(wait=False)
            pool.close.assert_called_once()
            assert recovery.executor is None and recovery.pool is None
            await recovery.close()
            assert recovery.task is None
    asyncio.run(check())
    assert "secret-database-detail" not in caplog.text


def test_recovery_start_is_nonblocking_and_shutdown_waits_for_schema_transaction(monkeypatch):
    async def check():
        recovery = ConversationControlSchemaRecovery()
        pool, repository = Mock(), Mock()
        entered, release = threading.Event(), threading.Event()

        def initialize():
            entered.set()
            release.wait(timeout=2)

        repository.ensure_table.side_effect = initialize
        monkeypatch.setattr(schema_recovery, "RETRY_SECONDS", 0)
        with patch.object(schema_recovery, "_recovery_pool", return_value=pool), patch.object(schema_recovery, "ConversationControlRepository", return_value=repository):
            await asyncio.wait_for(recovery.start(source_settings()), timeout=0.2)
            assert await asyncio.to_thread(entered.wait, 1)
            shutdown = asyncio.create_task(recovery.close())
            try:
                await asyncio.sleep(0)
                assert not shutdown.done()
                pool.close.assert_not_called()
            finally:
                release.set()
                await asyncio.wait_for(shutdown, timeout=2)
            pool.close.assert_called_once()
            assert recovery.executor is None and recovery.pool is None and recovery.task is None
    asyncio.run(check())


def test_immediate_shutdown_before_the_background_task_runs_still_releases_executor():
    async def check():
        recovery = ConversationControlSchemaRecovery()
        with patch.object(schema_recovery, "_recovery_pool") as pool:
            await recovery.start(source_settings())
            executor = recovery.executor
            await recovery.close()
            assert executor._shutdown
            assert recovery.executor is None and recovery.pool is None and recovery.task is None
            pool.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize("schema_fails", [False, True])
def test_application_starts_existing_workers_and_recovery_only_after_initial_schema_failure(monkeypatch, schema_fails):
    from ics_agent.app import main

    memory = SimpleNamespace(startup=AsyncMock(), start_cleanup_task=AsyncMock(), shutdown=AsyncMock())
    query = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    recovery = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(main, "get_conversation_memory_manager", lambda: memory)
    monkeypatch.setattr(main, "get_query_runtime", lambda: query)
    monkeypatch.setattr(main, "ConversationControlSchemaRecovery", lambda: recovery)
    monkeypatch.setattr(main, "settings", SimpleNamespace(reminder_enabled=False))
    monkeypatch.setattr(main, "rag_settings", SimpleNamespace(rag_warm_up_on_startup=False))
    monkeypatch.setattr(main, "list_channels", lambda: [])
    monkeypatch.setattr(main, "ensure_admin_user", Mock())
    for name in ("webhook_repository", "contact_block_repository"):
        monkeypatch.setattr(main, name, SimpleNamespace(ensure_table=Mock()))
    ensure = Mock(side_effect=OSError("initial schema unavailable") if schema_fails else None)
    monkeypatch.setattr(main, "conversation_control_repository", SimpleNamespace(ensure_table=ensure))
    for name in ("start_chatwoot_webhook_workers", "stop_chatwoot_webhook_workers", "start_assignment_sync", "stop_assignment_sync"):
        monkeypatch.setattr(main, name, AsyncMock())

    async def check():
        async with main.lifespan(main.app):
            main.start_chatwoot_webhook_workers.assert_awaited_once()
            main.start_assignment_sync.assert_awaited_once()
            if schema_fails:
                recovery.start.assert_awaited_once_with(main.settings)
            else:
                recovery.start.assert_not_awaited()
        ensure.assert_called_once()
        recovery.close.assert_awaited_once()
        main.stop_chatwoot_webhook_workers.assert_awaited_once()
        main.stop_assignment_sync.assert_awaited_once()
    asyncio.run(check())
