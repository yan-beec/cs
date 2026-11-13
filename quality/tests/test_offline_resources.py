"""离线留档的资源隔离与服务器端不留存副本。"""
import os
from pathlib import Path
import sys
import time

import pytest

from quality.offline import jobs as jobs_module, worker
from quality.offline.bundle import BundleLimit
from quality.offline.config import OfflineSettings
from quality.offline.jobs import CpuThrottle, OfflineJobs


def make(tmp_path, **values):
    return OfflineJobs(OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0, **values))


class FakeClock:
    def __init__(self):
        self.wall = 0.0
        self.cpu = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.wall += seconds


def test_cpu_throttle_caps_average_usage():
    clock = FakeClock()
    throttle = CpuThrottle(0.5, clock=lambda: clock.wall, cpu=lambda: clock.cpu, sleep=clock.sleep)
    # 纯计算：每 0.1 秒墙钟消耗 0.1 秒 CPU（满一个核）。
    for _ in range(200):
        clock.wall += 0.1
        clock.cpu += 0.1
        throttle()
    assert clock.cpu / clock.wall <= 0.5 + 0.05
    assert max(clock.sleeps) <= 1.0  # 单次睡眠有上限，取消可以及时响应


def test_cpu_throttle_disabled_and_idle_time_does_not_build_burst_credit():
    clock = FakeClock()
    CpuThrottle(0, clock=lambda: clock.wall, cpu=lambda: clock.cpu, sleep=clock.sleep)()
    throttle = CpuThrottle(0.5, clock=lambda: clock.wall, cpu=lambda: clock.cpu, sleep=clock.sleep)
    clock.wall += 60  # 长时间等待网络，几乎不耗 CPU
    throttle()
    assert not clock.sleeps
    clock.wall += 1
    clock.cpu += 1
    throttle()
    assert clock.sleeps  # 空闲后的突发计算仍然被限速


def child_script(body):
    return [sys.executable, "-c", "import json,sys,time,pathlib\nwork=pathlib.Path(sys.argv[1])\n" + body]


def use_child(monkeypatch, jobs, body):
    monkeypatch.setattr(jobs, "_child_command", lambda job_id: child_script(body) + [str(jobs.root / job_id)])


def test_generation_runs_in_separate_process_and_publishes_artifact(tmp_path, monkeypatch):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    use_child(monkeypatch, jobs, """
work.mkdir(parents=True, exist_ok=True)
(work/'archive.html').write_text('<!doctype html>'+str(__import__('os').getpid()))
(work/'result.json').write_text(json.dumps({'status':'completed','artifact':'archive.html'}))
""")
    assert jobs.run_once()
    state = jobs.get(job["id"])
    assert state["status"] == "completed"
    html = jobs.artifact(job["id"]).read_text()
    assert html.startswith("<!doctype html>") and html != "<!doctype html>" + str(os.getpid())
    assert not (tmp_path / job["id"] / "result.json").exists()


def test_child_limit_and_crash_are_reported_without_leaving_files(tmp_path, monkeypatch):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    use_child(monkeypatch, jobs, """
work.mkdir(parents=True, exist_ok=True)
(work/'partial').write_bytes(b'x')
(work/'result.json').write_text(json.dumps({'status':'limit','message':'超出内存上限'}))
""")
    jobs.run_once()
    state = jobs.get(job["id"])
    assert state["status"] == "failed" and state["error"] == "超出内存上限"
    assert not (tmp_path / job["id"]).exists()
    job = jobs.enqueue({"mode": "all"})
    use_child(monkeypatch, jobs, "sys.exit(7)")
    jobs.run_once()
    assert jobs.get(job["id"])["status"] == "failed"


def test_cancel_or_shutdown_terminates_a_stuck_child(tmp_path, monkeypatch):
    jobs = make(tmp_path)
    monkeypatch.setattr(jobs_module, "CANCEL_GRACE_SECONDS", 0.5)
    job = jobs.enqueue({"mode": "all"})
    use_child(monkeypatch, jobs, "work.mkdir(parents=True, exist_ok=True)\ntime.sleep(60)")
    started = time.monotonic()
    import threading
    threading.Timer(0.5, lambda: jobs.cancel(job["id"])).start()
    jobs.run_once()
    assert time.monotonic() - started < 30
    assert jobs.get(job["id"])["status"] == "cancelled"
    assert not (tmp_path / job["id"]).exists()

    job = jobs.enqueue({"mode": "all"})
    threading.Timer(0.5, jobs.stopping.set).start()
    jobs.run_once()
    # 服务停机：任务回到队列，重启后重新生成。
    assert jobs.get(job["id"])["status"] == "queued"


