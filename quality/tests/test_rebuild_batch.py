"""批次完整性和恢复边界。"""
from quality.processing.rebuild_batch import rules_fingerprint, read_manifest
from quality.processing.config import ProcessingSettings
import pytest
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from quality.processing import rebuild_batch as batch
from quality.processing.repository import RebuildReport

@pytest.fixture
def planned(tmp_path):
    settings = ProcessingSettings(_env_file=None, ingest_password="test-only")
    repository = SimpleNamespace(settings=settings, channel_bindings={})
    rows = [dict(source_key="s", account_id=1, conversation_id=i, evidence_types="ics_handoff_queue_decided",
        last_evidence_id=i, cutoff_event_id=100) for i in (12, 13)]
    data = batch.csv_bytes(batch.MANIFEST_FIELDS, rows)
    (tmp_path / "candidates.csv").write_bytes(data)
    (tmp_path / "batch.json").write_text(json.dumps(dict(manifest_sha256=__import__("hashlib").sha256(data).hexdigest(),
        code_version=batch.rules_fingerprint(settings, {}), fact_version=batch.FACT_VERSION,
        rollup_version=batch.ROLLUP_VERSION, candidate_count=2, source_key="s", account_id=1, cutoff_event_id=100)))
    def rebuild(source, account, conversation):
        return RebuildReport(completed=[dict(source_key=source, account_id=account, conversation_id=conversation, status="completed")])
    repository.rebuild = rebuild
    return repository, tmp_path / "candidates.csv", tmp_path / "status.csv"

def test_resume_retries_only_unfinished_and_preserves_attempts(planned):
    repository, manifest, status = planned
    original = repository.rebuild
    def rebuild(source, account, conversation):
        if conversation == 13:
            return RebuildReport(skipped=[dict(source_key=source, account_id=account, conversation_id=conversation, status="skipped_busy")])
        return original(source, account, conversation)
    repository.rebuild = rebuild
    assert batch.execute_batch(repository, manifest, status) == 1
    calls = []
    def recovered(*scope):
        calls.append(scope)
        return original(*scope)
    repository.rebuild = recovered
    assert batch.execute_batch(repository, manifest, status, True) == 0
    assert calls == [("s", 1, 13)]
    rows = batch.read_csv(status, batch.STATUS_FIELDS)
    assert [r["attempts"] for r in rows] == ["1", "2"]

def test_checkpoint_failure_stops_before_next_scope(planned):
    repository, manifest, status = planned
    calls = []
    original, write = repository.rebuild, batch.atomic_write
    repository.rebuild = lambda *scope: (calls.append(scope), original(*scope))[1]
    counter = 0
    def fail_checkpoint(path, data):
        nonlocal counter
        counter += 1
        if counter == 3: raise OSError("checkpoint failed")
        write(path, data)
    with patch.object(batch, "atomic_write", side_effect=fail_checkpoint), pytest.raises(OSError):
        batch.execute_batch(repository, manifest, status)
    assert calls == [("s", 1, 12)]
    assert batch.read_csv(status, batch.STATUS_FIELDS)[0]["status"] == "running"

@pytest.mark.parametrize("change", ["manifest", "rules", "extra", "duplicate", "state"])
def test_resume_validates_all_inputs_before_writes(planned, change):
    repository, manifest, status = planned
    assert batch.execute_batch(repository, manifest, status) == 0
    if change == "manifest": manifest.write_bytes(manifest.read_bytes() + b"\n")
    elif change == "rules": repository.channel_bindings = {("1", "3"): "changed"}
    else:
        rows = batch.read_csv(status, batch.STATUS_FIELDS)
        if change == "extra": rows[0]["conversation_id"] = "99"
        if change == "duplicate": rows.append(rows[0])
        if change == "state": rows[0]["status"] = "unknown"
        status.write_bytes(batch.csv_bytes(batch.STATUS_FIELDS, rows))
    repository.rebuild = lambda *args: pytest.fail("校验失败前不应重放")
    with pytest.raises(ValueError): batch.execute_batch(repository, manifest, status, True)

