"""在独立数据库验证证据迟到、窗口重放及事实刷新。"""
import json
import os
import uuid
import pytest
from psycopg.types.json import Jsonb
from quality.tests import test_query_database as query_helpers
from quality.tests.test_processing import event
from quality.tests.test_handoff_decision_projection import queued
from quality.processing.repository import scope_lock
from quality.processing.service_facts import FACT_VERSION
from quality.processing.rollups import RollupBuilder

pytestmark = pytest.mark.skipif(os.environ.get("PROCESSING_TEST_DATABASE") != "1", reason="需要独立数据库")

@pytest.fixture
def archive():
    query_helpers.QueryDatabaseTest.setUpClass()
    case = query_helpers.QueryDatabaseTest()
    case.setUp()
    def drain_scope():
        # 共享隔离库可能保留其他用例作业，只租用本用例来源，随后走正式作业处理入口。
        for _ in range(100):
            row = case.admin.execute("""UPDATE ops.event_jobs j SET status='processing',locked_by='b2-test',
                locked_until=CURRENT_TIMESTAMP+interval '120 seconds',attempts=attempts+1
                WHERE event_id=(SELECT j.event_id FROM ops.event_jobs j JOIN archive.events e ON e.id=j.event_id
                  WHERE e.source_key=%s AND j.status IN ('queued','retry') ORDER BY j.event_id LIMIT 1)
                RETURNING event_id,locked_by,attempts""", (case.source,)).fetchone()
            if row is None:
                return
            case.processor.process_claim(row)
        raise AssertionError("本来源作业未收敛")
    case.drain = drain_scope
    try:
        yield case
    finally:
        case.doCleanups()
        query_helpers.QueryDatabaseTest.tearDownClass()

def insert_queue(case, connection=None, trigger=1, conversation=12):
    connection = connection or case.admin
    payload = queued(trigger=trigger)["payload"]
    payload["conversation"]["id"] = conversation
    row = connection.execute("""INSERT INTO archive.events(source_key,account_id,event_key,event_type,
        ingestion_kind,conversation_id,raw_body,payload) VALUES(%s,1,%s,'ics_handoff_queue_decided',
        'backfill',%s,%s,%s) RETURNING id""", (case.source, "queue:"+uuid.uuid4().hex, conversation,
        json.dumps(payload).encode(), Jsonb(payload))).fetchone()
    connection.execute("INSERT INTO ops.event_jobs(event_id) VALUES(%s)", (row["id"],))
    return row["id"]

def test_late_queue_refreshes_current_version_facts_and_preserves_other_scope(archive):
    archive.receive(event(1, content="转人工"), event(2, 400, conversation_id=13))
    archive.drain()
    original = archive.rows("ops.round_index")
    day = original[0]["started_at"].date()
    builder = RollupBuilder(archive.processor.pool)
    builder.build(day)
    untouched = next(row for row in original if row["conversation_id"] == 13)
    insert_queue(archive)
    archive.drain()
    changed = next(row for row in archive.rows("ops.round_index") if row["conversation_id"] == 12)
    assert changed["reception_type"] == "ai_only"
    fact = archive.admin.execute("SELECT * FROM ops.round_service_facts WHERE round_id=%s", (changed["round_id"],)).fetchone()
    assert fact["fact_version"] == FACT_VERSION and not fact["is_lost"] and not fact["is_handoff"]
    assert next(row for row in archive.rows("ops.round_index") if row["conversation_id"] == 13) == untouched
    assert archive.admin.execute("SELECT count(*) AS n FROM ops.stat_dirty_days").fetchone()["n"] > 0
    builder.build(day)
    assert archive.admin.execute("SELECT count(*) AS n FROM ops.stat_dirty_days WHERE day=%s", (day,)).fetchone()["n"] == 0
    report = archive.processor.rebuild(archive.source, 1, 12)
    assert report.successful and len(report.completed) == 1
    rebuilt = next(row for row in archive.rows("ops.round_index") if row["conversation_id"] == 12)
    assert rebuilt["data"] == changed["data"]

def test_assignment_without_notice_refreshes_classification(archive):
    archive.receive(event(1, attachments=[{"id": 1, "file_type": "image"}]))
    archive.drain()
    archive.receive_assignment()
    archive.drain()
    row = archive.rows("ops.round_index")[0]
    assert row["reception_type"] == "human" and row["data"]["handoff_result"] == "completed"
    assert not row["data"].get("has_agent_reply")

def test_rebuild_reports_busy_and_missing_scope(archive):
    archive.receive(event(1))
    with archive.admin.transaction():
        archive.admin.execute("SELECT pg_advisory_xact_lock(%s)", (scope_lock(archive.source, 1, 12),))
        report = archive.processor.rebuild(archive.source, 1, 12)
        assert len(report.skipped) == 1 and report.skipped[0]["status"] == "skipped_busy"
    assert archive.processor.rebuild(archive.source, 1, 99999).failed[0]["error_code"] == "NO_EVENTS"
