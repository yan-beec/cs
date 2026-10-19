"""Database-owned cleanup schedule, progress and failure boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from quality.presence.runtime import PresenceBusy, PresenceRuntime


NOW = datetime(2026, 10, 15, 20, tzinfo=timezone.utc)


def progress(*, status="completed", batches=1, samples=0, periods=0, total_samples=None,
             total_periods=None, day=date(2026, 10, 16)):
    return {"deleted_samples": samples, "deleted_periods": periods, "cleanup_status": status,
            "cleanup_day": day, "cutoff_at": "2026-08-31T20:00:00+00:00", "batches": batches,
            "total_deleted_samples": samples if total_samples is None else total_samples,
            "total_deleted_periods": periods if total_periods is None else total_periods,
            "last_success_at": NOW.isoformat() if batches else None}


def collector():
    runtime = PresenceRuntime()
    runtime.enabled = True
    runtime.status = "ready"
    runtime.settings = SimpleNamespace(source_key="local", presence_max_agents=2)
    runtime.executor = Mock()
    runtime.slot = asyncio.Semaphore(1)
    runtime.query_runtime = SimpleNamespace(
        directory_presence=AsyncMock(return_value={"status": "unavailable"}),
        get_directory=Mock(return_value=SimpleNamespace(configured=True, account_id=7)),
    )
    runtime._execute = AsyncMock(return_value=progress())
    return runtime


def observation(minute=0, count=1):
    return {"status": "ready", "source_key": "local", "account_id": 7,
            "sampled_at": (NOW + timedelta(minutes=minute, seconds=5)).isoformat(),
            "agents": [{"agent_id": str(index + 1), "availability_status": "online"}
                       for index in range(count)]}


def freeze_now(monkeypatch, at=NOW):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromisoformat(at.isoformat())
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr("quality.presence.runtime.datetime", FixedDatetime)


@pytest.mark.parametrize("host_time", [NOW - timedelta(hours=1), NOW + timedelta(hours=12)])
def test_database_clock_and_saved_progress_override_application_clock(host_time):
    async def check():
        runtime = collector()
        runtime._execute.return_value = progress(status="pending", batches=34, samples=2000,
                                                 total_samples=68000)
        await runtime._maybe_cleanup(host_time)
        assert runtime._execute.call_args.args == (runtime._repository_call, "cleanup", "local", 7)
        assert runtime.cleanup_day == date(2026, 10, 16)
        assert runtime.cleanup_batches == 34 and runtime.cleanup_deleted_samples == 68000
        assert runtime.cleanup_status == "pending" and runtime.status == "ready"
    asyncio.run(check())


def test_database_waiting_and_next_day_are_refreshed_without_memory_gate():
    async def check():
        runtime = collector()
        runtime._execute.side_effect = [progress(status="limited", batches=200, total_samples=400000),
            progress(status="waiting", batches=0),
            progress(day=date(2026, 10, 17), batches=1, samples=12)]
        await runtime._maybe_cleanup(NOW)
        assert runtime.cleanup_status == "limited" and runtime.cleanup_batches == 200
        await runtime._maybe_cleanup(NOW + timedelta(days=1, hours=-1))
        assert runtime.cleanup_status == "waiting" and runtime.cleanup_batches == 0
        await runtime._maybe_cleanup(NOW + timedelta(days=1))
        assert runtime.cleanup_day == date(2026, 10, 17)
        assert runtime.cleanup_status == "completed" and runtime.cleanup_deleted_samples == 12
        assert runtime._execute.await_count == 3
    asyncio.run(check())


def test_restart_uses_committed_totals_instead_of_resetting_daily_limit():
    async def check():
        for _ in range(2):
            runtime = collector()
            runtime._execute.return_value = progress(status="limited", batches=200, total_samples=400000)
            await runtime._maybe_cleanup(NOW)
            assert runtime.cleanup_status == "limited" and runtime.cleanup_batches == 200
            assert runtime.cleanup_deleted_samples == 400000
    asyncio.run(check())


def test_tick_saves_samples_before_one_cleanup_batch(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.query_runtime.directory_presence.return_value = observation()
        operations = []
        async def execute(function, operation, *args, **kwargs):
            operations.append(operation)
            if operation == "append":
                return {"sampled_at": args[0]["sampled_at"]}
            assert not runtime.pending
            return progress(status="pending", samples=2000)
        runtime._execute.side_effect = execute
        await runtime._tick()
        assert operations == ["append", "cleanup"]
        assert runtime.cleanup_batches == 1 and runtime.cleanup_status == "pending"
    asyncio.run(check())


@pytest.mark.parametrize("upstream", ["failed", "capacity_exceeded"])
def test_upstream_failure_or_oversized_roster_does_not_block_cleanup(monkeypatch, upstream):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        if upstream == "failed":
            runtime.query_runtime.directory_presence.side_effect = OSError("upstream unavailable")
        else:
            runtime.query_runtime.directory_presence.return_value = observation(count=3)
        await runtime._tick()
        assert runtime.status == ("upstream_unavailable" if upstream == "failed" else upstream)
        assert runtime.cleanup_status == "completed" and not runtime.pending
        assert runtime._execute.await_count == 1
    asyncio.run(check())


@pytest.mark.parametrize("error,status", [(PresenceBusy(), "busy"), (OSError("DB unavailable"), "failed")])
def test_failed_cleanup_keeps_collection_healthy_and_retries(error, status):
    async def check():
        runtime = collector()
        runtime._execute.side_effect = error
        await runtime._maybe_cleanup(NOW)
        assert runtime.status == "ready" and runtime.cleanup_status == status
        assert runtime.cleanup_batches == 0
        runtime._execute.side_effect = None
        await runtime._maybe_cleanup(NOW + timedelta(minutes=1))
        assert runtime.cleanup_status == "completed" and runtime.cleanup_batches == 1
    asyncio.run(check())


def test_pending_samples_and_recent_backlog_take_priority(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.pending.append(observation())
        from quality.presence import runtime as runtime_module
        await runtime._maybe_cleanup(runtime_module.datetime.now(timezone.utc))
        assert runtime.cleanup_status == "deferred" and len(runtime.pending) == 1
        runtime._execute.assert_not_called()
        runtime.pending.append(observation(1))
        async def flush():
            runtime.pending.clear()
        runtime._flush = AsyncMock(side_effect=flush)
        await runtime._tick()
        assert runtime.cleanup_status == "deferred" and not runtime.pending
        runtime._execute.assert_not_called()
        await runtime._tick()
        assert runtime.cleanup_status == "completed"
    asyncio.run(check())


def test_failed_sample_flush_never_runs_cleanup(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.query_runtime.directory_presence.return_value = observation()
        runtime._execute.side_effect = OSError("database unavailable")
        await runtime._tick()
        assert runtime.status == "database_unavailable" and len(runtime.pending) == 1
        assert runtime._execute.await_count == 1
        assert runtime._execute.call_args.args[1] == "append"
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("deleted_samples", 2001), ("deleted_periods", True),
    ("batches", 201), ("batches", -1), ("total_deleted_samples", 2001),
    ("total_deleted_periods", -1), ("cleanup_status", "invented"), ("cleanup_day", "2026-10-16")])
def test_invalid_cleanup_results_do_not_replace_saved_progress(field, value):
    async def check():
        runtime = collector()
        runtime._execute.return_value = {**progress(), field: value}
        await runtime._maybe_cleanup(NOW)
        assert runtime.cleanup_status == "failed" and runtime.status == "ready"
        assert runtime.cleanup_batches == 0 and runtime.cleanup_last_success_at is None
    asyncio.run(check())


def test_naive_now_is_rejected():
    async def check():
        runtime = collector()
        with pytest.raises(ValueError, match="timezone-aware"):
            await runtime._maybe_cleanup(NOW.replace(tzinfo=None))
    asyncio.run(check())


@pytest.mark.parametrize("enabled,status", [(True, "expired"), (False, "disabled")])
def test_expired_history_does_not_open_database(monkeypatch, enabled, status):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.enabled = enabled
        for method in (runtime.daily, lambda day: runtime.timeline(day, 5)):
            result = await method(date(2026, 8, 31))
            assert result["status"] == status
            assert result["oldest_available_day"] == "2026-09-01" and result["cleanup_hour"] == 4
        runtime._execute.assert_not_called()
    asyncio.run(check())


def test_oldest_partial_day_remains_queryable(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime._execute.return_value = {"status": "ready", "agents": []}
        result = await runtime.daily(date(2026, 9, 1))
        assert result["status"] == "ready" and result["oldest_available_day"] == "2026-09-01"
        assert result["retention_cutoff_at"] == "2026-08-31T20:00:00+00:00"
        assert runtime._execute.call_args.args[1:5] == ("daily", date(2026, 9, 1), "local", 7)
    asyncio.run(check())


def test_shutdown_waits_for_cleanup_without_allocating_another_worker():
    async def check():
        runtime = collector()
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime._execute = PresenceRuntime._execute.__get__(runtime)
        entered, release = threading.Event(), threading.Event()
        def cleanup(source, account):
            entered.set()
            assert release.wait(3)
            return progress(samples=1)
        runtime._repository_call = Mock(side_effect=lambda method, *args: cleanup(*args))
        runtime.repository = Mock()
        repository = runtime.repository
        cleanup_task = asyncio.create_task(runtime._maybe_cleanup(NOW))
        assert await asyncio.to_thread(entered.wait, 2)
        runtime.task = cleanup_task
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        repository.close.assert_called_once()
        assert runtime.cleanup_status == "stopped" and runtime.executor is None
    asyncio.run(check())
