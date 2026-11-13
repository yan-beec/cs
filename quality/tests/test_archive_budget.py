"""归档预算默认值、逐批检查和运行时异常分类。"""

import asyncio
import threading
from unittest.mock import Mock, patch

import pytest
from psycopg.errors import QueryCanceled
from psycopg_pool import PoolTimeout

from quality.processing.budget import BudgetConnection, BudgetCursor
from quality.query.budget import ArchiveBudgetExceeded, archive_budget
from quality.query.runtime import (
    ArchiveBusy, ArchiveQueryAborted, ArchiveQueryTimeout, QueryRuntime,
)


def test_archive_defaults():
    budget = archive_budget(clock=lambda: 123)
    assert budget.deadline == 143
    assert budget.thread is None
    assert (budget.statement_seconds, budget.connect_seconds) == (3, 1)

def test_fetchmany_refreshes_and_checks():
    connection = Mock()
    raw = Mock()
    raw.fetchmany.return_value = [1]
    assert BudgetCursor(raw, connection).fetchmany(500) == [1]
    connection._prepare.assert_called_once()
    connection.budget.check.assert_called_once()

@pytest.mark.parametrize('error,aborted,expected', [
    (QueryCanceled(), False, ArchiveQueryTimeout),
    (ArchiveBudgetExceeded(), False, ArchiveQueryTimeout),
    (QueryCanceled(), True, ArchiveQueryAborted),
    (PoolTimeout(), False, ArchiveBusy),
])
def test_runtime_classification_preserves_readiness(error, aborted, expected):
    async def run():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.status = 'ready'
        runtime.repository = Mock()
        abort = threading.Event()
        if aborted:
            abort.set()
        async def execute(function, *args, **kwargs):
            assert kwargs['budget'].abort_event is abort
            raise error
        with patch.object(runtime, '_execute', side_effect=execute):
            with pytest.raises(expected):
                await runtime.call('list_customers', abort=abort)
        assert runtime.ready and runtime.status == 'ready'
    asyncio.run(run())

def test_fetchmany_stops_before_fetch_when_aborted():
    abort = threading.Event()
    abort.set()
    raw = Mock()
    cursor = BudgetCursor(raw, BudgetConnection(Mock(), archive_budget(abort_event=abort)))
    with pytest.raises(ArchiveBudgetExceeded):
        cursor.fetchmany(500)
    raw.fetchmany.assert_not_called()

def test_save_timeout_preserves_original_readiness_without_budget():
    async def run():
        runtime = QueryRuntime()
        runtime.ready = True
        runtime.repository = Mock()
        async def execute(function, *args, **kwargs):
            assert 'budget' not in kwargs
            raise QueryCanceled()
        with patch.object(runtime, '_execute', side_effect=execute):
            with pytest.raises(ArchiveQueryTimeout):
                await runtime.call('save_round_review')
        assert runtime.ready
    asyncio.run(run())
