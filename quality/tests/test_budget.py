"""验证数据库预算的事务顺序、外部取消与连接归还边界。"""

from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from quality.processing.budget import BudgetConnection, BudgetExceeded, MaintenanceBudget


class Pool:
    def __init__(self):
        self.events = []
        self.cancelled = threading.Event()
        self.budget = None

    @contextmanager
    def connection(self, *, timeout):
        self.timeout = timeout
        try:
            yield self
        finally:
            assert not self.budget.thread.is_alive()
            self.events.append('returned')

    @contextmanager
    def transaction(self):
        self.events.append('begin')
        try:
            yield
        except BaseException:
            assert not self.budget.thread.is_alive()
            self.events.append('rollback')
            raise
        else:
            self.events.append('commit')

    def execute(self, query, params=None):
        self.events.append((query, params))
        return SimpleNamespace()

    def cancel_safe(self, **kwargs):
        self.cancelled.set()


def test_setup_precedes_timeout_configuration_and_connection_wait_is_capped():
    pool = Pool()
    budget = pool.budget = MaintenanceBudget(20, connect_seconds=1)
    with budget.connection(pool, setup=lambda raw: raw.execute('SET TRANSACTION READ ONLY')) as connection:
        connection.execute('SELECT 1')
    assert pool.timeout == 1
    assert pool.events[0] == 'begin'
    assert pool.events[1] == ('SET TRANSACTION READ ONLY', None)
    assert 'set_config' in pool.events[2][0]
    assert pool.events[-1] == 'returned'


def test_abort_cancels_connection_and_watchdog_stops_before_rollback():
    pool = Pool()
    abort = threading.Event()
    budget = pool.budget = MaintenanceBudget(20, abort_event=abort)
    with pytest.raises(BudgetExceeded):
        with budget.connection(pool):
            abort.set()
            assert pool.cancelled.wait(1)
            budget.check()
    assert budget.reason == 'aborted'
    assert pool.events[-2:] == ['rollback', 'returned']


def test_deadline_between_statements_prevents_the_next_statement():
    current = [0]
    pool = Pool()
    budget = pool.budget = MaintenanceBudget(20, clock=lambda: current[0])
    with pytest.raises(BudgetExceeded):
        with budget.connection(pool) as connection:
            connection.execute('SELECT 1')
            current[0] = 21
            connection.execute('SELECT 2')
    assert not any(isinstance(item, tuple) and item[0] == 'SELECT 2' for item in pool.events)
    assert budget.reason == 'expired'


def test_reason_uses_real_deadline_and_abort_takes_priority():
    current = [0]
    abort = threading.Event()
    budget = MaintenanceBudget(20, clock=lambda: current[0], abort_event=abort)
    assert budget.reason is None
    current[0] = 20
    assert not budget.expired.is_set()
    assert budget.reason == 'expired'
    abort.set()
    assert budget.reason == 'aborted'
    with pytest.raises(BudgetExceeded):
        budget.check()


@pytest.mark.parametrize('seconds,capped', [(7, True), (20, False)])
def test_capped_statement_does_not_itself_expire_the_budget(seconds, capped):
    budget = MaintenanceBudget(seconds, statement_seconds=8, clock=lambda: 0)
    raw = Pool()
    BudgetConnection(raw, budget).execute('SELECT 1')
    assert budget.last_capped is capped
    assert budget.reason is None
    assert raw.events[0][1] == (str(min(seconds, 8) * 1000),)


def test_connection_wait_keeps_shorter_remaining_budget():
    pool = Pool()
    budget = pool.budget = MaintenanceBudget(.5, connect_seconds=1, clock=lambda: 0)
    with budget.connection(pool):
        pass
    assert pool.timeout == .5


def test_setup_failure_stops_watchdog_before_rollback():
    pool = Pool()
    budget = pool.budget = MaintenanceBudget(20)

    def setup(raw):
        raise RuntimeError('setup failed')

    with pytest.raises(RuntimeError):
        with budget.connection(pool, setup=setup):
            pytest.fail('初始化失败后不得执行查询')
    assert pool.events == ['begin', 'rollback', 'returned']
