"""一致性候选清单、逐会话检查点与可恢复串行重建。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import io
import json
import logging
import os
import time
import uuid

from .service_facts import FACT_VERSION
from .rollups import ROLLUP_VERSION

MANIFEST_FIELDS = "source_key,account_id,conversation_id,evidence_types,last_evidence_id,cutoff_event_id".split(",")
STATUS_FIELDS = "source_key,account_id,conversation_id,status,attempts,error_code,error_message,started_at,finished_at".split(",")
STATES = {"pending", "running", "completed", "skipped_busy", "failed", "interrupted"}
SQL_PATH = Path(__file__).parent / "sql/handoff_rebuild_candidates.sql"

def now():
    return datetime.now(timezone.utc).isoformat()

def rules_fingerprint(settings, bindings):
    """按显式白名单计算实际源码和投影配置，跨平台规范化换行。"""
    root = Path(__file__).resolve().parents[2]
    paths = ["quality/processing/" + name for name in (
        "projector.py", "windows.py", "replay.py", "repository.py", "rebuild_batch.py",
        "worker.py", "service_facts.py", "rollups.py", "config.py", "sql/handoff_rebuild_candidates.sql")]
    paths += ["ics_agent/app/chatwoot/payload.py"]
    paths += ["quality/query/service_metrics.py", "quality/processing/stat_metrics.py", "quality/processing/budget.py"]
    digest = hashlib.sha256()
    for name in sorted(paths):
        content = (root / name).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(name.encode() + b"\0" + str(len(content)).encode() + b"\0" + content)
    config = {"idle_timeout_seconds": settings.idle_timeout_seconds,
        "channel_bindings": sorted([list(key) + [value] for key, value in bindings.items()]),
        "fact_version": FACT_VERSION, "rollup_version": ROLLUP_VERSION}
    digest.update(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()

def csv_bytes(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")

def atomic_write(path, data):
    """写入并刷新同目录临时文件，替换失败不继续下一会话。"""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

@contextmanager
def batch_lock(directory):
    """使用内核文件锁，进程崩溃后可自动恢复，不删除其他进程的锁。"""
    path = Path(directory) / ".batch.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Batch directory is busy") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)

def identity(row):
    source = row["source_key"]
    if not source or source != source.strip():
        raise ValueError("Invalid source key")
    for key in ("account_id", "conversation_id"):
        value = str(row[key])
        if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
            raise ValueError("Invalid scope identity")
    return source, int(row["account_id"]), int(row["conversation_id"])

def read_csv(path, fields):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != fields:
            raise ValueError("Invalid CSV header")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("Invalid CSV row")
    return rows

def read_manifest(path):
    rows = read_csv(path, MANIFEST_FIELDS)
    seen, scopes, cutoffs = set(), set(), set()
    for row in rows:
        key = identity(row)
        if key in seen:
            raise ValueError("Duplicate manifest identity")
        seen.add(key)
        scopes.add(key[:2])
        for field in ("last_evidence_id", "cutoff_event_id"):
            if not row[field].isascii() or not row[field].isdigit():
                raise ValueError("Invalid evidence ID")
        if not 0 < int(row["last_evidence_id"]) <= int(row["cutoff_event_id"]):
            raise ValueError("Invalid evidence cutoff")
        cutoffs.add(row["cutoff_event_id"])
        types = row["evidence_types"].split(";")
        if not types or len(types) != len(set(types)) or set(types) - {"ics_assignment_recorded", "ics_handoff_queue_decided"}:
            raise ValueError("Invalid evidence types")
    if len(scopes) > 1 or len(cutoffs) > 1:
        raise ValueError("Manifest contains mixed scopes or cutoffs")
    return rows

def generate_plan(repository, source_key, account_id, output_dir, chunk_size=200000, *, stopped=None):
    if (not source_key or source_key != source_key.strip() or type(account_id) is not int
            or not 0 < account_id < 2**63 or not 1000 <= chunk_size <= 2000000):
        raise ValueError("Invalid plan scope or chunk size")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with batch_lock(directory):
        if any((directory / name).exists() for name in ("candidates.csv", "batch.json", "status.csv")):
            raise ValueError("Batch already exists")
        started, found, chunks = time.monotonic(), {}, 0
        with repository.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cutoff = connection.execute("SELECT COALESCE(max(id),0) AS cutoff_event_id FROM archive.events").fetchone()["cutoff_event_id"]
            query = SQL_PATH.read_text(encoding="utf-8")
            for lower in range(0, cutoff, chunk_size):
                if stopped is not None and stopped.is_set():
                    raise KeyboardInterrupt
                upper = min(lower + chunk_size, cutoff)
                try:
                    rows = connection.execute(query, dict(lower_id=lower, upper_id=upper,
                        source_key=source_key, account_id=account_id)).fetchall()
                except Exception as error:
                    logging.error("Plan failed range=(%s,%s] error=%s", lower, upper, type(error).__name__)
                    raise
                for row in rows:
                    key = identity(row)
                    target = found.setdefault(key, dict(source_key=key[0], account_id=key[1], conversation_id=key[2],
                        evidence_types=set(), last_evidence_id=0, cutoff_event_id=cutoff))
                    target["evidence_types"].update(row["evidence_types"])
                    target["last_evidence_id"] = max(target["last_evidence_id"], row["last_evidence_id"])
                chunks += 1
                logging.info("Plan scanned=%s cutoff=%s candidates=%s", upper, cutoff, len(found))
            if stopped is not None and stopped.is_set():
                raise KeyboardInterrupt
        manifest = []
        for key in sorted(found):
            row = found[key]
            row["evidence_types"] = ";".join(sorted(row["evidence_types"]))
            manifest.append(row)
        data = csv_bytes(MANIFEST_FIELDS, manifest)
        if stopped is not None and stopped.is_set():
            raise KeyboardInterrupt
        metadata = dict(batch_id=directory.name, generated_at=now(), source_key=source_key, account_id=account_id,
            cutoff_event_id=cutoff, candidate_count=len(manifest), manifest_sha256=hashlib.sha256(data).hexdigest(),
            code_version=rules_fingerprint(repository.settings, repository.channel_bindings),
            fact_version=FACT_VERSION, rollup_version=ROLLUP_VERSION, chunk_size=chunk_size,
            chunks=chunks, elapsed_seconds=time.monotonic()-started)
        atomic_write(directory / "batch.json", json.dumps(metadata, indent=2).encode())
        try:
            atomic_write(directory / "candidates.csv", data)
        except BaseException:
            (directory / "batch.json").unlink(missing_ok=True)
            raise
        return metadata

def execute_batch(repository, manifest_path, result_path, resume=False, stopped=None):
    manifest_path, result_path = Path(manifest_path).resolve(), Path(result_path).resolve()
    if manifest_path.parent != result_path.parent or result_path.name in {"candidates.csv", "batch.json"}:
        raise ValueError("Result must be a separate file in the batch directory")
    with batch_lock(manifest_path.parent):
        manifest = read_manifest(manifest_path)
        metadata = json.loads((manifest_path.parent / "batch.json").read_text(encoding="utf-8"))
        if (metadata["manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                or metadata["code_version"] != rules_fingerprint(repository.settings, repository.channel_bindings)
                or metadata["fact_version"] != FACT_VERSION or metadata["rollup_version"] != ROLLUP_VERSION
                or metadata["candidate_count"] != len(manifest)):
            raise ValueError("Manifest or rules changed")
        targets = {identity(row): row for row in manifest}
        if any(key[:2] != (metadata["source_key"], metadata["account_id"]) or
               int(row["cutoff_event_id"]) != metadata["cutoff_event_id"] for key, row in targets.items()):
            raise ValueError("Manifest metadata mismatch")
        if result_path.exists() and not resume:
            raise ValueError("Existing status requires --resume")
        statuses = {}
        if result_path.exists():
            for row in read_csv(result_path, STATUS_FIELDS):
                key = identity(row)
                attempts = row["attempts"]
                if (key not in targets or key in statuses or row["status"] not in STATES
                        or not attempts.isascii() or not attempts.isdecimal() or len(attempts) > 18):
                    raise ValueError("Invalid status identity or state")
                row["attempts"] = int(attempts)
                statuses[key] = row
        for key in targets:
            statuses.setdefault(key, dict(zip(STATUS_FIELDS, [*key, "pending", 0, "", "", "", ""])))
        def checkpoint():
            atomic_write(result_path, csv_bytes(STATUS_FIELDS, [statuses[key] for key in sorted(statuses)]))
        checkpoint()
        interrupted = False
        for key in sorted(targets):
            row = statuses[key]
            if row["status"] == "completed":
                continue
            if stopped is not None and stopped.is_set():
                interrupted = True
                break
            row.update(status="running", attempts=int(row["attempts"])+1, started_at=now(), finished_at="", error_code="", error_message="")
            checkpoint()
            try:
                report = repository.rebuild(*key, stopped=stopped) if stopped is not None else repository.rebuild(*key)
                results = report.completed + report.skipped + report.failed
                if len(results) != 1 or identity(results[0]) != key:
                    raise RuntimeError("Invalid rebuild result")
                row.update({field: results[0].get(field, "") for field in ("status", "error_code", "error_message")})
            except KeyboardInterrupt:
                row.update(status="interrupted", error_code="INTERRUPTED")
                interrupted = True
            except Exception as error:
                row.update(status="failed", error_code=type(error).__name__, error_message=type(error).__name__, finished_at=now())
                checkpoint()
                raise
            row["finished_at"] = now()
            checkpoint()
            if interrupted:
                break
        counts = {state: sum(row["status"] == state for row in statuses.values()) for state in sorted(STATES)}
        interrupted = interrupted or (stopped is not None and stopped.is_set())
        logging.info("Batch states=%s result=%s", counts, result_path)
        return 130 if interrupted else (0 if all(row["status"] == "completed" for row in statuses.values()) else 1)
