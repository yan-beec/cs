"""真实事务快照、低序号迟提交与批次恢复。"""
import json
from unittest.mock import patch
import psycopg
from psycopg.rows import dict_row
from quality.tests.test_handoff_replay_database import archive, insert_queue, pytestmark
from quality.tests.test_processing import event
from quality.processing.rebuild_batch import generate_plan, execute_batch, read_manifest, SQL_PATH
from quality.processing import rebuild_batch as batch
from quality.processing.repository import scope_lock
import pytest

def test_snapshot_excludes_late_commit_below_cutoff_and_normal_job_handles_it(archive, tmp_path):
    archive.receive(event(1, content="转人工"))
    archive.drain()
    kwargs = {**archive.settings.connection_kwargs, "user": "archive_admin",
        "password": __import__("os").environ["PROCESSING_TEST_ADMIN_PASSWORD"], "row_factory": dict_row}
    with psycopg.connect(**kwargs) as pending:
        late_id = insert_queue(archive, pending)
        high_id = insert_queue(archive, trigger=999, conversation=13)
        assert late_id < high_id
        original = type(archive.processor.pool).connection
        from contextlib import contextmanager
        single_scan = []
        @contextmanager
        def snapshot_connection(pool, *args, **kwargs):
            with original(pool, *args, **kwargs) as connection:
                actual = connection.execute
                def execute(query, params=None, **options):
                    result = actual(query, params, **options)
                    if "COALESCE(max(id),0)" in str(query):
                        pending.commit()
                    return result
                with patch.object(connection, "execute", side_effect=execute):
                    yield connection
                    single_scan.extend(actual(SQL_PATH.read_text(encoding="utf-8"), dict(lower_id=0,
                        upper_id=high_id, source_key=archive.source, account_id=1)).fetchall())
        with patch.object(type(archive.processor.pool), "connection", snapshot_connection):
            metadata = generate_plan(archive.processor, archive.source, 1, tmp_path / "late", 1000)
        rows = read_manifest(tmp_path / "late/candidates.csv")
        assert metadata["cutoff_event_id"] >= high_id
        assert len(rows) == 1 and int(rows[0]["last_evidence_id"]) == high_id and rows[0]["conversation_id"] == "13"
        assert [(r["source_key"], int(r["account_id"]), int(r["conversation_id"]), int(r["last_evidence_id"])) for r in rows] == [
            (r["source_key"], r["account_id"], r["conversation_id"], r["last_evidence_id"]) for r in single_scan]
        archive.drain()
        assert archive.rows("ops.round_index")[0]["reception_type"] == "ai_only"
        assert archive.admin.execute("SELECT status FROM ops.event_jobs WHERE event_id=%s", (late_id,)).fetchone()["status"] == "completed"

def test_plan_batch_execute_and_resume_identity_set(archive, tmp_path):
    archive.receive(event(1, content="转人工"))
    insert_queue(archive)
    directory = tmp_path / "batch"
    generate_plan(archive.processor, archive.source, 1, directory, 1000)
    manifest, status = directory / "candidates.csv", directory / "status.csv"
    assert execute_batch(archive.processor, manifest, status) == 0
    assert execute_batch(archive.processor, manifest, status, resume=True) == 0
    import csv
    states = list(csv.DictReader(status.open(encoding="utf-8")))
    assert len(states) == 1 and states[0]["status"] == "completed" and states[0]["attempts"] == "1"

