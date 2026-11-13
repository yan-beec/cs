"""默认预算共享、超时分类竞态和导出取消的上层响应。"""
import asyncio
from contextlib import contextmanager
from datetime import date
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import OperationalError
from psycopg.errors import QueryCanceled

from ics_agent.app.core.auth import require_admin
from ics_agent.statistics import repository, routes, export
from ics_agent.statistics.periods import ReportPeriod
from quality.processing.budget import BudgetConnection
from quality.query import routes as archive_routes
from quality.query.config import QuerySettings
from quality.query.export import ExportCancelled, ExportTooLarge
from quality.query.runtime import QueryRuntime


@pytest.mark.parametrize('method', ['time_report', 'agent_report', 'quality_report', 'user_report', 'user_detail'])
def test_default_budget_created_once_and_shared(monkeypatch, method):
    period = ReportPeriod.build('day', date(2099, 1, 1))
    budget = Mock()
    factory = Mock(return_value=budget)
    monkeypatch.setattr(repository, 'report_budget', factory)
    cursor = Mock()
    cursor.fetchone.return_value = {'pending_jobs': 0, 'last_processed_at': None}
    cursor.fetchall.return_value = []
    connection = Mock(execute=Mock(return_value=cursor))
    used = []

    @contextmanager
    def read(self, passed):
        used.append(passed)
        yield connection

    monkeypatch.setattr(repository.StatisticsRepository, '_read', read)
    monkeypatch.setattr(repository.StatisticsRepository, '_agents', lambda *a, **kw: {'total_failed_inquiries': 0, 'total_inquiries': 0})
    monkeypatch.setattr(repository.user_ranking, 'user_report', lambda *a, **kw: {'users': []})
    monkeypatch.setattr(repository.user_ranking, 'user_detail', lambda *a, **kw: {'items': []})
    kwargs = {'source_key': 's', 'account_id': 1, 'contact_id': '42'} if method == 'user_detail' else {}
    getattr(repository.StatisticsRepository(object()), method)(period, **kwargs)
    factory.assert_called_once_with()
    assert used == [budget]
    if method == 'agent_report':
        assert budget.check.call_count == 2


@pytest.mark.parametrize('after', [False, True])
def test_presence_checks_same_default_budget_before_and_after(monkeypatch, after):
    period = ReportPeriod.build('day', date(2099, 1, 1))
    budget = Mock()
    budget.check.side_effect = [None, repository.ReportBudgetExceeded()] if after else repository.ReportBudgetExceeded()
    monkeypatch.setattr(repository, 'report_budget', Mock(return_value=budget))

    @contextmanager
    def read(self, passed):
        assert passed is budget
        yield Mock()

    monkeypatch.setattr(repository.StatisticsRepository, '_read', read)
    monkeypatch.setattr(repository.StatisticsRepository, '_agents', lambda *a, **kw: {})
    presence = Mock(return_value={})
    monkeypatch.setattr(repository.StatisticsRepository, '_presence', presence)
    with pytest.raises(repository.ReportBudgetExceeded):
        repository.StatisticsRepository(object()).agent_report(period)
    assert presence.call_count == int(after)


@pytest.mark.parametrize('seconds,expected', [(.5, 'expired'), (20, 'statement')])
def test_capped_query_timeout_race_before_deadline(seconds, expected):
    budget = repository.report_budget(seconds=seconds, clock=lambda: 100, check_interval=3600)
    raw = Mock()
    raw.execute.side_effect = [None, QueryCanceled()]
    with pytest.raises(QueryCanceled) as error:
        BudgetConnection(raw, budget).execute('SELECT pg_sleep(1)')
    assert not budget.expired.is_set()
    assert budget.reason is None
    assert repository.classify(budget, error.value) == expected


@pytest.mark.parametrize('error', [None, OperationalError('db'), KeyError('python')])
def test_capped_statement_never_implies_expiration(error):
    budget = repository.report_budget(seconds=7, clock=lambda: 0)
    raw = Mock()
    if isinstance(error, OperationalError):
        raw.execute.side_effect = [None, error]
        with pytest.raises(OperationalError):
            BudgetConnection(raw, budget).execute('SELECT 1')
    else:
        BudgetConnection(raw, budget).execute('SELECT 1')
    assert budget.last_capped is True
    assert budget.reason is None
    if error is not None:
        assert repository.classify(budget, error) is None


@pytest.mark.parametrize('endpoint', ['/admin/api/quality/export.xlsx', '/admin/api/statistics/time/export.xlsx',
                                      '/admin/api/statistics/users/export.xlsx'])
