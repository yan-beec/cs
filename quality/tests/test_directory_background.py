"""验证目录后台刷新不阻塞已有缓存，以及关闭和旧任务的隔离。"""

import asyncio
from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

from quality.query.directory import ChatwootDirectory
from quality.tests.test_directory import settings


@pytest.mark.parametrize("initial_agents", [[], [{"id": 1, "name": "原名称"}]])
def test_initialized_directory_returns_without_waiting_and_updates_in_background(initial_agents):
    async def check():
        now = [0]
        gate = threading.Event()
        client = Mock()
        client.list_agents.return_value = initial_agents
        client.list_inboxes.return_value = []
        directory = ChatwootDirectory("local", settings(), client=client, clock=lambda: now[0])
        pending = None
        try:
            previous = await directory.enrich({})
            now[0] = 61
            def blocked(**kwargs):
                assert gate.wait(3)
                return [{"id": 1, "name": "新名称"}]
            client.list_agents.side_effect = blocked
            for _ in range(3):
                result = await asyncio.wait_for(directory.enrich({}), timeout=0.2)
                assert result["agents"] == previous["agents"]
                current = directory.refresh
                assert current is not None
                assert pending is None or current is pending
                pending = current
            gate.set()
            await pending
            assert directory.cached["agents"][0]["agent_name"] == "新名称"
            assert directory.refresh is None
            assert client.list_agents.call_count == 2
        finally:
            gate.set()
            await directory.close()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(check())


def test_closed_directory_cannot_be_reopened_or_mutated_by_pending_refresh():
    async def check():
        gate = threading.Event()
        client = Mock()
        client.list_agents.side_effect = lambda **kw: gate.wait(3) and [{"id": 1}]
        client.list_inboxes.return_value = []
        directory = ChatwootDirectory("local", settings(), client=client)
        directory.WAIT_SECONDS = 0.02
        try:
            await directory.enrich({})
            pending = directory.refresh
            await directory.close()
            original = (directory.status, deepcopy(directory.cached), directory.refresh)
            gate.set()
            await pending
            assert (directory.status, directory.cached, directory.refresh) == original
            directory.next_refresh = 0
            await directory.enrich({})
            assert directory.executor is None
            assert directory.refresh is None
        finally:
            gate.set()
            await directory.close()
    asyncio.run(check())


def test_old_refresh_callback_does_not_replace_new_task():
    async def check():
        directory = ChatwootDirectory("local", settings(), client=Mock())
        old = asyncio.get_running_loop().create_future()
        current = asyncio.get_running_loop().create_future()
        old.set_result({"agents": [], "inboxes": []})
        directory.refresh = current
        directory._apply_refresh(old)
        assert directory.refresh is current
        assert not directory.initialized
        current.cancel()
        directory.refresh = None
        await directory.close()
    asyncio.run(check())