def test_missing_status_and_running_are_retried(planned):
    repository, manifest, status = planned
    batch.execute_batch(repository, manifest, status)
    rows = batch.read_csv(status, batch.STATUS_FIELDS)
    rows[0]["status"] = "running"
    status.write_bytes(batch.csv_bytes(batch.STATUS_FIELDS, rows[:1]))
    assert batch.execute_batch(repository, manifest, status, True) == 0
    assert [r["attempts"] for r in batch.read_csv(status, batch.STATUS_FIELDS)] == ["2", "1"]

def test_interrupt_preserves_checkpoint_and_returns_130(planned):
    import threading
    repository, manifest, status = planned
    stopped = threading.Event()
    def rebuild(*scope, stopped=None):
        stopped.set()
        raise KeyboardInterrupt
    repository.rebuild = rebuild
    assert batch.execute_batch(repository, manifest, status, stopped=stopped) == 130
    assert [r["status"] for r in batch.read_csv(status, batch.STATUS_FIELDS)] == ["interrupted", "pending"]

@pytest.mark.parametrize("attempts", ["²", "9" * 10000, "-1", "1.0"])
def test_invalid_late_row_attempts_rejected_before_any_write(planned, attempts):
    repository, manifest, status = planned
    batch.execute_batch(repository, manifest, status)
    rows = batch.read_csv(status, batch.STATUS_FIELDS)
    rows[0]["status"] = "pending"
    rows[1]["attempts"] = attempts
    status.write_bytes(batch.csv_bytes(batch.STATUS_FIELDS, rows))
    before = status.read_bytes()
    repository.rebuild = lambda *args: pytest.fail("非法后续行不应允许先写第一行")
    with pytest.raises(ValueError): batch.execute_batch(repository, manifest, status, True)
    assert status.read_bytes() == before

def test_fingerprint_covers_transitive_rules_but_not_unrelated_files():
    settings = ProcessingSettings(_env_file=None, ingest_password="test-only")
    before = batch.rules_fingerprint(settings, {})
    original = Path.read_bytes
    def changed(path):
        content = original(path)
        return content + b"\nMAX_RESPONSE_SECONDS=42\n" if path.name == "service_metrics.py" else content
    with patch.object(Path, "read_bytes", changed):
        assert before != batch.rules_fingerprint(settings, {})
    def unrelated(path):
        if path.suffix in {".md", ".csv", ".log"}: raise AssertionError("不应读取无关文件")
        return original(path)
    with patch.object(Path, "read_bytes", unrelated):
        assert before == batch.rules_fingerprint(settings, {})

@pytest.mark.parametrize("arguments", [
    ["--rebuild-plan", "--source-key", "s", "--account-id", "-1", "--output-dir", "x"],
    ["--rebuild", "--source-key", "  "], ["--once", "--plan-chunk-size", "1000"],
    ["--rebuild-plan", "--source-key", "s", "--account-id", "1", "--output-dir", "x", "--plan-chunk-size", "999"]])
def test_cli_rejects_invalid_scope_before_database(arguments):
    from quality.processing.worker import main
    with patch("quality.processing.worker.ProcessingRepository", side_effect=AssertionError("不能连接数据库")):
        with pytest.raises(SystemExit) as error: main(arguments)
        assert error.value.code == 2

def test_rules_include_actual_projection_configuration():
    settings = ProcessingSettings(_env_file=None, ingest_password="test-only")
    assert rules_fingerprint(settings, {}) != rules_fingerprint(settings, {("1", "3"): "x"})
    changed = settings.model_copy(update={"idle_timeout_seconds": settings.idle_timeout_seconds + 1})
    assert rules_fingerprint(settings, {}) != rules_fingerprint(changed, {})

def test_manifest_rejects_duplicate_scope(tmp_path):
    path = tmp_path / "candidates.csv"
    path.write_text("source_key,account_id,conversation_id,evidence_types,last_evidence_id,cutoff_event_id\ns,1,12,ics_handoff_queue_decided,1,2\ns,1,12,ics_handoff_queue_decided,1,2\n")
    with pytest.raises(ValueError): read_manifest(path)
