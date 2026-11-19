"""仅在显式一次性数据库内验证排队证据原子性。"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
import psycopg
from ics_agent.app.handoff_queue.repository import HandoffQueueRepository
from quality.tests.test_assignment_database import AssignmentDatabaseTest
from quality.tests.test_handoff_decision_sync import evidence

class QueueEvidenceDatabaseTest(AssignmentDatabaseTest):
    def setUp(self):
        super().setUp()
        from types import SimpleNamespace
        self.queue=HandoffQueueRepository(SimpleNamespace(postgres_dsn=os.environ['HANDOFF_QUEUE_TEST_DSN']))
        self.queue.open();self.addCleanup(self.queue.close);self.queue.ensure_tables()
        with self.queue.pool.connection() as c:
            c.execute("DELETE FROM ics_handoff_queue_event_outbox WHERE account_id='1' AND conversation_id='12'")
        self.addCleanup(self.clear_queue)

    def clear_queue(self):
        with self.queue.pool.connection() as c:
            c.execute("DELETE FROM ics_handoff_queue_event_outbox WHERE account_id='1' AND conversation_id='12'")

    def test_required_contact_immutable_concurrent_and_cleanup(self):
        with self.assertRaises(psycopg.errors.NotNullViolation): self.queue.save_decision(evidence(contact_id=None))
        with ThreadPoolExecutor(max_workers=2) as executor:
            rows=list(executor.map(lambda i:self.queue.save_decision(evidence(decided_at=evidence()['decided_at']+timedelta(seconds=i))), range(2)))
        self.assertEqual(sum(created for _,created in rows),1)
        self.assertEqual(rows[0][0],rows[1][0])
        self.queue.cleanup(evidence()['decided_at']+timedelta(days=365))
        self.assertEqual(self.queue.saved_decision('1','12','101'),rows[0][0])

    def test_target_commit_ack_retry_and_role(self):
        self.queue.save_decision(evidence())
        with patch.object(self.sync,'acknowledge',side_effect=RuntimeError('ack')):
            with self.assertRaises(RuntimeError): self.sync.sync_once()
        rows=self.admin.execute("SELECT event_type,ingestion_kind,raw_body,id FROM archive.events WHERE source_key=%s",(self.source,)).fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['event_type'],rows[0]['ingestion_kind']),('ics_handoff_queue_decided','backfill'))
        jobs=self.admin.execute('SELECT * FROM ops.event_jobs WHERE event_id=%s',(rows[0]['id'],)).fetchall()
        self.assertEqual(len(jobs),1)
        self.assertEqual(self.sync.sync_once(),1)
        self.assertEqual(self.sync.sync_once(),0)
        with self.sync.target_pool.connection() as c:
            for sql in ('DELETE FROM archive.events WHERE FALSE','UPDATE ops.round_index SET data=data WHERE FALSE'):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with c.transaction(): c.execute(sql)

    def test_target_job_failure_rolls_back_both(self):
        self.queue.save_decision(evidence())
        self.admin.execute('REVOKE INSERT ON ops.event_jobs FROM archive_assignment')
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege): self.sync.sync_once()
            self.assertEqual(self.admin.execute('SELECT count(*) AS n FROM archive.events WHERE source_key=%s',(self.source,)).fetchone()['n'],0)
            self.assertIsNone(self.queue.saved_decision('1','12','101')['delivered_at'])
        finally: self.admin.execute('GRANT INSERT ON ops.event_jobs TO archive_assignment')

    def test_conflict_is_quarantined(self):
        from quality.assignment.queue_evidence import queue_event
        row=evidence();self.queue.save_decision(row)
        self.sync.append({**row,'contact_id':'99'})
        self.assertEqual(self.sync.sync_once(),0)
        self.assertEqual(self.queue.saved_decision('1','12','101')['delivery_error_code'],'evidence_conflict')

    def test_missing_queue_table_does_not_stop_assignments(self):
        self.control.mark_handoff_action(account_id='1',conversation_id='12',action='assignment_applied')
        self.admin.execute('ALTER TABLE public.ics_handoff_queue_event_outbox RENAME TO b1_hidden_queue_outbox')
        try:
            self.assertEqual(self.sync.sync_once(),1)
            self.assertEqual(self.sync.pending(),[])
        finally: self.admin.execute('ALTER TABLE public.b1_hidden_queue_outbox RENAME TO ics_handoff_queue_event_outbox')
