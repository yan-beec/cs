"""离线留档子进程入口：python -m quality.offline.worker <留档目录> <任务 ID> <父进程 PID>

由 OfflineJobs 在独立进程中启动，与 ICS 主进程（webhook、后台接口）隔离：
- 调低 CPU 调度优先级（nice），Linux 下由父进程用 ionice 调低磁盘 IO 优先级；
- 按 ARCHIVE_OFFLINE_CPU_LIMIT 限制平均 CPU 占用（默认半个核），宁可导出慢；
- 按 ARCHIVE_OFFLINE_MEMORY_LIMIT_BYTES 限制虚拟内存，超限只让本任务失败；
- 父进程退出时随之结束，不留下孤儿进程。
结果写入任务目录下的 result.json，由父进程读取并更新任务状态。
"""
import json
import logging
import os
import signal
import sys

logger = logging.getLogger("quality.offline.worker")
MEMORY_MESSAGE = "留档生成超出内存上限（ARCHIVE_OFFLINE_MEMORY_LIMIT_BYTES），请缩小导出范围后重试。"


def lower_priority(settings):
    if hasattr(os, "nice") and settings.nice:
        try:
            os.nice(settings.nice)
        except OSError:
            pass
    if settings.memory_limit_bytes and sys.platform.startswith("linux"):
        import resource
        try:
            resource.setrlimit(resource.RLIMIT_AS, (settings.memory_limit_bytes, settings.memory_limit_bytes))
        except (ValueError, OSError):
            logger.warning("Offline archive memory limit not applied")


def die_with_parent(parent):
    """Linux：父进程退出时内核向本进程发送 SIGTERM。

    PDEATHSIG 以创建子进程的线程为准；子进程由常驻的 offline-archive 工作线程启动，
    该线程随服务退出才结束。check() 中另有 getppid 兜底。
    """
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
        except (OSError, AttributeError):
            pass
    # 设置之前父进程已经退出：直接结束。
    return os.getppid() == parent


def main(argv=None, builder=None, apply_limits=True):
    argv = sys.argv[1:] if argv is None else argv
    root, job_id, parent = argv[0], argv[1], int(argv[2])
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from .config import OfflineSettings
    settings = OfflineSettings(directory=root)
    # 测试在当前进程内调用 main 时不改动测试进程本身的优先级和内存上限。
    if apply_limits:
        lower_priority(settings)
        if not die_with_parent(parent):
            return 3

    from .bundle import BundleLimit
    from .jobs import CpuThrottle, Cancelled, OfflineJobs, RESULT
    jobs = OfflineJobs(settings)
    if not jobs.valid_id(job_id):
        return 2
    work = jobs.root / job_id

    def finish(**result):
        work.mkdir(parents=True, exist_ok=True)
        temporary = work / (RESULT + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, work / RESULT)

    job = jobs.get(job_id)
    if not job or job["status"] != "running":
        finish(status="cancelled")
        return 0
    progress, check = jobs.callbacks(job_id, throttle=CpuThrottle(settings.cpu_limit), parent=parent)
    try:
        path = (builder or jobs._build)(job["request"], work, progress, check)
        finish(status="completed", artifact=path.name)
    except Cancelled:
        finish(status="cancelled")
    except BundleLimit as error:
        finish(status="limit", message=str(error))
    except MemoryError:
        finish(status="limit", message=MEMORY_MESSAGE)
    except Exception as error:
        # 只记录异常类型，不把消息正文或签名链接写进日志。
        logger.warning("Offline archive generation failed: %s", type(error).__name__)
        finish(status="failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
