"""真实数据库取消语句并释放查询名额。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest

from psycopg_pool import ConnectionPool

from quality.query.repository import QueryRepository
from quality.query.budget import archive_budget
from quality.processing.budget import classify
from quality.query.config import QuerySettings
from quality.query.runtime import QueryRuntime


@unittest.skipUnless(os.environ.get('PROCESSING_TEST_DATABASE') == '1', '需要隔离数据库')
class ArchiveBudgetDatabaseTest(unittest.TestCase):
    def test_sql_timeouts_and_abort_release_slots(self):
        settings = QuerySettings()
        if not settings.postgres_db.startswith(('quality_query_test_', 'quality_capture_test_')):
            self.fail('只能在隔离测试数据库运行')
        with ConnectionPool(conninfo='', kwargs=settings.connection_kwargs, min_size=1, max_size=1, timeout=1) as pool:
            async def check():
                runtime = QueryRuntime()
                runtime.executor = ThreadPoolExecutor(max_workers=1)
                runtime.slots = asyncio.Semaphore(1)
                try:
                    for expected, seconds, statement in [('expired', 1, 3), ('statement', 20, 3), ('aborted', 20, 3)]:
                        abort = threading.Event()
                        budget = archive_budget(seconds=seconds, abort_event=abort)
                        started = threading.Event()

                        def work():
                            repository = QueryRepository.__new__(QueryRepository)
                            repository.pool = pool
                            with repository._read(budget) as connection:
                                started.set()
                                connection.execute('SELECT pg_sleep(5)')

                        task = asyncio.create_task(runtime._execute(work))
                        self.assertTrue(await asyncio.to_thread(started.wait, 2))
                        if expected == 'aborted':
                            abort.set()
                        try:
                            await asyncio.wait_for(task, timeout=4)
                        except Exception as error:
                            self.assertEqual(classify(budget, error), expected)
                        else:
                            self.fail('慢语句未被取消')
                        self.assertFalse(budget.thread.is_alive())
                        self.assertEqual(await runtime._execute(lambda: 'reused'), 'reused')
                        self.assertEqual(pool.get_stats()['pool_available'], 1)
                finally:
                    await runtime.close()
            asyncio.run(check())
    def test_failed_cancel_keeps_slot_until_statement_timeout(self):
        from unittest.mock import patch
        import psycopg
        import time
        from quality.query.runtime import ArchiveQueryAborted, ArchiveQueryTimeout
        settings = QuerySettings()
        self.assertTrue(settings.postgres_db.startswith(('quality_query_test_', 'quality_capture_test_')))
        with ConnectionPool(conninfo='', kwargs=settings.connection_kwargs, min_size=1, max_size=1, timeout=1) as pool:
            async def check():
                runtime = QueryRuntime()
                runtime.executor = ThreadPoolExecutor(max_workers=1)
                runtime.slots = asyncio.Semaphore(1)
                runtime.ready, runtime.status = True, 'ready'
                try:
                    for reason in ('aborted', 'expired'):
                        abort = threading.Event()
                        started = threading.Event()
                        budget = archive_budget(1 if reason == 'expired' else 20, abort_event=abort)
                        captured = []

                        class Repository:
                            def list_customers(self, budget=None):
                                try:
                                    with budget.connection(pool) as connection:
                                        started.set()
                                        connection.execute('SELECT pg_sleep(5)')
                                except Exception as error:
                                    captured.append(error)
                                    raise

                        runtime.repository = Repository()
                        expected = ArchiveQueryAborted if reason == 'aborted' else ArchiveQueryTimeout
                        with patch.object(psycopg.Connection, 'cancel_safe', side_effect=OSError('模拟取消失败')), patch('quality.query.runtime.archive_budget', return_value=budget):
                            task = asyncio.create_task(runtime.call('list_customers', abort=abort))
                            self.assertTrue(await asyncio.to_thread(started.wait, 2))
                            await asyncio.sleep(.15)
                            if reason == 'aborted':
                                abort.set()
                            await asyncio.sleep(.3)
                            self.assertTrue(runtime.slots.locked())
                            began = time.monotonic()
                            with self.assertRaises(expected):
                                await asyncio.wait_for(task, 4)
                            self.assertLess(time.monotonic()-began, 3.5)
                            self.assertEqual(classify(budget, captured[0]), reason)
                        self.assertTrue(runtime.ready)
                        self.assertEqual(runtime.status, 'ready')
                        for _ in range(5):
                            with pool.connection() as connection:
                                self.assertEqual(connection.execute('SELECT 1').fetchone()[0], 1)
                        self.assertFalse(runtime.slots.locked())
                        self.assertFalse(budget.thread.is_alive())
                        self.assertEqual(pool.get_stats()['pool_available'], 1)
                        self.assertEqual(pool.get_stats()['pool_size'], 1)
                finally:
                    runtime.repository = None
                    await runtime.close()
            asyncio.run(check())

    def test_pool_smaller_than_slots_is_busy_without_losing_readiness(self):
        from quality.query.runtime import ArchiveBusy
        from quality.query.repository import ArchiveFilters
        settings = QuerySettings()
        self.assertTrue(settings.postgres_db.startswith(('quality_query_test_', 'quality_capture_test_')))
        with ConnectionPool(conninfo='', kwargs=settings.connection_kwargs, min_size=1, max_size=1, timeout=1) as pool:
            async def check():
                runtime = QueryRuntime()
                runtime.ready, runtime.status = True, 'ready'
                runtime.executor = ThreadPoolExecutor(max_workers=2)
                runtime.slots = asyncio.Semaphore(2)
                class Repository:
                    def list_customers(self, filters, budget=None):
                        with budget.connection(pool) as connection:
                            connection.execute('SELECT 1')
                        return {}
                runtime.repository = Repository()
                try:
                    with pool.connection():
                        with self.assertRaises(ArchiveBusy):
                            await runtime.call('list_customers', ArchiveFilters())
                    self.assertTrue(runtime.ready)
                    self.assertEqual(await runtime.call('list_customers', ArchiveFilters()), {})
                finally:
                    runtime.repository = None
                    await runtime.close()
            asyncio.run(check())
    def test_total_budget_covers_multiple_statements(self):
        from quality.query.repository import ArchiveFilters
        from quality.query.runtime import ArchiveQueryTimeout
        from unittest.mock import patch
        import time
        settings = QuerySettings()
        self.assertTrue(settings.postgres_db.startswith(('quality_query_test_', 'quality_capture_test_')))
        with ConnectionPool(conninfo='', kwargs=settings.connection_kwargs, min_size=1, max_size=1, timeout=1) as pool:
            async def check():
                runtime = QueryRuntime()
                runtime.executor = ThreadPoolExecutor(max_workers=2)
                runtime.slots = asyncio.Semaphore(2)
                runtime.ready = True
                repository = QueryRepository.__new__(QueryRepository)
                repository.pool = pool
                runtime.repository = repository
                def slow(connection, *args, **kwargs):
                    for _ in range(6):
                        connection.execute('SELECT pg_sleep(.8)')
                try:
                    with patch('quality.query.runtime.archive_budget', side_effect=lambda **kw: archive_budget(2, **kw)), patch('quality.query.repository.load_facts', side_effect=slow):
                        began = time.monotonic()
                        with self.assertRaises(ArchiveQueryTimeout):
                            await runtime.call('list_rounds', ArchiveFilters())
                        self.assertLess(time.monotonic()-began, 3)
                        self.assertTrue(runtime.ready)
                        self.assertEqual(runtime.slots._value, 2)
                finally:
                    runtime.repository = None
                    await runtime.close()
            asyncio.run(check())

    def test_server_cursor_batches_refresh_timeout_and_stop(self):
        from quality.query.budget import ArchiveBudgetExceeded
        import time
        settings = QuerySettings()
        self.assertTrue(settings.postgres_db.startswith(('quality_query_test_', 'quality_capture_test_')))
        with ConnectionPool(conninfo='', kwargs=settings.connection_kwargs, min_size=1, max_size=1, timeout=1) as pool:
            for mode in ('aborted', 'expired'):
                abort = threading.Event()
                budget = archive_budget(1, abort_event=abort)
                with self.assertRaises(ArchiveBudgetExceeded) as raised:
                    with budget.connection(pool) as connection:
                        with connection.cursor(name='archive_budget_batches') as cursor:
                            cursor.execute('SELECT generate_series(1, 2000)')
                            self.assertEqual(len(cursor.fetchmany(500)), 500)
                            first = connection.raw.execute("SELECT setting::int FROM pg_settings WHERE name='statement_timeout' AND unit='ms'").fetchone()[0]
                            time.sleep(.1)
                            self.assertEqual(len(cursor.fetchmany(500)), 500)
                            second = connection.raw.execute("SELECT setting::int FROM pg_settings WHERE name='statement_timeout' AND unit='ms'").fetchone()[0]
                            self.assertLess(second, first)
                            if mode == 'aborted':
                                abort.set()
                            else:
                                time.sleep(1)
                            cursor.fetchmany(500)
                self.assertEqual(classify(budget, raised.exception), mode)
                self.assertFalse(budget.thread.is_alive())
                with pool.connection() as connection:
                    self.assertEqual(connection.execute('SELECT 1').fetchone()[0], 1)
    def test_two_abandoned_round_queries_release_slots_for_customers(self):
        from quality.query.repository import ArchiveFilters
        from quality.query import routes
        from unittest.mock import AsyncMock, patch
        from psycopg import connect
        from psycopg.rows import dict_row
        import time
        import json
        import inspect
        from quality.query.repository import load_facts as original_load_facts
        settings = QuerySettings()
        self.assertTrue(settings.postgres_db.startswith(('quality_query_test_', 'quality_capture_test_')))
        measurements = []
        with ConnectionPool(conninfo='', kwargs={**settings.connection_kwargs, 'row_factory': dict_row}, min_size=2, max_size=2, timeout=1) as pool:
            async def check():
                runtime = QueryRuntime()
                runtime.executor = ThreadPoolExecutor(max_workers=2)
                runtime.slots = asyncio.Semaphore(2)
                runtime.ready = True
                repository = QueryRepository.__new__(QueryRepository)
                repository.pool = pool
                runtime.repository = repository
                runtime._directory_options = AsyncMock(return_value={})
                async def active_sleep_count():
                    def read():
                        with connect(**settings.connection_kwargs, autocommit=True) as connection:
                            return connection.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active' AND query LIKE 'SELECT pg_sleep(2.5)%'").fetchone()[0]
                    return await asyncio.to_thread(read)
                def slow(connection, *args, **kwargs):
                    if any(frame.function == 'list_customers' for frame in inspect.stack()):
                        return original_load_facts(connection, *args, **kwargs)
                    while True:
                        connection.execute('SELECT pg_sleep(2.5) /* archive cancellation */')
                class Request:
                    disconnected = False
                    async def is_disconnected(self):
                        return self.disconnected
                try:
                    for _ in range(3):
                        requests = [Request(), Request()]
                        with patch.object(routes, 'get_query_runtime', return_value=runtime), patch('quality.query.repository.load_facts', side_effect=slow):
                            tasks = [asyncio.create_task(routes._query(request, 'list_rounds', ArchiveFilters())) for request in requests]
                            deadline = time.monotonic()+4
                            while await active_sleep_count() != 2:
                                self.assertLess(time.monotonic(), deadline)
                                await asyncio.sleep(.02)
                            for request in requests:
                                request.disconnected = True
                            began = time.monotonic()
                            fresh = asyncio.create_task(runtime.call('list_customers', ArchiveFilters()))
                            while await active_sleep_count():
                                self.assertLess(time.monotonic()-began, 4)
                                await asyncio.sleep(.02)
                            vanished = time.monotonic()-began
                            responses = await asyncio.gather(*tasks)
                            self.assertEqual([r.status_code for r in responses], [499, 499])
                            released = time.monotonic()-began
                            self.assertIsInstance(await fresh, dict)
                            self.assertTrue(runtime.ready)
                            self.assertEqual(runtime.slots._value, 2)
                            self.assertEqual(pool.get_stats()['pool_available'], 2)
                            self.assertEqual(pool.get_stats()['pool_size'], 2)
                            measurements.append({'sleep_disappeared_seconds': vanished, 'slots_released_seconds': released})
                finally:
                    runtime.repository = None
                    await runtime.close()
            asyncio.run(check())
        print('ARCHIVE_CANCELLATION_LATENCY=' + json.dumps(measurements))