def test_worker_entry_writes_result_for_parent(tmp_path):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    jobs._claim()
    seen = []
    def builder(request, work, progress, check):
        check()
        progress(stage="media", messages=1)
        seen.append(request["mode"])
        work.mkdir(parents=True, exist_ok=True)
        path = work / "archive.html"
        path.write_text("ok")
        return path
    code = worker.main([str(tmp_path), job["id"], str(os.getppid())], builder=builder, apply_limits=False)
    assert code == 0 and seen == ["all"]
    assert '"completed"' in (tmp_path / job["id"] / "result.json").read_text()

    def limited(*args):
        raise BundleLimit("容量超限")
    worker.main([str(tmp_path), job["id"], str(os.getppid())], builder=limited, apply_limits=False)
    assert "容量超限" in (tmp_path / job["id"] / "result.json").read_text(encoding="utf-8")


def test_worker_stops_when_parent_changes(tmp_path):
    jobs = make(tmp_path)
    job = jobs.enqueue({"mode": "all"})
    jobs._claim()
    def builder(request, work, progress, check):
        time.sleep(0.3)
        check()
        raise AssertionError("should have been cancelled")
    worker.main([str(tmp_path), job["id"], str(os.getppid() + 100000)], builder=builder, apply_limits=False)
    assert '"cancelled"' in (tmp_path / job["id"] / "result.json").read_text()


def finished(jobs):
    job = jobs.enqueue({"mode": "all"})
    def builder(request, work, progress, check):
        work.mkdir(parents=True)
        path = work / "archive.html"
        path.write_text("ok")
        return path
    jobs.run_once(builder)
    return job


def test_delete_record_removes_files_but_not_running_jobs(tmp_path):
    jobs = make(tmp_path, isolated_process=False)
    job = finished(jobs)
    assert (tmp_path / job["id"]).exists()
    assert jobs.delete(job["id"]) is True
    assert jobs.get(job["id"]) is None and not (tmp_path / job["id"]).exists()
    assert jobs.delete(job["id"]) is None  # 已删除 / 不存在
    assert jobs.delete("../escape") is None
    queued = jobs.enqueue({"mode": "all"})
    with pytest.raises(ValueError, match="先取消"):
        jobs.delete(queued["id"])
    assert jobs.get(queued["id"])["status"] == "queued"
    jobs.cancel(queued["id"])
    assert jobs.delete(queued["id"]) is True


def test_consume_keeps_server_copy_for_downloaded_hours(tmp_path, monkeypatch):
    jobs = make(tmp_path, isolated_process=False, downloaded_hours=2, unclaimed_hours=24)
    job = finished(jobs)
    jobs.consume(job["id"])
    state = jobs.get(job["id"])
    assert state["status"] == "downloaded" and state["available"]
    assert (tmp_path / job["id"]).exists()
    assert jobs.ticket(job["id"])  # 保留期内可重新下载
    jobs.consume(job["id"])  # 重复下载不顺延
    assert jobs.get(job["id"])["updated"] == state["updated"]
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 3600)
    jobs.cleanup()
    assert (tmp_path / job["id"]).exists()
    monkeypatch.setattr(time, "time", lambda: now + 2 * 3600 + 1)
    assert not jobs.get(job["id"])["available"]
    with pytest.raises(ValueError, match="已删除"):
        jobs.ticket(job["id"])
    jobs.cleanup()
    assert not (tmp_path / job["id"]).exists()
    assert jobs.get(job["id"])["status"] == "downloaded"


def test_unclaimed_files_and_orphan_directories_are_removed(tmp_path, monkeypatch):
    jobs = make(tmp_path, isolated_process=False, unclaimed_hours=1, retention_days=1)
    job = finished(jobs)
    orphan = tmp_path / ("b" * 32)
    orphan.mkdir()
    (orphan / "archive.html").write_text("left over")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    jobs.cleanup()
    assert (tmp_path / job["id"]).exists()  # 未到时间的待下载文件保留
    assert not orphan.exists()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 3601)
    jobs.cleanup()
    assert jobs.get(job["id"])["status"] == "expired" and not (tmp_path / job["id"]).exists()
    monkeypatch.setattr(time, "time", lambda: now + 3 * 86400)
    jobs.cleanup()
    assert jobs.get(job["id"]) is None  # 任务记录超过 RETENTION_DAYS 后删除
    assert (tmp_path / "jobs.sqlite3").exists()


def test_child_command_lowers_io_priority_without_failing(tmp_path):
    jobs = make(tmp_path)
    command = jobs._child_command("a" * 32)
    assert command[-6:] == [sys.executable, "-m", "quality.offline.worker", str(jobs.root), "a" * 32, str(os.getpid())]
    if len(command) > 6:
        # ionice 的 -t：容器不允许调整 IO 优先级时照常运行，不让任务失败。
        assert command[1:6] == ["-t", "-c", "2", "-n", "7"]
