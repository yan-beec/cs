"""Failure boundaries for the minute collector, independent of live services."""

import asyncio
from copy import deepcopy
from datetime import date
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from quality.presence.config import PresenceSettings
from quality.presence.runtime import PresenceBusy, PresenceRuntime
from quality.query.runtime import QueryRuntime


def observation(minute=0, *, status="ready", count=2):
    return {"status": status, "source_key": "local", "account_id": 1,
            "sampled_at": f"2026-10-16T00:{minute:02d}:05+00:00",
            "agents": [{"source_key": "local", "account_id": 1, "agent_id": str(i),
                        "agent_name": f"Seat {i}", "agent_name_source": "chatwoot",
                        "availability_status": "online" if i == 1 else "busy"}
                       for i in range(1, count + 1)]}


def collector():
    runtime = PresenceRuntime()
    runtime.enabled = True
    runtime.settings = PresenceSettings(_env_file=None, presence_password="test-only", source_key="local")
    runtime.query_runtime = SimpleNamespace(directory_presence=AsyncMock(return_value=observation()))
    runtime._execute = AsyncMock(side_effect=lambda method, operation, batch, **kwargs: {"sampled_at": batch["sampled_at"]})
    return runtime


def test_disabled_collector_allocates_no_threads_or_database(monkeypatch):
    monkeypatch.setattr("quality.presence.runtime.PresenceSettings", lambda: PresenceSettings(_env_file=None, presence_enabled=False))
    async def check():
        runtime = PresenceRuntime()
        await runtime.start(Mock())
        assert runtime.task is runtime.executor is runtime.repository is None
        assert (await runtime.daily(date(2026, 10, 16)))["status"] == "disabled"
        await runtime.close()
    asyncio.run(check())


def test_real_observation_time_deduplicates_without_altering_input():
    runtime = collector()
    original = observation()
    before = deepcopy(original)
    assert runtime._enqueue(original)
    assert not runtime._enqueue({**original, "sampled_at": "2026-10-16T08:00:58+08:00"})
    assert len(runtime.pending) == 1 and original == before
    assert runtime._enqueue(observation(1))
    assert [batch["sampled_at"] for batch in runtime.pending] == [before["sampled_at"], observation(1)["sampled_at"]]


def test_database_failure_buffers_then_recovers_original_observations_in_order():
    async def check():
        runtime = collector()
        runtime._execute.side_effect = OSError("database unavailable")
        await runtime._tick()
        assert runtime.status == "database_unavailable" and len(runtime.pending) == 1
        assert runtime.last_saved_at is None
        runtime.query_runtime.directory_presence.return_value = observation(1)
        saved = []
        async def append(method, operation, batch, **kwargs):
            saved.append(batch)
            return {"sampled_at": batch["sampled_at"]}
        runtime._execute.side_effect = append
        await runtime._tick()
        assert [batch["sampled_at"] for batch in saved] == [observation()["sampled_at"], observation(1)["sampled_at"]]
        assert not runtime.pending and runtime.status == "ready"
        assert runtime.last_saved_at == observation(1)["sampled_at"]
    asyncio.run(check())


@pytest.mark.parametrize("status", ["stale", "unavailable", "unconfigured"])
def test_failed_or_stale_observation_never_becomes_offline(status):
    async def check():
        runtime = collector()
        runtime.query_runtime.directory_presence.return_value = observation(status=status)
        await runtime._tick()
        assert not runtime.pending and runtime.last_saved_at is None
        runtime._execute.assert_not_called()
        assert runtime.status == ("unconfigured" if status == "unconfigured" else "upstream_unavailable")
    asyncio.run(check())


def test_retry_buffer_is_bounded_and_excess_is_explicit():
    runtime = collector()
    for minute in range(9):
        runtime._enqueue(observation(minute))
    assert len(runtime.pending) == 5 and runtime.dropped_batches == 4
    assert runtime.pending[0]["sampled_at"] == observation(4)["sampled_at"]


def test_agent_limit_rejects_whole_batch_without_partial_offline_roster():
    runtime = collector()
    runtime.settings.presence_max_agents = 1
    assert not runtime._enqueue(observation(count=2))
    assert not runtime.pending and runtime.last_observed_minute is None
    assert runtime.status == "capacity_exceeded"
    assert runtime._enqueue(observation(count=1))


