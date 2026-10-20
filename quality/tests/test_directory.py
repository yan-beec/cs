"""Live directory scoping, refresh capacity and archive fallback behavior."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock, patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from ics_agent.app.chatwoot.client import ChatwootAPIError, ChatwootClient
from ics_agent.app.core.config import AppSettings
from quality.query.directory import ChatwootDirectory
from quality.query.runtime import QueryRuntime
from quality.query.config import QuerySettings
from quality.query.repository import ArchiveFilters


def settings(**changes):
    return AppSettings(_env_file=None, chatwoot_base_url="http://chatwoot.test",
                       chatwoot_account_id="1", chatwoot_api_token="user-token", **changes)


def test_directory_adds_unobserved_seats_and_inboxes_without_crossing_sources():
    async def check():
        client = Mock()
        client.list_agents.return_value = [{"id": 7, "name": "Current name", "email": "must-not-return"},
                                           {"id": 8, "name": "No archived reply"}]
        client.list_inboxes.return_value = [{"id": 3, "name": "Chatwoot inbox"}, {"id": 99, "name": "Other inbox"}]
        directory = ChatwootDirectory("local", settings(), client=client)
        try:
            result = await directory.enrich({
                "sources": [{"source_key": "other", "account_id": 1}],
                "agents": [{"source_key": "other", "account_id": 1, "agent_id": "7", "agent_name": "Other instance"},
                           {"source_key": "local", "account_id": 1, "agent_id": "7", "agent_name": "Old name"},
                           {"source_key": "local", "account_id": 1, "agent_id": "9", "agent_name": "Deleted seat"}],
                "channels": [],
            })
            assert len(result["agents"]) == 4
            assert {row["agent_name"] for row in result["agents"]} == {
                "Current name", "No archived reply", "Other instance", "Deleted seat",
            }
            assert result["inboxes"][0]["channel_key"] == "xingliao"
            assert result["inboxes"][1]["channel_key"] is None
            assert result["directory"] == {"status": "ready"}
            assert result["directory_warning"] is None
            assert next(row for row in result["agents"] if row["source_key"] == "local" and row["agent_id"] == "8")["agent_name_source"] == "chatwoot"
            assert "must-not-return" not in json.dumps(result)
            assert {row["source_key"] for row in result["sources"]} == {"local", "other"}
            client.list_agents.assert_called_once_with(account_id="1", timeout_seconds=2,
                                                       max_response_bytes=2097152, strict_response=True)
        finally:
            await directory.close()
    asyncio.run(check())


def test_concurrent_requests_share_one_refresh_and_cached_values():
    async def check():
        gate = threading.Event()
        client = Mock()
        client.list_agents.side_effect = lambda **kw: gate.wait(2) and [{"id": 1, "name": "Seat"}]
        client.list_inboxes.return_value = []
        directory = ChatwootDirectory("local", settings(), client=client)
        requests = [asyncio.create_task(directory.enrich({})) for _ in range(8)]
        try:
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*requests)
            assert all(row["agents"][0]["agent_id"] == "1" for row in results)
            assert (await directory.enrich({}))["directory"]["status"] == "ready"
            assert client.list_agents.call_count == 1
            assert client.list_inboxes.call_count == 1
        finally:
            gate.set()
            await directory.close()
    asyncio.run(check())


def test_partial_failure_retains_last_good_values_and_backs_off():
    async def check():
        now = [0]
        client = Mock()
        client.list_agents.return_value = [{"id": 1, "name": "Seat"}]
        client.list_inboxes.return_value = [{"id": 3, "name": "Inbox"}]
        directory = ChatwootDirectory("local", settings(), client=client, clock=lambda: now[0])
        try:
            await directory.enrich({})
            now[0] = 301
            client.list_agents.side_effect = ChatwootAPIError("secret upstream body", status_code=403)
            client.list_inboxes.return_value = [{"id": 3, "name": "New inbox name"}]
            result = await directory.enrich({})
            assert result["directory"]["status"] == "stale"
            assert result["agents"][0]["agent_name"] == "Seat"
            assert result["inboxes"][0]["name"] == "New inbox name"
            assert "secret" not in json.dumps(result)
            now[0] = 302
            await directory.enrich({})
            assert client.list_agents.call_count == 2
        finally:
            await directory.close()
    asyncio.run(check())


def test_missing_user_token_never_uses_agent_bot_token_or_calls_chatwoot():
    async def check():
        client = Mock()
        cfg = settings().model_copy(update={"chatwoot_api_token": "", "chatwoot_agent_bot_access_token": "bot-only"})
        directory = ChatwootDirectory("local", cfg, client=client)
        archived = {"agents": [{"source_key": "local", "account_id": 1, "agent_id": "7", "agent_name": "Archived"}]}
        result = await directory.enrich(archived)
        assert result["directory"]["status"] == "unconfigured"
        assert result["agents"] == archived["agents"]
        assert directory.executor is None
        client.list_agents.assert_not_called()
    asyncio.run(check())


def test_oversized_directory_falls_back_without_disabling_queries():
    async def check():
        client = Mock()
        client.list_agents.return_value = [{"id": n} for n in range(2001)]
        client.list_inboxes.side_effect = ChatwootAPIError("too large")
        directory = ChatwootDirectory("local", settings(), client=client)
        try:
            result = await directory.enrich({"channels": [{"key": "xingliao"}]})
            assert result["directory"]["status"] == "unavailable"
            assert result["agents"] == []
            assert result["channels"] == [{"key": "xingliao"}]
        finally:
            await directory.close()
    asyncio.run(check())


def test_shared_inbox_client_uses_user_token_and_response_bounds():
    client = ChatwootClient(settings())
    with patch.object(client, "_request_json", return_value={"payload": [{"id": 3}]}) as request:
        assert client.list_inboxes(account_id="1", timeout_seconds=2, max_response_bytes=1024) == [{"id": 3}]
    request.assert_called_once_with("GET", "/api/v1/accounts/1/inboxes", access_token="user-token",
                                    timeout_seconds=2, max_response_bytes=1024)


def test_shared_client_stops_reading_at_directory_byte_limit():
    client = ChatwootClient(settings())
    response = Mock(status=200)
    response.read.return_value = b"x" * 1025
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    with patch("ics_agent.app.chatwoot.client.urlopen", return_value=context):
        try:
            client.list_inboxes(account_id="1", max_response_bytes=1024)
            assert False, "Oversized response accepted"
        except ChatwootAPIError:
            pass
    response.read.assert_called_once_with(1025)


@pytest.mark.parametrize("payload", [{"raw_response": "<html>error</html>"}, {}, {"data": ["bad"]}])
def test_directory_rejects_invalid_successful_response(payload):
    client = ChatwootClient(settings())
    with patch.object(client, "_request_json", return_value=payload):
        with pytest.raises(ChatwootAPIError):
            client.list_agents(account_id="1", strict_response=True)
        with pytest.raises(ChatwootAPIError):
            client.list_inboxes(account_id="1", strict_response=True)


def test_invalid_live_response_preserves_both_cached_directories():
    async def check():
        now = [0]
        client = ChatwootClient(settings())
        directory = ChatwootDirectory("local", settings(), client=client, clock=lambda: now[0])
        try:
            with patch.object(client, "_request_json", return_value={"data": [{"id": 1, "name": "Valid"}]}):
                previous = await directory.enrich({})
            now[0] = 301
            with patch.object(client, "_request_json", return_value={"raw_response": "<html>Bad gateway</html>"}):
                result = await directory.enrich({})
            assert result["directory"]["status"] == "stale"
            assert result["agents"] == previous["agents"]
            assert result["inboxes"] == previous["inboxes"]
        finally:
            await directory.close()
    asyncio.run(check())


def test_shared_client_error_response_is_also_bounded():
    body = Mock()
    body.read.return_value = b"x" * 1000
    error = HTTPError("http://chatwoot.test", 403, "denied", {}, body)
    with patch("ics_agent.app.chatwoot.client.urlopen", side_effect=error):
        with pytest.raises(ChatwootAPIError):
            ChatwootClient(settings()).list_agents(account_id="1", max_response_bytes=1024)
    body.read.assert_called_once_with(1000)


def test_runtime_directory_failure_does_not_disable_the_archive_or_other_queries():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.executor = ThreadPoolExecutor(max_workers=2)
        runtime.slots = asyncio.Semaphore(2)
        runtime.repository = Mock()
        runtime.repository.filter_options.return_value = {"agents": [], "inboxes": []}
        runtime.repository.list_rounds.return_value = {"total": 3}
        runtime.directory = Mock(enrich=AsyncMock(side_effect=ValueError("private upstream failure")),
                                 close=AsyncMock())
        try:
            result = await runtime.call("filter_options")
            assert result["directory"]["status"] == "unavailable"
            assert "private upstream failure" not in json.dumps(result)
            assert runtime.ready is True
            assert await runtime.call("list_rounds") == {"total": 3}
        finally:
            await runtime.close()
    asyncio.run(check())


def test_rounds_use_live_names_while_export_keeps_archived_message_senders():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.executor = ThreadPoolExecutor(max_workers=2)
        runtime.slots = asyncio.Semaphore(2)
        runtime.settings = QuerySettings(_env_file=None, reader_password="test-reader", source_key="local")
        client = Mock()
        client.list_agents.return_value = [{"id": 1, "name": "beec", "available_name": "客服甲"}]
        client.list_inboxes.return_value = []
        runtime.directory = ChatwootDirectory("local", settings(), client=client)
        row = {"source_key": "local", "account_id": 1, "responsibility_basis": "ics_assignment",
            "responsible_agent_id": "1", "responsible_agent_name": "分配坐席1", "responsibility_event_id": 99}
        message = {"sender_id": "1", "sender_name": "Historical sender", "content": "Original reply"}
        runtime.repository = Mock()
        runtime.repository.list_rounds.return_value = {"items": [row], "total": 1}
        runtime.repository.round_detail.return_value = {"round": row, "messages": [message]}
        exporter = Mock()
        exporter.create_export.return_value = object()
        try:
            result = await runtime.call("list_rounds", ArchiveFilters())
            assert result["items"][0]["responsible_agent_name"] == "beec"
            detail = await runtime.call("round_detail", "round-one")
            assert detail["round"]["responsible_agent_name"] == "beec"
            assert detail["messages"] == [message] and message["sender_name"] == "Historical sender"
            assert row["responsible_agent_name"] == "分配坐席1"
            # Chat exports use archived senders and have no live directory dependency.
            runtime.directory.enrich = AsyncMock(side_effect=AssertionError("export must not refresh the directory"))
            with patch("quality.query.runtime.ExportRepository", return_value=exporter):
                await runtime.export("rounds", ArchiveFilters())
            exporter.create_export.assert_called_once()
            assert exporter.create_export.call_args.args[0] == "rounds"
            assert "agent_names" not in exporter.create_export.call_args.kwargs
            runtime.directory.enrich.assert_not_awaited()
            client.list_agents.assert_called_once()
            assert runtime.ready is True
        finally:
            await runtime.close()
    asyncio.run(check())


def test_round_name_lookup_failure_preserves_query_health_and_actual_messages():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.executor = ThreadPoolExecutor(max_workers=2)
        runtime.slots = asyncio.Semaphore(2)
        runtime.repository = Mock()
        message = {"sender_id": "1", "sender_name": "Historical sender", "content": "Original reply"}
        runtime.repository.round_detail.return_value = {"round": {"source_key": "local", "account_id": 1,
            "responsibility_basis": "ics_assignment", "responsible_agent_id": "1", "responsible_agent_name": "Known name"},
            "messages": [message]}
        runtime.directory = Mock(enrich=AsyncMock(side_effect=ValueError("private upstream failure")), close=AsyncMock())
        try:
            result = await runtime.call("round_detail", "round-one")
            assert result["round"]["responsible_agent_name"] == "Known name"
            assert result["messages"] == [message]
            assert runtime.ready is True
        finally:
            await runtime.close()
    asyncio.run(check())


def test_timed_out_or_cancelled_waiter_cannot_start_another_directory_worker():
    async def check():
        gate = threading.Event()
        client = Mock()
        def blocked(**kwargs):
            gate.wait(2)
            return [{"id": 1}]
        client.list_agents.side_effect = blocked
        client.list_inboxes.return_value = []
        directory = ChatwootDirectory("local", settings(), client=client)
        directory.WAIT_SECONDS = 0.02
        try:
            result = await directory.enrich({})
            assert result["directory"]["status"] == "unavailable"
            pending = directory.refresh
            waiter = asyncio.create_task(directory.enrich({}))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert directory.refresh is pending and not pending.cancelled()
            await directory.enrich({})
            assert client.list_agents.call_count == 1
            await directory.close()
            assert directory.executor is None
        finally:
            gate.set()
            if directory.executor is not None:
                await directory.close()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(check())


SAMPLE_START = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)


def presence_directory(client, now, *, cfg=None, source_key="local"):
    return ChatwootDirectory(source_key, cfg or settings(), client=client, clock=lambda: now[0],
                             utcnow=lambda: SAMPLE_START + timedelta(seconds=now[0]))


def test_presence_returns_only_scoped_agents_with_whitelisted_states_and_real_sample_time():
    async def check():
        now = [0]
        client = Mock()
        client.list_agents.return_value = [
            {"id": 7, "name": " Alice ", "availability_status": "online", "email": "secret-email"},
            {"id": 8, "available_name": "Bob", "availability_status": " BUSY "},
            {"id": 9, "name": "Carol", "availability_status": "offline"},
            {"id": 10, "name": "Dan", "availability": "online"},
            {"id": 11, "name": "Eve", "availability_status": "away"},
            {"id": 12, "name": "Frank"},
            {"id": 13, "name": "Grace", "availability_status": {"unexpected": "token-secret"}},
            {"id": False, "name": "Invalid"},
        ]
        client.list_inboxes.return_value = []
        directory = presence_directory(client, now)
        try:
            result = await directory.presence()
            assert result["status"] == "ready"
            assert result["source_key"] == "local" and result["account_id"] == 1
            assert result["sampled_at"] == SAMPLE_START.isoformat()
            assert {row["agent_id"]: row["availability_status"] for row in result["agents"]} == {
                "7": "online", "8": "busy", "9": "offline", "10": "online",
                "11": "unknown", "12": "unknown", "13": "unknown",
            }
            assert result["agents"][0]["agent_name"] == "Alice"
            assert all(set(row) == {"source_key", "account_id", "agent_id", "agent_name", "agent_name_source", "availability_status"}
                       and row["source_key"] == "local" and row["account_id"] == 1 for row in result["agents"])
            assert "secret" not in json.dumps(result) and "user-token" not in json.dumps(result)
            client.list_agents.assert_called_once_with(account_id="1", timeout_seconds=2,
                                                       max_response_bytes=2097152, strict_response=True)
            assert directory.executor._max_workers == 1
        finally:
            await directory.close()
    asyncio.run(check())


def test_presence_and_names_share_the_same_global_minute_cache_and_inflight_refresh():
    async def check():
        now = [0]
        gate = threading.Event()
        client = Mock()

        def agents(**kwargs):
            gate.wait(2)
            return [{"id": 1, "name": "Seat", "availability_status": "online" if now[0] < 60 else "busy"}]

        client.list_agents.side_effect = agents
        client.list_inboxes.return_value = []
        directory = presence_directory(client, now)
        archived = {"agents": [{"source_key": "other", "account_id": 1, "agent_id": "99", "agent_name": "Other history"}]}
        try:
            requests = [asyncio.create_task(directory.enrich(archived))] + [
                asyncio.create_task(directory.presence(force_refresh=True)) for _ in range(8)
            ]
            await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*requests)
            assert len(results[0]["agents"]) == 2
            assert all(len(result["agents"]) == 1 and result["agents"][0]["agent_id"] == "1" for result in results[1:])
            assert client.list_agents.call_count == client.list_inboxes.call_count == 1
            now[0] = 59
            cached = await directory.presence()
            assert cached["status"] == "ready" and cached["sampled_at"] == SAMPLE_START.isoformat()
            assert cached["agents"][0]["availability_status"] == "online"
            now[0] = 60
            current = await directory.presence()
            assert current["agents"][0]["availability_status"] == "busy"
            assert current["sampled_at"] == (SAMPLE_START+timedelta(minutes=1)).isoformat()
            await directory.enrich({})
            assert client.list_agents.call_count == client.list_inboxes.call_count == 2
        finally:
            gate.set()
            await directory.close()
    asyncio.run(check())


def test_presence_failure_preserves_last_sample_and_never_claims_offline_or_a_new_time():
    async def check():
        now = [0]
        client = Mock()
        client.list_agents.return_value = [{"id": 1, "name": "Seat", "availability_status": "online"}]
        client.list_inboxes.return_value = []
        directory = presence_directory(client, now)
        try:
            original = await directory.presence()
            now[0] = 60
            client.list_agents.side_effect = ChatwootAPIError("secret upstream body", status_code=403)
            client.list_inboxes.return_value = [{"id": 3, "name": "Refreshed inbox"}]
            stale = await directory.presence()
            assert stale == {**original, "status": "stale"}
            assert directory.cached["inboxes"][0]["name"] == "Refreshed inbox"
            now[0] = 61
            assert await directory.presence() == stale
            assert client.list_agents.call_count == 2
            assert "secret" not in json.dumps(stale)
            now[0] = 120
            client.list_agents.side_effect = None
            client.list_agents.return_value = [{"id": 1, "name": "Seat", "availability_status": "offline"}]
            recovered = await directory.presence()
            assert recovered["status"] == "ready" and recovered["agents"][0]["availability_status"] == "offline"
            assert recovered["sampled_at"] == (SAMPLE_START+timedelta(minutes=2)).isoformat()
        finally:
            await directory.close()
    asyncio.run(check())


def test_presence_is_fresh_after_agent_success_even_when_inbox_refresh_fails():
    async def check():
        now = [0]
        client = Mock()
        client.list_agents.return_value = [{"id": 1, "name": "Seat", "availability_status": "busy"}]
        client.list_inboxes.side_effect = ChatwootAPIError("secret inbox failure")
        directory = presence_directory(client, now)
        try:
            result = await directory.presence()
            assert result["status"] == "ready" and result["sampled_at"] == SAMPLE_START.isoformat()
            assert directory.status == "stale"
            now[0] = 60
            result = await directory.presence()
            assert result["status"] == "ready"
            assert result["sampled_at"] == (SAMPLE_START+timedelta(minutes=1)).isoformat()
        finally:
            await directory.close()
    asyncio.run(check())


@pytest.mark.parametrize("agents", [None, [{"id": index+1} for index in range(2001)]])
def test_presence_first_agent_failure_has_no_sample_and_no_invented_offline_seats(agents):
    async def check():
        client = Mock()
        if agents is None:
            client.list_agents.side_effect = ChatwootAPIError("secret response")
        else:
            client.list_agents.return_value = agents
        client.list_inboxes.return_value = [{"id": 3, "name": "Inbox success"}]
        directory = presence_directory(client, [0])
        try:
            result = await directory.presence(force_refresh=True)
            assert result == {"status": "unavailable", "source_key": "local", "account_id": 1,
                              "sampled_at": None, "agents": []}
        finally:
            await directory.close()
    asyncio.run(check())


@pytest.mark.parametrize("changes,source_key", [
    ({"chatwoot_api_token": "", "chatwoot_agent_bot_access_token": "bot-only"}, "local"),
    ({"chatwoot_account_id": ""}, "local"),
    ({}, ""),
])
def test_presence_unconfigured_source_or_account_allocates_no_resources(changes, source_key):
    async def check():
        client = Mock()
        cfg = settings().model_copy(update=changes)
        directory = presence_directory(client, [0], cfg=cfg, source_key=source_key)
        result = await directory.presence(force_refresh=True)
        assert result["status"] == "unconfigured" and result["sampled_at"] is None and result["agents"] == []
        assert result["source_key"] == source_key
        assert result["account_id"] == (None if changes.get("chatwoot_account_id") == "" else 1)
        assert directory.executor is None
        client.list_agents.assert_not_called()
        client.list_inboxes.assert_not_called()
        await directory.close()
    asyncio.run(check())


def test_presence_empty_success_is_a_real_sample_and_force_refresh_bypasses_cache():
    async def check():
        now = [0]
        client = Mock()
        client.list_agents.return_value = []
        client.list_inboxes.return_value = []
        directory = presence_directory(client, now)
        try:
            first = await directory.presence()
            assert first["status"] == "ready" and first["agents"] == []
            now[0] = 1
            assert (await directory.presence())["sampled_at"] == first["sampled_at"]
            sampled = await directory.presence(force_refresh=True)
            assert sampled["sampled_at"] == (SAMPLE_START+timedelta(seconds=1)).isoformat()
            assert client.list_agents.call_count == 2
        finally:
            await directory.close()
    asyncio.run(check())


def test_agent_sample_timestamp_and_age_are_independent_of_late_inbox_completion():
    async def check():
        now = [0]
        client = Mock()
        client.list_agents.return_value = [{"id": 1, "name": "Seat", "availability_status": "online"}]

        def inboxes(**kwargs):
            now[0] = 61
            return []

        client.list_inboxes.side_effect = inboxes
        directory = presence_directory(client, now)
        try:
            result = await directory.presence()
            assert result["status"] == "stale"
            assert result["sampled_at"] == SAMPLE_START.isoformat()
            assert result["agents"][0]["availability_status"] == "online"
            assert client.list_agents.call_count == 1
        finally:
            await directory.close()
    asyncio.run(check())


def test_presence_timeout_and_cancelled_waiter_preserve_one_actual_sampling_task():
    async def check():
        now = [0]
        gate = threading.Event()
        client = Mock()

        def agents(**kwargs):
            gate.wait(2)
            return [{"id": 1, "name": "Seat", "availability_status": "online"}]

        client.list_agents.side_effect = agents
        client.list_inboxes.return_value = []
        directory = presence_directory(client, now)
        directory.WAIT_SECONDS = 0.02
        pending = None
        try:
            result = await directory.presence()
            assert result["status"] == "unavailable" and result["sampled_at"] is None
            pending = directory.refresh
            waiter = asyncio.create_task(directory.presence(force_refresh=True))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert directory.refresh is pending and not pending.cancelled()
            assert (await directory.presence(force_refresh=True))["sampled_at"] is None
            assert client.list_agents.call_count == 1
            now[0] = 7
            gate.set()
            await asyncio.wait_for(asyncio.shield(pending), timeout=1)
            now[0] = 8
            result = await directory.presence(force_refresh=True)
            assert result["status"] == "ready"
            assert result["sampled_at"] == (SAMPLE_START+timedelta(seconds=7)).isoformat()
            assert client.list_agents.call_count == 1
        finally:
            gate.set()
            await directory.close()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(check())
