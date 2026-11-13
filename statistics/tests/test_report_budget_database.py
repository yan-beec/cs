"""真实数据库取消语句并释放查询名额。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest

from psycopg_pool import ConnectionPool

from ics_agent.statistics.repository import StatisticsRepository, report_budget, classify
from quality.query.config import QuerySettings
from quality.query.runtime import QueryRuntime


@unittest.skipUnless(os.environ.get('PROCESSING_TEST_DATABASE') == '1', '需要隔离数据库')
class ReportBudgetDatabaseTest(unittest.TestCase):
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
                    for expected, seconds, statement in [('expired', 1, 8), ('statement', 20, .1), ('aborted', 20, 8)]:
                        abort = threading.Event()
                        budget = report_budget(seconds=seconds, statement_seconds=statement, abort_event=abort)
                        started = threading.Event()

                        def work():
                            with StatisticsRepository(pool)._read(budget) as connection:
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