def test_empty_successful_roster_is_still_persisted():
    async def check():
        runtime = collector()
        runtime.query_runtime.directory_presence.return_value = observation(count=0)
        await runtime._tick()
        assert runtime.status == "ready"
        assert runtime._execute.call_args.args[2]["agents"] == []
    asyncio.run(check())


def test_cancelled_db_work_retains_single_slot_until_thread_finishes():
    async def check():
        from concurrent.futures import ThreadPoolExecutor
        runtime = PresenceRuntime()
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime.slot = asyncio.Semaphore(1)
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            assert release.wait(3)
        task = asyncio.create_task(runtime._execute(work))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(PresenceBusy):
                await runtime._execute(lambda: None, wait_seconds=0.02)
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()
    asyncio.run(check())


def test_presence_works_when_archive_query_database_is_unavailable_and_shares_directory():
    async def check():
        runtime = QueryRuntime()
        directory = Mock(presence=AsyncMock(return_value=observation()), close=AsyncMock())
        runtime.directory = directory
        result = await runtime.directory_presence(force_refresh=True)
        assert result == observation() and runtime.get_directory() is directory
        assert not runtime.ready and runtime.repository is None
        directory.presence.assert_awaited_once_with(force_refresh=True)
        await runtime.close()
    asyncio.run(check())


def test_daily_capacity_failure_returns_explicit_unknown_instead_of_empty_ready():
    async def check():
        runtime = collector()
        runtime.executor = Mock()
        runtime.query_runtime.get_directory = Mock(return_value=SimpleNamespace(configured=True, account_id=1))
        runtime._execute.side_effect = PresenceBusy()
        result = await runtime.daily(date(2026, 10, 16))
        assert result["status"] == "unavailable" and result["agents"] == []
        assert result["day"] == "2026-10-16" and result["timezone"] == "Asia/Shanghai"
    asyncio.run(check())


def test_overview_names_follow_chatwoot_scope_without_changing_responsibility_totals():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.repository = Mock()
        runtime._execute = AsyncMock(return_value={"participating_agents": [
            {"source_key": "local", "account_id": 1, "agent_id": "1", "agent_name": "分配坐席1", "responsible_rounds": 4},
            {"source_key": "other", "account_id": 1, "agent_id": "1", "agent_name": "Old", "responsible_rounds": 1}]})
        runtime._directory_options = AsyncMock(return_value={"agents": [
            {"source_key": "local", "account_id": 1, "agent_id": "1", "agent_name": "Real", "agent_name_source": "chatwoot"}]})
        result = await runtime.call("overview", Mock())
        assert result["participating_agents"][0]["agent_name"] == "Real"
        assert result["participating_agents"][0]["responsible_rounds"] == 4
        assert result["participating_agents"][1]["agent_name"] == "Old"
    asyncio.run(check())


