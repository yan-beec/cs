"""Live directory scoping, refresh capacity and archive fallback behavior."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock, patch
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

import pytest

from ics_agent.app.chatwoot.client import ChatwootAPIError, ChatwootClient
from ics_agent.app.core.config import AppSettings
from quality.query.directory import ChatwootDirectory
from quality.query.runtime import QueryRuntime


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
