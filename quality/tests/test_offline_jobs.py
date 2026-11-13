import asyncio
import json
import os
from pathlib import Path
import threading
import time
from zipfile import ZipFile

import pytest

from quality.offline.config import OfflineSettings
from quality.offline.jobs import OfflineJobs, Cancelled


def make(tmp_path):
    # 这些用例直接注入 builder，在当前进程同步执行；子进程路径见 test_offline_resources.py。
    return OfflineJobs(OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0,
                                       isolated_process=False))


def test_windows_export_directory_inherits_shared_volume_permissions(tmp_path, monkeypatch):
    root = (tmp_path / "exports").resolve()
    original = Path.mkdir
    modes = []
    def mkdir(path, *args, **kwargs):
        if path == root:
            modes.append(kwargs.get("mode", 0o777))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    jobs = make(root)
    assert jobs.database.is_file()
    assert modes == [0o777 if os.name == "nt" else 0o700]


def test_jobs_survive_restart_and_return_real_downloads(tmp_path):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all", "filters": {}})
    assert job["status"] == "queued"
    # 模拟进程领取任务后、尚未完成时退出。
    jobs._claim()
    restarted = make(tmp_path)
    def builder(request, work, progress, check):
        progress(stage="media", messages=3)
        check()
        work.mkdir(parents=True, exist_ok=True)
        path = work / "archive.zip"
        with ZipFile(path, "w") as z:
            z.writestr("index.html", "离线页面")
        return path
    restarted.recover()
    restarted.run_once(builder)
    state = restarted.get(job["id"])
    assert state["status"] == "completed" and state["progress"]["messages"] == 3
    ticket = restarted.ticket(job["id"])
    with ZipFile(restarted.download_path(job["id"], ticket)) as z:
        assert z.read("index.html").decode() == "离线页面"
    with pytest.raises(ValueError):
        restarted.download_path(job["id"], ticket + "invalid")


def test_cancel_during_generation_cleans_partial_data(tmp_path):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    def builder(request, work, progress, check):
        work.mkdir(parents=True, exist_ok=True)
        (work / "partial").write_bytes(b"incomplete")
        jobs.cancel(job["id"])
        check()
        path = work / "archive.zip"
        path.write_bytes(b"incomplete zip")
        return path
    jobs.run_once(builder)
    assert jobs.get(job["id"])["status"] == "cancelled"
    assert not (tmp_path / job["id"]).exists()


def test_busy_queue_does_not_create_unbounded_disk_work(tmp_path):
    jobs = make(tmp_path)
    jobs.enqueue({"mode": "all"})
    with pytest.raises(ValueError):
        jobs.enqueue({"mode": "all"})
    assert len(jobs.list()) == 1


def test_invalid_ids_do_not_escape_export_directory(tmp_path):
    jobs = make(tmp_path)
    assert jobs.get("../outside") is None
    with pytest.raises(ValueError):
        jobs.download_path("../outside", "no")


def test_background_failures_do_not_leak_source_content(tmp_path):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    def builder(*args):
        raise RuntimeError("private message and signed URL")
    jobs.run_once(builder)
    state = jobs.get(job["id"])
    assert state["status"] == "failed"
    assert "private" not in json.dumps(state)


def test_tickets_expire(tmp_path, monkeypatch):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    def builder(request, work, *args):
        work.mkdir()
        path = work / "archive.zip"
        path.write_bytes(b"zip")
        return path
    jobs.run_once(builder)
    ticket = jobs.ticket(job["id"])
    monkeypatch.setattr(time, "time", lambda: 99999999999)
    with pytest.raises(ValueError):
        jobs.download_path(job["id"], ticket)


def test_background_worker_recovers_and_isolated_thread_completes(tmp_path):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    jobs._claim()
    threads = []
    def builder(request, work, progress, check):
        threads.append(threading.current_thread().name)
        work.mkdir(parents=True)
        path = work / "archive.zip"
        path.write_bytes(b"zip")
        return path
    jobs._build = builder
    async def scenario():
        await jobs.start()
        try:
            for _ in range(100):
                if jobs.get(job["id"])["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert jobs.get(job["id"])["status"] == "completed"
            second = make(tmp_path)
            await second.start()
            assert second.worker_lock is None
            await second.close()
        finally:
            await jobs.close()
    asyncio.run(scenario())
    assert threads and threads[0].startswith("offline-archive")