@pytest.mark.parametrize("chunk_size", [1000, 2000, 200000])
def test_chunked_equals_single_scan(archive, tmp_path, chunk_size):
    first = insert_queue(archive)
    insert_queue(archive, trigger=2)
    insert_queue(archive, conversation=13)
    # 同一会话铺过多个真实主键分段，不修改共享序列或借相邻三行冒充跨段覆盖。
    inserted = archive.admin.execute("""INSERT INTO archive.events(source_key,account_id,event_key,event_type,
        ingestion_kind,conversation_id,raw_body,payload)
        SELECT source_key,account_id,event_key || ':' || n,event_type,ingestion_kind,
               conversation_id,raw_body,payload
        FROM archive.events CROSS JOIN generate_series(1,2501) n WHERE id=%s RETURNING id""", (first,)).fetchall()
    if chunk_size <= 2000:
        assert len({(row["id"] - 1) // chunk_size for row in inserted}) >= 2
    directory = tmp_path / str(chunk_size)
    single = []
    original = type(archive.processor.pool).connection
    from contextlib import contextmanager
    @contextmanager
    def compared_snapshot(pool, *args, **kwargs):
        with original(pool, *args, **kwargs) as connection:
            yield connection
            assert connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
            assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
            cutoff = connection.execute("SELECT max(id) AS id FROM archive.events").fetchone()["id"]
            single.extend(connection.execute(SQL_PATH.read_text(encoding="utf-8"), dict(lower_id=0,
                upper_id=cutoff, source_key=archive.source, account_id=1)).fetchall())
    with patch.object(type(archive.processor.pool), "connection", compared_snapshot):
        generate_plan(archive.processor, archive.source, 1, directory, chunk_size)
    actual = read_manifest(directory / "candidates.csv")
    assert sorted((r["conversation_id"], r["last_evidence_id"], r["evidence_types"]) for r in single) == [
        (int(r["conversation_id"]), int(r["last_evidence_id"]), r["evidence_types"].split(";")) for r in actual]

def test_plan_chunk_failure_writes_nothing(archive, tmp_path):
    original = type(archive.processor.pool).connection
    from contextlib import contextmanager
    @contextmanager
    def failing(pool, *args, **kwargs):
        with original(pool, *args, **kwargs) as connection:
            actual = connection.execute
            def execute(query, params=None, **options):
                if "array_agg(DISTINCT" in str(query): raise RuntimeError("故障注入")
                return actual(query, params, **options)
            with patch.object(connection, "execute", side_effect=execute): yield connection
    directory = tmp_path / "broken"
    with patch.object(type(archive.processor.pool), "connection", failing), pytest.raises(RuntimeError):
        generate_plan(archive.processor, archive.source, 1, directory, 1000)
    assert not (directory / "candidates.csv").exists() and not (directory / "batch.json").exists()

def test_mixed_failed_busy_completed_then_resume(archive, tmp_path):
    archive.receive(event(1), event(2, conversation_id=13), event(3, conversation_id=14))
    for conversation in (12, 13, 14): insert_queue(archive, conversation=conversation)
    directory = tmp_path / "mixed"
    generate_plan(archive.processor, archive.source, 1, directory, 1000)
    original = archive.processor.replay.step
    def failing(connection, scope, *args, **kwargs):
        if scope[2] == 14: raise RuntimeError("单会话失败")
        return original(connection, scope, *args, **kwargs)
    with archive.admin.transaction():
        archive.admin.execute("SELECT pg_advisory_xact_lock(%s)", (scope_lock(archive.source, 1, 13),))
        with patch.object(archive.processor.replay, "step", side_effect=failing):
            assert execute_batch(archive.processor, directory / "candidates.csv", directory / "status.csv") == 1
    statuses = batch.read_csv(directory / "status.csv", batch.STATUS_FIELDS)
    assert [r["status"] for r in statuses] == ["completed", "skipped_busy", "failed"]
    assert execute_batch(archive.processor, directory / "candidates.csv", directory / "status.csv", True) == 0
    statuses = batch.read_csv(directory / "status.csv", batch.STATUS_FIELDS)
    assert [r["status"] for r in statuses] == ["completed"] * 3
    assert {batch.identity(r) for r in statuses} == {batch.identity(r) for r in read_manifest(directory / "candidates.csv")}

@pytest.mark.parametrize("error_type", [psycopg.errors.AdminShutdown, psycopg.errors.CrashShutdown, psycopg.errors.CannotConnectNow])
def test_database_shutdown_stops_before_next_scope(archive, error_type):
    archive.receive(event(1), event(2, conversation_id=13))
    calls = []
    def shutdown(connection, scope, *args, **kwargs):
        calls.append(scope)
        raise error_type("不保存数据库原始错误文本")
    with patch.object(archive.processor.replay, "step", side_effect=shutdown), pytest.raises(error_type):
        archive.processor.rebuild(archive.source)
    assert calls == [(archive.source, 1, 12)]