def test_export_cancel_response_is_restart(monkeypatch, endpoint):
    runtime = Mock(export=AsyncMock(side_effect=ExportCancelled()))
    monkeypatch.setattr(routes, 'get_query_runtime', lambda: runtime)
    monkeypatch.setattr(archive_routes, 'get_query_runtime', lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(archive_routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    response = TestClient(app).get(endpoint)
    assert response.status_code == 503
    assert response.json()['detail'] == '服务正在重启，请稍后重试。'
    assert response.headers['Retry-After'] == '10'


def test_runtime_close_cancels_connected_export():
    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = SimpleNamespace()
        runtime.export_repository = SimpleNamespace(pool=object(), close=lambda: None)
        started = threading.Event()

        def builder(*args, cancelled, **kwargs):
            started.set()
            assert cancelled.wait(3)
            raise QueryCanceled()

        task = asyncio.create_task(runtime.export('time', object(), builder=builder))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await runtime.close()
            with pytest.raises(ExportCancelled):
                await task
        finally:
            await runtime.close()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize('reason', ['aborted', 'expired', 'statement', 'other'])
def test_export_converts_repository_failure_with_its_own_budget(monkeypatch, reason):
    cancelled = threading.Event()
    settings = QuerySettings(_env_file=None, reader_password='test', export_timeout_seconds=120)
    period = ReportPeriod.build('day', date(2026, 10, 1))

    def report(self, period, *, budget, **kwargs):
        assert budget.abort_event is cancelled
        assert 20 < budget.check() <= 120
        assert budget.statement_seconds == 8
        if reason == 'aborted':
            cancelled.set()
        elif reason == 'expired':
            budget.last_capped = True
        if reason == 'other':
            budget.last_capped = True
            raise OperationalError()
        raise QueryCanceled()

    monkeypatch.setattr(repository.StatisticsRepository, 'time_report', report)
    expected = ExportCancelled if reason == 'aborted' else OperationalError if reason == 'other' else ExportTooLarge
    with pytest.raises(expected) as error:
        export.create_export(None, settings, 'time', period, cancelled=cancelled)
    if expected is ExportTooLarge:
        assert '导出用时过长' in str(error.value)


def test_export_deadline_used_up_before_repository_read(monkeypatch):
    settings = QuerySettings(_env_file=None, reader_password='test', export_timeout_seconds=120)
    period = ReportPeriod.build('day', date(2026, 10, 1))
    monkeypatch.setattr(export, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 121])))

    def report(self, period, *, budget, **kwargs):
        budget.check()

    monkeypatch.setattr(repository.StatisticsRepository, 'time_report', report)
    with pytest.raises(ExportTooLarge, match='导出用时过长'):
        export.create_export(None, settings, 'time', period)


def test_python_presence_failure_after_capped_success_is_not_timeout(monkeypatch):
    period = ReportPeriod.build('day', date(2099, 1, 1))
    budget = repository.report_budget(seconds=7, clock=lambda: 0)

    @contextmanager
    def read(self, passed):
        connection = BudgetConnection(Mock(), passed)
        connection.execute('SELECT 1')
        yield connection

    monkeypatch.setattr(repository.StatisticsRepository, '_read', read)
    monkeypatch.setattr(repository.StatisticsRepository, '_agents', lambda *a, **kw: {})
    monkeypatch.setattr(repository.StatisticsRepository, '_presence', Mock(side_effect=KeyError('presence')))
    with pytest.raises(KeyError) as error:
        repository.StatisticsRepository(None).agent_report(period, budget=budget)
    assert budget.last_capped
    assert repository.classify(budget, error.value) is None


@pytest.mark.parametrize('endpoint', ['/admin/api/quality/export.xlsx', '/admin/api/statistics/time/export.xlsx',
                                      '/admin/api/statistics/users/export.xlsx'])
def test_runtime_shutdown_returns_restart_to_connected_http_request(monkeypatch, endpoint):
    import httpx

    async def check():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.settings = QuerySettings(_env_file=None, reader_password='test')
        started = threading.Event()

        def build(*args, cancelled, **kwargs):
            started.set()
            assert cancelled.wait(3)
            raise QueryCanceled()

        runtime.export_repository = SimpleNamespace(pool=None, close=lambda: None, create_export=build)
        monkeypatch.setattr(routes, 'get_query_runtime', lambda: runtime)
        monkeypatch.setattr(archive_routes, 'get_query_runtime', lambda: runtime)
        monkeypatch.setattr(routes, 'create_export', build)
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(archive_routes.router)
        app.dependency_overrides[require_admin] = lambda: object()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.get(endpoint))
            try:
                assert await asyncio.to_thread(started.wait, 1)
                await runtime.close()
                response = await asyncio.wait_for(task, 2)
                assert response.status_code == 503
                assert response.json()['detail'] == '服务正在重启，请稍后重试。'
                assert response.headers['Retry-After'] == '10'
            finally:
                await runtime.close()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())
