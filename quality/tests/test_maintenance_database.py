"""仅在显式配置的可丢弃测试数据库上验证维护取消和事务回滚。"""

from datetime import date, datetime, timezone
import os
import time
import unittest
from unittest.mock import patch

from quality.processing.rollups import RollupBuilder
from quality.tests import test_query_database as fixture


@unittest.skipUnless(os.environ.get('PROCESSING_TEST_DATABASE') == '1', '需要显式配置隔离测试数据库')
class MaintenanceDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)

    def test_slow_sql_rolls_back_and_preserves_building_day(self):
        day = date(2026, 10, 1)
        builder = RollupBuilder(self.processor.pool, build_budget_seconds=.3)
        self.admin.execute('DELETE FROM ops.stat_rollup_days WHERE day=%s', (day,))

        def slow_source(connection, *args, **kwargs):
            connection.execute('SELECT pg_sleep(3)')
            self.fail('长 SQL 应在预算到期时被取消')

        started = time.monotonic()
        with patch.object(builder, 'next_day', return_value=day), patch(
                'quality.processing.rollups.FactSource.load', side_effect=slow_source):
            with self.assertLogs('quality.processing.rollups', level='WARNING'):
                self.assertTrue(builder.step(datetime.now(timezone.utc)))
        self.assertLess(time.monotonic() - started, 2)
        row = self.admin.execute('SELECT state,built_at FROM ops.stat_rollup_days WHERE day=%s', (day,)).fetchone()
        self.assertEqual(row['state'], 'building')
        self.assertIsNone(row['built_at'])
        self.assertEqual(builder.failures[day][0], 1)
        with self.processor.pool.connection() as connection:
            self.assertEqual(connection.execute('SELECT 1 AS value').fetchone()['value'], 1)
