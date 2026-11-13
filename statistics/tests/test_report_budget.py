"""统计读取预算与异常来源回归。"""
import threading
import pytest
from psycopg.errors import QueryCanceled
from psycopg import OperationalError
from ics_agent.statistics import repository

@pytest.mark.parametrize('seconds,capped,error,expected', [(7, True, QueryCanceled(), 'expired'), (20, False, QueryCanceled(), 'statement'), (7, True, OperationalError(), None), (7, True, KeyError(), None)])
def test_classification(seconds, capped, error, expected):
    budget = repository.report_budget(seconds=seconds, clock=lambda: 0)
    budget.last_capped = capped
    assert budget.reason is None
    assert repository.classify(budget, error) == expected

def test_abort_priority():
    abort = threading.Event()
    budget = repository.report_budget(abort_event=abort)
    abort.set()
    assert repository.classify(budget, ValueError()) == 'aborted'

def test_defaults():
    budget = repository.report_budget()
    assert budget.statement_seconds == 8
    assert budget.connect_seconds == 1
    assert 19 < budget.check() <= 20


import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from ics_agent.statistics import routes
from quality.query.runtime import QueryRuntime, ArchiveUnavailable
from quality.query.export import ExportCancelled, ExportTooLarge


@pytest.mark.parametrize('external', [False, True])
def test_report_disconnect_and_external_cancel_abort_thread(monkeypatch, external):
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.repository = SimpleNamespace(pool=object())
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime.slots = asyncio.Semaphore(1)
        started = threading.Event()
        seen = []

        def report(self, period, *, budget):
            seen.append(budget)
            started.set()
            assert budget.abort_event.wait(3)
            budget.check()

        monkeypatch.setattr(repository.StatisticsRepository, 'time_report', report)
        monkeypatch.setattr(routes, 'get_query_runtime', lambda: runtime)
        request = Mock(is_disconnected=AsyncMock(return_value=not external))
        task = asyncio.create_task(routes.run_report(request, 'time_report', object()))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            if external:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert (await task).status_code == 499
            assert seen[0].abort_event.is_set()
            assert await runtime._execute(lambda: 'free') == 'free'
        finally:
            for budget in seen:
                budget.abort_event.set()
            await asyncio.gather(task, return_exceptions=True)
            runtime.repository = None
            await runtime.close()
    asyncio.run(check())


@pytest.mark.parametrize('error', [ExportCancelled(), QueryCanceled(), ExportTooLarge('limit'), KeyError()])
def test_runtime_export_cancel_precedes_business_errors(error, caplog):
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = SimpleNamespace()
        runtime.export_repository = SimpleNamespace(pool=object(), close=lambda: None)

        def builder(*args, cancelled, **kwargs):
            cancelled.set()
            raise error

        try:
            with pytest.raises(ExportCancelled):
                await runtime.export('time', object(), builder=builder)
            assert not [r for r in caplog.records if r.levelname == 'WARNING']
        finally:
            await runtime.close()
    asyncio.run(check())


def test_runtime_export_regular_failure_is_unavailable(caplog):
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = SimpleNamespace()
        runtime.export_repository = SimpleNamespace(pool=object(), close=lambda: None)

        def builder(*args, **kwargs):
            raise KeyError('unexpected')

        try:
            with pytest.raises(ArchiveUnavailable):
                await runtime.export('time', object(), builder=builder)
            assert any(r.levelname == 'WARNING' for r in caplog.records)
        finally:
            await runtime.close()
    asyncio.run(check())


def test_disconnected_completed_failure_is_retrieved(monkeypatch):
    import gc

    async def check():
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        runtime = SimpleNamespace(ready=True, repository=SimpleNamespace(pool=None),
                                  _execute=AsyncMock(side_effect=KeyError('query failed')))
        monkeypatch.setattr(routes, 'get_query_runtime', lambda: runtime)
        request = Mock(is_disconnected=AsyncMock(return_value=True))
        response = await routes.run_report(request, 'time_report', object())
        assert response.status_code == 499
        # 释放异常回溯与 mock 引用后检查事件循环，确保失败已被取走。
        runtime._execute.side_effect = None
        gc.collect()
        await asyncio.sleep(0)
        assert not unhandled
    asyncio.run(check())


def test_disconnected_queued_report_never_enters_repository(monkeypatch):
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.repository = SimpleNamespace(pool=None)
        runtime.executor = ThreadPoolExecutor(max_workers=1)
        runtime.slots = asyncio.Semaphore(0)
        report = Mock()
        factory = Mock(wraps=repository.report_budget)
        monkeypatch.setattr(routes, 'get_query_runtime', lambda: runtime)
        monkeypatch.setattr(routes, 'report_budget', factory)
        monkeypatch.setattr(repository.StatisticsRepository, 'time_report', report)
        try:
            response = await routes.run_report(Mock(is_disconnected=AsyncMock(return_value=True)),
                                               'time_report', object())
            assert response.status_code == 499
            assert factory.call_args.kwargs['abort_event'].is_set()
            runtime.slots.release()
            assert await runtime._execute(lambda: 'free') == 'free'
            report.assert_not_called()
        finally:
            runtime.repository = None
            await runtime.close()
    asyncio.run(check())