def test_presence_routes_return_collection_metadata_and_validate_daily_date(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ics_agent.app.core.auth import require_admin
    from quality.query import routes
    runtime = Mock(presence=AsyncMock(return_value={**observation(), "collector": {"enabled": True, "status": "ready"}}),
                   presence_daily=AsyncMock(return_value={"status": "ready", "agents": [], "timezone": "Asia/Shanghai"}),
                   presence_timeline=AsyncMock(return_value={"status": "ready", "segments": [], "timezone": "Asia/Shanghai"}))
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    with TestClient(app) as browser:
        response = browser.get("/admin/api/quality/presence")
        assert response.status_code == 200 and response.json()["collector"]["enabled"]
        response = browser.get("/admin/api/quality/presence/daily?day=2026-10-16")
        assert response.status_code == 200 and response.json()["timezone"] == "Asia/Shanghai"
        runtime.presence_daily.assert_awaited_once_with(date(2026, 10, 16))
        assert browser.get("/admin/api/quality/presence/daily?day=2026-02-31").status_code == 422
        assert runtime.presence_daily.await_count == 1
        response = browser.get("/admin/api/quality/presence/timeline?day=2026-10-16&agent_id=1")
        assert response.status_code == 200 and response.json()["timezone"] == "Asia/Shanghai"
        runtime.presence_timeline.assert_awaited_once_with(date(2026, 10, 16), 1)
        for invalid in ("", "?agent_id=0", "?agent_id=-1", "?agent_id=bad", "?agent_id=9223372036854775808", "?agent_id=1&day=2026-02-31"):
            assert browser.get("/admin/api/quality/presence/timeline"+invalid).status_code == 422
        assert runtime.presence_timeline.await_count == 1
        app.dependency_overrides.clear()
        assert browser.get("/admin/api/quality/presence/timeline?agent_id=1").status_code == 401


def test_timeline_uses_existing_presence_worker_and_preserves_agent_day_scope():
    async def check():
        runtime = collector()
        runtime.executor = Mock()
        runtime.query_runtime.get_directory = Mock(return_value=SimpleNamespace(configured=True, account_id=1))
        runtime._execute.side_effect = None
        runtime._execute.return_value = {"status": "ready", "segments": [], "agent": {"agent_id": "7"}}
        result = await runtime.timeline(date(2026, 10, 16), 7)
        assert result["status"] == "ready" and result["day"] == "2026-10-16"
        args = runtime._execute.call_args.args
        assert args[1:6] == ("timeline", date(2026, 10, 16), "local", 1, 7)
        runtime._execute.side_effect = PresenceBusy()
        assert (await runtime.timeline(date(2026, 10, 16), 7))["status"] == "unavailable"
    asyncio.run(check())


def test_management_uses_existing_worker_rejects_changed_scope_and_never_calls_chatwoot():
    async def check():
        runtime = collector()
        runtime.executor = Mock()
        runtime.query_runtime.get_directory = Mock(return_value=SimpleNamespace(configured=True, account_id=1))
        runtime._execute.side_effect = None
        runtime._execute.return_value = {"status": "ready", "agents": [], "total": 0}
        assert (await runtime.agents(2, 50, True))["status"] == "ready"
        assert runtime._execute.call_args.args[1:] == ("list_agents", "local", 1)
        assert runtime._execute.call_args.kwargs == {"page": 2, "page_size": 50, "include_hidden": True}
        runtime._execute.reset_mock()
        assert (await runtime.remove_agent("other", 1, 7))["status"] == "scope_changed"
        assert (await runtime.remove_agent("local", 2, 7))["status"] == "scope_changed"
        runtime._execute.assert_not_called()
        await runtime.remove_agent("local", 1, 7)
        assert runtime._execute.call_args.args[1:] == ("remove_agent", "local", 1, 7)
        assert runtime._execute.call_args.kwargs["now"].tzinfo is not None
        runtime.query_runtime.directory_presence.assert_not_awaited()
        await runtime.daily(date(2026, 10, 16), include_hidden=True)
        assert runtime._execute.call_args.kwargs == {"include_hidden": True}
        runtime._execute.side_effect = PresenceBusy()
        assert (await runtime.agents())["status"] == "unavailable"
        assert (await runtime.remove_agent("local", 1, 7))["status"] == "unavailable"
    asyncio.run(check())


def test_management_routes_validate_scope_authentication_and_map_write_failures(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ics_agent.app.core.auth import require_admin
    from quality.query import routes
    runtime = Mock(presence_agents=AsyncMock(return_value={"status": "ready", "agents": [], "total": 0}),
                   presence_remove_agent=AsyncMock(return_value={"status": "ready", "hidden_at": "2026-10-16T00:00:00Z"}))
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as browser:
        path = "/admin/api/quality/presence/agents"
        assert browser.get(path).status_code == 401
        assert browser.delete(path+"/7?source_key=local&account_id=1").status_code == 401
        app.dependency_overrides[require_admin] = lambda: object()
        assert browser.get(path+"?page=2&page_size=10&include_hidden=true").status_code == 200
        runtime.presence_agents.assert_awaited_once_with(2, 10, True)
        for suffix in ("/0?source_key=local&account_id=1", "/7", "/7?source_key=local&account_id=0", "/9223372036854775808?source_key=local&account_id=1"):
            assert browser.delete(path+suffix).status_code == 422
        runtime.presence_remove_agent.assert_not_awaited()
        scoped = path+"/7?source_key=local&account_id=1"
        assert browser.delete(scoped).status_code == 200
        runtime.presence_remove_agent.assert_awaited_once_with("local", 1, 7)
        for status, code in (("not_found", 404), ("scope_changed", 409), ("unavailable", 503), ("disabled", 503)):
            runtime.presence_remove_agent.return_value = {"status": status}
            assert browser.delete(scoped).status_code == code
