"""使用显式测试 DSN 运行真实 PostgreSQL 并发、持久化和清理验证。"""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

@unittest.skipUnless(os.getenv('HANDOFF_QUEUE_TEST_DSN'),'未配置独立排队测试数据库')
class QueueDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ics_agent.app.handoff_queue.repository import HandoffQueueRepository
        cls.repo=HandoffQueueRepository(SimpleNamespace(postgres_dsn=os.environ['HANDOFF_QUEUE_TEST_DSN']))
        cls.repo.open()
        cls.repo.ensure_tables()

    @classmethod
    def tearDownClass(cls): cls.repo.close()

    def setUp(self):
        self.account='queue-test-'+str(uuid.uuid4())
        self.contact='42'
        self.now=datetime(2026,11,13,1,10,tzinfo=timezone.utc)

    def tearDown(self):
        with self.repo.pool.connection() as conn:
            for table in ('ics_handoff_queue_state','ics_handoff_success_counter'):
                conn.execute(f'DELETE FROM {table} WHERE account_id=%s',(self.account,))

    def test_concurrent_success_and_duplicate_id_are_atomic(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:self.repo.record_success(self.account,self.contact,'same',self.now),range(2)))
        self.assertEqual(sum(results),1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda i:self.repo.record_success(self.account,self.contact,f'unique-{i}',self.now),range(8)))
        row=self.repo.get_counter(self.account,self.contact)
        self.assertEqual((len(row['success_times']),row['week_count']),(9,9))

    def test_unique_queue_success_delete_and_week_reset(self):
        state=dict(initial_count=2,entered_at=self.now,seed=1,step_min_s=5,step_max_s=5,config_version=1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rows=list(executor.map(lambda i:self.repo.create_state(self.account,self.contact,{**state,'seed':i}),range(2)))
        self.assertEqual(rows[0]['seed'],rows[1]['seed'])
        self.repo.record_success(self.account,self.contact,'old',self.now-timedelta(minutes=20))
        self.assertIsNone(self.repo.get_state(self.account,self.contact))
        self.repo.record_success(self.account,self.contact,'new',self.now)
        row=self.repo.get_counter(self.account,self.contact)
        self.assertEqual((len(row['success_times']),row['week_count']),(2,1))

    def test_cleanup_preserves_recent_and_redeem_is_durable(self):
        state=dict(initial_count=2,entered_at=self.now-timedelta(seconds=10),seed=1,step_min_s=5,step_max_s=5,config_version=1)
        self.repo.create_state(self.account,self.contact,state)
        self.repo.record_success(self.account,'old','expired',self.now-timedelta(days=2))
        self.repo.record_success(self.account,'recent','kept',self.now-timedelta(hours=2))
        self.repo.cleanup(self.now)
        row=self.repo.get_state(self.account,self.contact)
        self.assertTrue(row['redeem_ready'])
        self.assertIsNone(row['initial_count'])
        self.assertIsNone(self.repo.get_counter(self.account,'old'))
        self.assertIsNotNone(self.repo.get_counter(self.account,'recent'))
        self.repo.ensure_tables()
        self.assertTrue(self.repo.get_state(self.account,self.contact)['redeem_ready'])

    def test_config_optimistic_concurrency_history_and_restart(self):
        from ics_agent.app.handoff_queue.repository import ConfigConflict
        current=self.repo.current_config()
        cfg=deepcopy(current['config'])
        cfg['estimate_per_person_s']=77
        saved=self.repo.save_config(current['version'],cfg,'queue-test')
        with self.assertRaises(ConfigConflict): self.repo.save_config(current['version'],cfg,'stale')
        self.repo.ensure_tables()
        self.assertEqual(self.repo.current_config(),saved)
        history=self.repo.history(1)[0]
        self.assertEqual(history['previous_config'],current['config'])
        self.assertEqual(history['updated_by'],'queue-test')

    def test_delayed_compression_cannot_consume_a_replacement_queue(self):
        import asyncio
        from unittest.mock import patch
        from ics_agent.app.handoff_queue.service import HandoffQueueService
        from ics_agent.app.handoff_queue.policy import DEFAULT_CONFIG
        old = dict(initial_count=2, entered_at=self.now-timedelta(seconds=10), seed=1,
                   step_min_s=5, step_max_s=5, config_version=1)
        self.repo.create_state(self.account, self.contact, old)
        mark_redeem = self.repo.mark_redeem
        def interleaved_compression(account, contact, *expected):
            # 旧排队已成功消耗，新请求进入排队后，才执行迟到的压缩操作。
            self.repo.record_success(account, contact, 'consumed-old', self.now)
            self.repo.create_state(account, contact, {**old, 'entered_at':self.now, 'seed':2})
            return mark_redeem(account, contact, *expected)
        config = {'version':1, 'config':{**deepcopy(DEFAULT_CONFIG), 'enabled':True}}
        with patch.object(self.repo, 'current_config', return_value=config), \
                patch.object(self.repo, 'mark_redeem', side_effect=interleaved_compression):
            asyncio.run(HandoffQueueService(self.repo).evaluate(self.account, self.contact, 'late', self.now))
        state = self.repo.get_state(self.account, self.contact)
        self.assertFalse(state['redeem_ready'])
        self.assertEqual((state['seed'], state['initial_count']), (2, 2))

    def test_delayed_previous_week_success_does_not_reset_new_week(self):
        from ics_agent.app.handoff_queue.policy import week_anchor, success_counts
        anchor = week_anchor(self.now)
        self.repo.record_success(self.account, self.contact, 'new-week', anchor+timedelta(seconds=1))
        self.repo.record_success(self.account, self.contact, 'late-previous-week', anchor-timedelta(seconds=1))
        counter = self.repo.get_counter(self.account, self.contact)
        self.assertEqual(counter['week_anchor'], anchor)
        self.assertEqual(success_counts(counter, self.now), (2, 1))

    def test_ranking_batch_counts_match_queue_at_daily_and_weekly_boundaries(self):
        """真实批量查询与排队单用户读取一致，窗口切换不会依赖清理任务。"""
        from ics_agent.app.handoff_queue.policy import week_anchor, success_counts
        from ics_agent.statistics.repository import report_budget
        anchor = week_anchor(self.now)
        self.repo.record_success(self.account, self.contact, 'day-boundary', anchor-timedelta(days=1))
        self.repo.record_success(self.account, self.contact, 'last-week', anchor-timedelta(seconds=1))
        self.repo.record_success(self.account, 'other', 'new-week', anchor)
        keys = [(self.account, self.contact), (self.account, 'other'), (self.account, 'missing')]
        for now in (anchor-timedelta(microseconds=1), anchor, anchor+timedelta(days=1)):
            snapshot = self.repo.counter_snapshot(keys, now=now, budget=report_budget())
            expected = {key: success_counts(self.repo.get_counter(*key), now) for key in keys}
            self.assertEqual(snapshot['counts'], expected)
            self.assertEqual(snapshot['week_start'], week_anchor(now))
        self.assertEqual(self.repo.counter_snapshot(keys, now=anchor)['counts'][(self.account, self.contact)], (1, 0))
