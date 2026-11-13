"""验证查询繁忙提示、短暂排队及客户端断开后的容量释放。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from quality.query import routes, runtime as runtime_module
from quality.query.runtime import QueryRuntime


def test_query_waits_for_transient_capacity_and_reports_busy_separately(monkeypatch):
    async def check():
        runtime = QueryRuntime()
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime.slots = asyncio.Semaphore(0)
        task = asyncio.create_task(runtime._execute(lambda: "result"))
        try:
            await asyncio.sleep(0.3)
            runtime.slots.release()
            assert await task == "result"
            await runtime.slots.acquire()
            with pytest.raises(runtime_module.ArchiveBusy):
                await runtime._execute(lambda: "unexpected")
            monkeypatch.setattr(routes, "get_query_runtime", lambda: Mock(call=AsyncMock(side_effect=runtime_module.ArchiveBusy())))
            with pytest.raises(HTTPException) as error:
                await routes._call("list_rounds")
            assert error.value.status_code == 503
            assert "繁忙" in error.value.detail
        finally:
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()
    asyncio.run(check())


def test_disconnected_queued_request_never_runs_repository(monkeypatch):
    async def check():
        runtime = QueryRuntime()
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime.slots = asyncio.Semaphore(0)
        runtime.ready = True
        called = []
        runtime.repository = Mock()
        runtime.repository.list_customers.side_effect = lambda: called.append(True)
        request = Mock(is_disconnected=AsyncMock(return_value=True))
        monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
        try:
            response = await routes._query(request, "list_customers")
            assert response.status_code == 499
            runtime.slots.release()
            await asyncio.sleep(0)
            assert called == []
            assert await runtime._execute(lambda: "slot-reusable") == "slot-reusable"
        finally:
            await runtime.close()
    asyncio.run(check())


@pytest.mark.parametrize("fails", [False, True])
def test_repeated_cancellation_holds_slot_until_database_thread_finishes(monkeypatch, fails):
    async def check():
        runtime = QueryRuntime()
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime.slots = asyncio.Semaphore(1)
        runtime.ready = True
        started = threading.Event()
        release = threading.Event()
        disconnected = asyncio.Event()

        def query(budget=None):
            started.set()
            assert release.wait(5)
            if fails:
                raise RuntimeError("查询在客户端断开后失败")
            return {"items": []}

        async def is_disconnected():
            disconnected.set()
            return True

        runtime.repository = Mock(list_customers=query)
        monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
        task = asyncio.create_task(routes._query(Mock(is_disconnected=is_disconnected), "list_customers"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await asyncio.wait_for(disconnected.wait(), timeout=1)
            # 先由断开检测取消内部任务，再模拟服务器清理请求时的第二次取消。
            await asyncio.sleep(0.05)
            assert runtime.slots.locked()
            task.cancel()
            await asyncio.sleep(0.05)
            assert runtime.slots.locked()
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await runtime._execute(lambda: "slot-reusable") == "slot-reusable"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.close()

    asyncio.run(check())
