"""Bounded daily cleanup shares the collector resources and failure boundary."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from quality.presence.retention import CLEANUP_BATCH_SIZE, CLEANUP_MAX_BATCHES
from quality.presence.runtime import PresenceBusy, PresenceRuntime


NOW = datetime(2026, 10, 15, 20, tzinfo=timezone.utc)  # Beijing 2026-10-16 04:00.


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
    runtime._execute = AsyncMock(return_value={"deleted_samples": 0, "deleted_periods": 0})
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


def test_before_four_beijing_time_never_submits_cleanup():
    async def check():
        runtime = collector()
        await runtime._maybe_cleanup(NOW - timedelta(microseconds=1))
        runtime._execute.assert_not_called()
        runtime.query_runtime.get_directory.assert_not_called()
        assert runtime.state()["cleanup"] == {
            "status": "waiting", "day": "2026-10-16", "batches": 0,
            "deleted_samples": 0, "deleted_periods": 0, "last_success_at": None,
        }
    asyncio.run(check())


@pytest.mark.parametrize("at", [NOW, NOW.astimezone(timezone(timedelta(hours=8))),
                                 NOW + timedelta(hours=19, minutes=59)])
def test_four_or_late_startup_runs_one_batch_in_beijing_day(at):
    async def check():
        runtime = collector()
        await runtime._maybe_cleanup(at)
        args = runtime._execute.call_args.args
        assert args == (runtime._repository_call, "cleanup", "local", 7)
        assert runtime.cleanup_day == date(2026, 10, 16)
        assert runtime.cleanup_status == "completed" and runtime.cleanup_batches == 1
        assert runtime.cleanup_last_success_at == at.astimezone(timezone.utc).isoformat()
        assert runtime.status == "ready"
    asyncio.run(check())


def test_tick_flushes_samples_first_and_submits_at_most_one_cleanup_batch(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.query_runtime.directory_presence.side_effect = [observation(0), observation(1)]
        order = []
        async def execute(function, operation, *args, **kwargs):
            order.append(operation)
            if operation == "append":
                return {"sampled_at": args[0]["sampled_at"]}
            assert not runtime.pending
            return {"deleted_samples": CLEANUP_BATCH_SIZE, "deleted_periods": CLEANUP_BATCH_SIZE}
        runtime._execute.side_effect = execute
        await runtime._tick()
        assert order == ["append", "cleanup"]
        assert runtime.cleanup_batches == 1 and runtime.cleanup_status == "pending"
        await runtime._tick()
        assert order == ["append", "cleanup", "append", "cleanup"]
        assert runtime.cleanup_batches == 2 and runtime.status == "ready"
        assert runtime.cleanup_deleted_samples == runtime.cleanup_deleted_periods == 4000
    asyncio.run(check())


@pytest.mark.parametrize("failure", [False, True])
def test_cleanup_does_not_require_successful_chatwoot_fetch(monkeypatch, failure):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        if failure:
            runtime.query_runtime.directory_presence.side_effect = OSError("network unavailable")
        await runtime._tick()
        assert runtime.status == "upstream_unavailable"
        assert runtime.cleanup_status == "completed"
        assert runtime._execute.call_args.args[1:] == ("cleanup", "local", 7)
    asyncio.run(check())


def test_unconfigured_directory_prevents_cleanup_without_affecting_capture_status():
    async def check():
        runtime = collector()
        runtime.query_runtime.get_directory.return_value.configured = False
        await runtime._maybe_cleanup(NOW)
        assert runtime.cleanup_status == "unconfigured" and runtime.status == "ready"
        runtime._execute.assert_not_called()
        runtime.query_runtime.get_directory.return_value.configured = True
        await runtime._maybe_cleanup(NOW + timedelta(minutes=1))
        assert runtime.cleanup_status == "completed"
    asyncio.run(check())


@pytest.mark.parametrize("error,status", [(PresenceBusy(), "busy"), (OSError("db unavailable"), "failed")])
def test_cleanup_busy_or_failure_leaves_capture_healthy_and_retries_next_tick(error, status):
    async def check():
        runtime = collector()
        runtime._execute.side_effect = error
        await runtime._maybe_cleanup(NOW)
        assert runtime.status == "ready" and runtime.cleanup_status == status
        assert runtime.cleanup_batches == 0 and runtime.cleanup_last_success_at is None
        runtime._execute.side_effect = None
        await runtime._maybe_cleanup(NOW + timedelta(minutes=1))
        assert runtime.status == "ready" and runtime.cleanup_status == "completed"
        assert runtime._execute.await_count == 2 and runtime.cleanup_batches == 1
    asyncio.run(check())


@pytest.mark.parametrize("counts", [(2000, 0), (0, 2000), (2000, 2000)])
def test_any_full_table_batch_requires_a_later_tick(counts):
    async def check():
        runtime = collector()
        runtime._execute.return_value = dict(zip(("deleted_samples", "deleted_periods"), counts))
        await runtime._maybe_cleanup(NOW)
        assert runtime._execute.await_count == 1
        assert runtime.cleanup_status == "pending" and runtime.cleanup_batches == 1
    asyncio.run(check())


def test_incomplete_batch_finishes_today_and_new_day_resets_only_daily_counters():
    async def check():
        runtime = collector()
        runtime._execute.return_value = {"deleted_samples": 1999, "deleted_periods": 4}
        await runtime._maybe_cleanup(NOW)
        success = runtime.cleanup_last_success_at
        await runtime._maybe_cleanup(NOW + timedelta(hours=10))
        assert runtime._execute.await_count == 1
        assert runtime.cleanup_status == "completed"
        assert runtime.cleanup_deleted_samples == 1999 and runtime.cleanup_deleted_periods == 4
        await runtime._maybe_cleanup(NOW + timedelta(days=1, hours=-1))
        assert runtime.cleanup_status == "waiting" and runtime.cleanup_batches == 0
        assert runtime.cleanup_deleted_samples == runtime.cleanup_deleted_periods == 0
        assert runtime.cleanup_last_success_at == success
        assert runtime._execute.await_count == 1
        await runtime._maybe_cleanup(NOW + timedelta(days=1))
        assert runtime._execute.await_count == 2 and runtime.cleanup_status == "completed"
    asyncio.run(check())


def test_daily_batch_limit_stops_work_until_next_day():
    async def check():
        runtime = collector()
        runtime._execute.return_value = {"deleted_samples": 2000, "deleted_periods": 2000}
        for minute in range(CLEANUP_MAX_BATCHES + 2):
            await runtime._maybe_cleanup(NOW + timedelta(minutes=minute))
        assert runtime.cleanup_status == "limited"
        assert runtime._execute.await_count == runtime.cleanup_batches == CLEANUP_MAX_BATCHES
        assert runtime.cleanup_deleted_samples == runtime.cleanup_deleted_periods == CLEANUP_BATCH_SIZE * CLEANUP_MAX_BATCHES
        await runtime._maybe_cleanup(NOW + timedelta(days=1))
        assert runtime.cleanup_status == "pending" and runtime.cleanup_batches == 1
        assert runtime.cleanup_deleted_samples == runtime.cleanup_deleted_periods == 2000
        assert runtime._execute.await_count == CLEANUP_MAX_BATCHES + 1
    asyncio.run(check())


def test_final_allowed_batch_that_exhausts_history_is_completed_rather_than_limited():
    async def check():
        runtime = collector()
        runtime._execute.return_value = {"deleted_samples": 2000, "deleted_periods": 0}
        for minute in range(CLEANUP_MAX_BATCHES - 1):
            await runtime._maybe_cleanup(NOW + timedelta(minutes=minute))
        runtime._execute.return_value = {"deleted_samples": 0, "deleted_periods": 0}
        await runtime._maybe_cleanup(NOW + timedelta(minutes=CLEANUP_MAX_BATCHES - 1))
        assert runtime.cleanup_status == "completed"
        assert runtime.cleanup_batches == CLEANUP_MAX_BATCHES
    asyncio.run(check())


def test_unsaved_samples_defer_cleanup_without_discarding_the_batch():
    async def check():
        runtime = collector()
        runtime.pending.append(observation())
        await runtime._maybe_cleanup(NOW)
        assert runtime.cleanup_status == "deferred" and len(runtime.pending) == 1
        runtime._execute.assert_not_called()
        runtime.pending.clear()
        await runtime._maybe_cleanup(NOW + timedelta(minutes=1))
        assert runtime.cleanup_status == "completed"
    asyncio.run(check())


def test_draining_retry_backlog_defers_this_tick_then_recovers(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.pending.extend([observation(), observation(1)])
        async def flush():
            runtime.pending.clear()
        runtime._flush = AsyncMock(side_effect=flush)
        await runtime._tick()
        assert runtime.cleanup_status == "deferred" and not runtime.pending
        runtime._execute.assert_not_called()
        await runtime._tick()
        assert runtime.cleanup_status == "completed"
        assert runtime._execute.await_count == 1
    asyncio.run(check())


def test_capacity_pressure_defers_cleanup_even_when_no_batch_was_accepted(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.query_runtime.directory_presence.return_value = observation(count=3)
        await runtime._tick()
        assert runtime.status == "capacity_exceeded" and runtime.cleanup_status == "deferred"
        assert not runtime.pending
        runtime._execute.assert_not_called()
    asyncio.run(check())


def test_slow_collector_tick_defers_cleanup(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        clock = iter((100, 131))
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr("quality.presence.runtime.asyncio.get_running_loop",
                                lambda: SimpleNamespace(time=lambda: next(clock)))
            await runtime._tick()
        assert runtime.cleanup_status == "deferred"
        runtime._execute.assert_not_called()
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
        assert runtime.cleanup_batches == 0
    asyncio.run(check())


@pytest.mark.parametrize("counts", [(-1, 0), (2001, 0), (0, 2001), (True, 0), (0.0, 0)])
def test_invalid_cleanup_result_is_isolated_without_accepting_progress(counts):
    async def check():
        runtime = collector()
        runtime._execute.return_value = dict(zip(("deleted_samples", "deleted_periods"), counts))
        await runtime._maybe_cleanup(NOW)
        assert runtime.cleanup_status == "failed" and runtime.status == "ready"
        assert runtime.cleanup_batches == 0 and runtime.cleanup_last_success_at is None
        assert runtime.cleanup_deleted_samples == runtime.cleanup_deleted_periods == 0
    asyncio.run(check())


def test_cleanup_rejects_naive_now_instead_of_guessing_schedule_timezone():
    async def check():
        runtime = collector()
        with pytest.raises(ValueError, match="timezone-aware"):
            await runtime._maybe_cleanup(NOW.replace(tzinfo=None))
        runtime._execute.assert_not_called()
    asyncio.run(check())


def test_cleanup_diagnostics_are_copied_and_do_not_allocate_resources():
    runtime = PresenceRuntime()
    original = runtime.state()
    original["cleanup"]["batches"] = 99
    assert runtime.state()["cleanup"]["batches"] == 0
    assert runtime.executor is runtime.repository is runtime.slot is None


@pytest.mark.parametrize("enabled,status", [(True, "expired"), (False, "disabled")])
def test_history_older_than_45_natural_days_is_explicit_without_database(monkeypatch, enabled, status):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime.enabled = enabled
        for method in (runtime.daily, lambda day: runtime.timeline(day, 5)):
            result = await method(date(2026, 9, 1))
            assert result["status"] == status
            assert result["retention_days"] == 45
            assert result["oldest_available_day"] == "2026-09-02"
            assert result["cleanup_hour"] == 4
        runtime._execute.assert_not_called()
        runtime.query_runtime.get_directory.assert_not_called()
    asyncio.run(check())


def test_oldest_retained_day_is_queryable_and_busy_result_keeps_retention_metadata(monkeypatch):
    freeze_now(monkeypatch)
    async def check():
        runtime = collector()
        runtime._execute.return_value = {"status": "ready", "agents": []}
        result = await runtime.daily(date(2026, 9, 2))
        assert result["status"] == "ready" and result["retention_days"] == 45
        args = runtime._execute.call_args.args
        assert args[1:5] == ("daily", date(2026, 9, 2), "local", 7)
        assert args[5] == NOW
        runtime._execute.side_effect = PresenceBusy()
        result = await runtime.timeline(date(2026, 9, 2), 5)
        assert result["status"] == "unavailable"
        assert result["oldest_available_day"] == "2026-09-02" and result["cleanup_hour"] == 4
    asyncio.run(check())


def test_retention_window_changes_at_beijing_midnight_not_utc_midnight(monkeypatch):
    freeze_now(monkeypatch, NOW - timedelta(hours=4, microseconds=1))
    async def check():
        runtime = collector()
        runtime._execute.return_value = {"status": "ready", "agents": []}
        result = await runtime.daily(date(2026, 9, 1))
        assert result["status"] == "ready" and result["oldest_available_day"] == "2026-09-01"
    asyncio.run(check())


def test_shutdown_waits_for_active_cleanup_on_existing_worker_before_closing_pool():
    async def check():
        runtime = collector()
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime._execute = PresenceRuntime._execute.__get__(runtime)
        entered, release = threading.Event(), threading.Event()
        def cleanup(source, account):
            entered.set()
            assert release.wait(3)
            return {"deleted_samples": 1, "deleted_periods": 0}
        runtime.repository = Mock(cleanup=Mock(side_effect=cleanup))
        repository = runtime.repository
        runtime.task = asyncio.create_task(runtime._maybe_cleanup(NOW))
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            closing = asyncio.create_task(runtime.close())
            await asyncio.sleep(0.01)
            assert not closing.done()
            assert runtime.slot.locked()
            repository.close.assert_not_called()
        finally:
            release.set()
            if closing is None:
                await runtime.close()
            else:
                await closing
        assert runtime.task is runtime.executor is runtime.repository is runtime.slot is None
        assert runtime.status == runtime.cleanup_status == "stopped"
        repository.cleanup.assert_called_once_with("local", 7)
        repository.close.assert_called_once()
    asyncio.run(check())
