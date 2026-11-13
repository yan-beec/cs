from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from quality.processing.repository import ProcessingRepository
from quality.processing.rollups import RollupBuilder
from quality.processing.service_facts import FactBackfill
from quality.processing import worker


class MaintenanceTest(unittest.TestCase):
    def test_retry_batches_rotate_busy_scope_and_commit_only_bounded_rows(self):
        stored = []
        busy = [True]
        class Connection:
            def execute(self, query, params=None):
                rows = [{'round_id': value} for value in params[0]] if 'SELECT round_id FROM' in query else []
                return SimpleNamespace(fetchall=lambda: rows,
                    fetchone=lambda: {'locked': not busy[0] or params[0] != 1})
            @contextmanager
            def transaction(self):
                yield
        class Pool:
            @contextmanager
            def connection(self, **kwargs):
                yield Connection()
        first, second = ('s', 1, 1), ('s', 1, 2)
        backfill = FactBackfill(Pool(), lambda source, account, conversation: conversation, batch_size=2)
        backfill.skipped = {first: ['a', 'b', 'c'], second: ['d', 'e']}
        with patch('quality.processing.service_facts.store', side_effect=lambda connection, ids: stored.append(ids)):
            self.assertTrue(backfill.step())
            self.assertEqual(list(backfill.skipped), [second, first])
            self.assertEqual(backfill.skipped[first], ['c', 'a', 'b'])
            self.assertTrue(backfill.step())
            self.assertEqual(stored, [['d', 'e']])
            self.assertEqual(backfill.skipped, {first: ['c', 'a', 'b']})
            self.assertFalse(backfill.done)
            busy[0] = False
            self.assertTrue(backfill.step())
            self.assertTrue(backfill.step())
            self.assertFalse(backfill.step())
        self.assertEqual(stored, [['d', 'e'], ['c', 'a'], ['b']])
        self.assertTrue(backfill.done)
        self.assertEqual(backfill.skipped, {})

    def test_retry_batch_failure_preserves_queue_order_and_cursor(self):
        class Connection:
            def execute(self, query, params=None):
                rows = [{'round_id': value} for value in params[0]] if 'SELECT round_id FROM' in query else []
                return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: {'locked': True})
            @contextmanager
            def transaction(self):
                yield
        class Pool:
            @contextmanager
            def connection(self, **kwargs):
                yield Connection()
        backfill = FactBackfill(Pool(), lambda *args: 1, batch_size=2)
        first, second = ('s', 1, 1), ('s', 1, 2)
        backfill.skipped = {first: ['a', 'b', 'c'], second: ['d']}
        backfill.cursor = (datetime(2026, 10, 1, tzinfo=timezone.utc), 'z')
        before_cursor = backfill.cursor
        attempts = []
        def fail_store(connection, ids):
            attempts.append(ids)
            raise RuntimeError()
        with patch('quality.processing.service_facts.store', side_effect=fail_store):
            with self.assertRaises(RuntimeError):
                backfill.step()
        self.assertEqual(attempts, [['a', 'b']])
        self.assertEqual(list(backfill.skipped), [first, second])
        self.assertEqual(backfill.skipped, {first: ['a', 'b', 'c'], second: ['d']})
        self.assertEqual(backfill.cursor, before_cursor)
        self.assertFalse(backfill.done)

    def test_budget_is_shared_across_build_preparation_and_second_connection(self):
        from quality.processing.budget import MaintenanceBudget
        from quality.processing.rollups import RollupBudgetExceeded
        clock, waits, committed = [0], [], []
        class Connection:
            def execute(self, query, params=None):
                return SimpleNamespace()
            @contextmanager
            def transaction(self):
                yield
                committed.append(True)
                clock[0] += .7
        class Pool:
            @contextmanager
            def connection(self, *, timeout):
                waits.append(timeout)
                if len(waits) == 2:
                    clock[0] += .5
                yield Connection()
        def budget(*args, **kwargs):
            return MaintenanceBudget(*args, **kwargs, clock=lambda: clock[0])
        builder = RollupBuilder(Pool(), build_budget_seconds=1)
        with patch('quality.processing.rollups.MaintenanceBudget', side_effect=budget):
            with self.assertRaises(RollupBudgetExceeded):
                builder.build(date(2026, 10, 1))
        self.assertEqual(committed, [True])
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[1], .3)

    def test_backfill_budget_covers_read_and_write_acquisitions(self):
        from quality.processing.budget import MaintenanceBudget, BudgetExceeded
        clock, waits = [0], []
        row = dict(round_id='r', source_key='s', account_id=1, conversation_id=2,
                   started_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        class Connection:
            def execute(self, query, params=None):
                return SimpleNamespace(fetchall=lambda: [row])
            @contextmanager
            def transaction(self):
                yield
        class Pool:
            @contextmanager
            def connection(self, *, timeout):
                waits.append(timeout)
                clock[0] += .6
                yield Connection()
        def budget(*args, **kwargs):
            return MaintenanceBudget(*args, **kwargs, clock=lambda: clock[0])
        backfill = FactBackfill(Pool(), lambda *a: 1, batch_budget_seconds=1)
        with patch('quality.processing.service_facts.MaintenanceBudget', side_effect=budget):
            with self.assertRaises(BudgetExceeded):
                backfill.step()
        self.assertIsNone(backfill.cursor)
        self.assertFalse(backfill.done)
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[1], .4)

    def test_locked_retry_is_preserved_and_never_marks_backfill_complete(self):
        class Connection:
            def execute(self, query, params=None):
                return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: {'locked': False})
            @contextmanager
            def transaction(self):
                yield
        class Pool:
            @contextmanager
            def connection(self, **kwargs):
                yield Connection()
        backfill = FactBackfill(Pool(), lambda *args: 1)
        backfill.skipped = {('s', 1, 2): ['r']}
        self.assertTrue(backfill.step())
        self.assertTrue(backfill.step())
        self.assertEqual(backfill.skipped, {('s', 1, 2): ['r']})
        self.assertFalse(backfill.done)

    def test_maintenance_routes_to_rollups_only_after_backfill_finishes(self):
        repository = object.__new__(ProcessingRepository)
        repository.facts_backfill = SimpleNamespace(done=False)
        actions = []
        repository.backfill_facts = lambda: actions.append('facts') or True
        repository.build_rollups = lambda: actions.append('rollups') or True
        self.assertTrue(repository.maintenance_step())
        repository.facts_backfill.done = True
        self.assertTrue(repository.maintenance_step())
        self.assertEqual(actions, ['facts', 'rollups'])

    def test_cursor_keeps_native_batches_and_checks_each_parameter(self):
        from quality.processing.budget import MaintenanceBudget, BudgetExceeded
        clock = [0]
        batches, singles, configurations = [], [], []
        class Cursor:
            def execute(self, *args, **kwargs):
                singles.append(args)
            def executemany(self, query, params):
                batches.append(list(params))
        class Connection:
            def execute(self, *args, **kwargs):
                configurations.append(args)
            @contextmanager
            def cursor(self):
                yield Cursor()
            @contextmanager
            def transaction(self):
                yield
        class Pool:
            @contextmanager
            def connection(self, **kwargs):
                yield Connection()
        budget = MaintenanceBudget(1, clock=lambda: clock[0])
        with budget.connection(Pool()) as connection:
            with connection.cursor() as cursor:
                cursor.executemany('INSERT', [(i,) for i in range(401)])
        self.assertEqual([len(batch) for batch in batches], [200, 200, 1])
        self.assertEqual(singles, [])
        self.assertEqual(len(configurations), 3)
        def params():
            yield (1,)
            clock[0] = 2
            yield (2,)
        with self.assertRaises(BudgetExceeded):
            with budget.connection(Pool()) as connection:
                with connection.cursor() as cursor:
                    cursor.executemany('INSERT', params())

    def test_budget_checks_sql_and_commit_and_joins_canceller(self):
        try:
            from quality.processing.budget import MaintenanceBudget, BudgetExceeded
        except ImportError:
            self.fail('缺少维护总预算')
        clock = [0]
        statements, outcomes, cancellations = [], [], []
        class Connection:
            def execute(self, sql, params=None):
                statements.append(sql)
                return SimpleNamespace()
            @contextmanager
            def transaction(self):
                try:
                    yield
                except BaseException:
                    self_test.assertFalse(budget.thread.is_alive(), '回滚开始前必须结束取消线程')
                    outcomes.append('rollback')
                    raise
                else:
                    outcomes.append('commit')
            def cancel_safe(self, *, timeout):
                cancellations.append(timeout)
        class Pool:
            @contextmanager
            def connection(self, *, timeout):
                self.timeout = timeout
                try:
                    yield Connection()
                finally:
                    self.returned = True
        pool = Pool()
        self_test = self
        budget = MaintenanceBudget(1, clock=lambda: clock[0])
        with self.assertRaises(BudgetExceeded):
            with budget.connection(pool) as connection:
                connection.execute('SELECT 1')
                clock[0] = 2
                connection.execute('SELECT 2')
        self.assertNotIn('SELECT 2', statements)
        self.assertEqual(outcomes, ['rollback'])
        self.assertTrue(pool.returned)
        self.assertLessEqual(pool.timeout, 1)
        self.assertFalse(budget.thread.is_alive())
        clock[0] = 0
        budget = MaintenanceBudget(1, clock=lambda: clock[0])
        with self.assertRaises(BudgetExceeded):
            with budget.connection(pool):
                clock[0] = 2
        self.assertEqual(outcomes, ['rollback', 'rollback'])

    def test_watchdog_retries_failed_cancel_and_stops_before_return(self):
        try:
            from quality.processing.budget import MaintenanceBudget, BudgetExceeded
        except ImportError:
            self.fail('缺少维护总预算')
        calls = []
        class Connection:
            @contextmanager
            def transaction(self):
                yield
            def cancel_safe(self, *, timeout):
                calls.append(timeout)
                raise RuntimeError('取消失败')
        class Pool:
            @contextmanager
            def connection(self, **kwargs):
                yield Connection()
        budget = MaintenanceBudget(.01, cancel_interval=.01)
        with self.assertRaises(BudgetExceeded):
            with budget.connection(Pool()):
                time.sleep(.06)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(0 < value <= 1 for value in calls))
        self.assertFalse(budget.thread.is_alive())

    def test_unconfirmed_backfill_blocks_rollup(self):
        repository = object.__new__(ProcessingRepository)
        repository.facts_backfill = None
        repository.rollups_idle_until = 0
        calls = []
        repository.rollups = SimpleNamespace(step=lambda: calls.append(1))
        self.assertFalse(repository.build_rollups())
        self.assertEqual(calls, [])

    def test_busy_worker_advances_maintenance(self):
        stopped = threading.Event()
        actions = []
        clock = [0]
        class Repository:
            def process_next(self, worker_id):
                clock[0] += 45
                return True
            def maintenance_step(self):
                actions.append(clock[0])
                if len(actions) == 2:
                    stopped.set()
                return True
        runner = getattr(worker, 'run_loop', None)
        self.assertIsNotNone(runner, '持续事件时仍需执行维护时间片')
        runner(Repository(), 'worker', stopped, 1, clock=lambda: clock[0])
        self.assertEqual(actions, [90, 180])

    def test_failure_backoff_allows_older_day_then_retries(self):
        newer, older = date(2026, 10, 2), date(2026, 10, 1)
        class Pool:
            @contextmanager
            def connection(self):
                yield SimpleNamespace(execute=lambda *args: None)
        builder = RollupBuilder(Pool())
        clock = [0]
        built = []
        def select(connection, now, exclude=()):
            return older if newer in exclude else newer
        def build(day):
            built.append(day)
            if len(built) == 1:
                raise RuntimeError('敏感内容不能进入日志')
        builder.next_day = select
        builder.build = build
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            with self.assertLogs('quality.processing.rollups', level='WARNING') as logs:
                self.assertTrue(builder.step(datetime.now(timezone.utc)))
            self.assertNotIn('敏感内容', str(logs.output))
            self.assertTrue(builder.step(datetime.now(timezone.utc)))
            clock[0] = 301
            self.assertTrue(builder.step(datetime.now(timezone.utc)))
        self.assertEqual(built, [newer, older, newer])
        self.assertNotIn(newer, builder.failures)

    def test_failed_backfill_keeps_cursor_and_pending_retry(self):
        row = dict(round_id='r', source_key='s', account_id=1, conversation_id=2,
                   started_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        class Connection:
            def execute(self, sql, params=None):
                return SimpleNamespace(fetchall=lambda: [row])
            @contextmanager
            def transaction(self):
                yield
        class Pool:
            @contextmanager
            def connection(self, **kwargs):
                yield Connection()
        backfill = FactBackfill(Pool(), lambda *a: 1, pause_seconds=0)
        backfill._store = lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
        with self.assertRaises(RuntimeError):
            backfill.step()
        self.assertIsNone(backfill.cursor, '失败批次不得推进游标')
        self.assertFalse(backfill.done)


if __name__ == '__main__':
    unittest.main()
