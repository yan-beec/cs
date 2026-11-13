"""持久化留档任务。

生成过程默认放在独立的低优先级子进程中（见 worker.py），并按 CPU 配额限速：
导出慢一些没关系，但不能抢占 webhook 和后台接口的 CPU / GIL。
HTML 只是临时产物：首次下载完成后再保留 downloaded_hours（默认 2 小时，可重新下载）后删除；
一直无人下载的在 unclaimed_hours（默认 24 小时）后删除。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from .bundle import build_bundle, BundleLimit
from .config import OfflineSettings
from .media import MediaDownloader
from .naming import download_name

logger = logging.getLogger(__name__)

# 新版产物为单个 HTML；保留期内的旧版 ZIP 仍可下载。
ARTIFACTS = ("archive.html", "archive.zip")
RESULT = "result.json"
ACTIVE = ("queued", "running", "cancelling")
# 子进程收到取消后应在 1 秒内退出；超过宽限期仍在运行则强制结束。
CANCEL_GRACE_SECONDS = 30
CLEANUP_INTERVAL_SECONDS = 600


class Cancelled(RuntimeError):
    pass


class CpuThrottle:
    """按本进程 CPU 时间限速：平均占用超过 cores 个核时睡眠补足。

    在生成过程的检查点（每条消息、每个图片数据块）调用，开销只是一次时钟读取。
    窗口在“没有欠账”时滚动，空闲时间不会攒成之后的突发额度。
    """

    def __init__(self, cores, *, clock=time.monotonic, cpu=time.process_time, sleep=time.sleep,
                 window=2.0, max_sleep=1.0):
        self.cores = cores
        self.clock, self.cpu, self.sleep = clock, cpu, sleep
        self.window, self.max_sleep = window, max_sleep
        self.slept = 0.0
        self.reset()

    def reset(self):
        self.wall_start, self.cpu_start = self.clock(), self.cpu()

    def __call__(self):
        if self.cores <= 0:
            return
        wall = self.clock() - self.wall_start
        if wall < 0.05:
            return
        debt = (self.cpu() - self.cpu_start) / self.cores - wall
        if debt > 0:
            # 单次最多睡 max_sleep 秒，保证取消和停机仍能及时响应。
            pause = min(debt, self.max_sleep)
            self.slept += pause
            self.sleep(pause)
        elif wall > self.window:
            self.reset()


class OfflineJobs:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.directory).resolve()
        # Windows 的 0700 会创建专用 ACL，使 Docker 共享目录中的文件不可访问。
        # 在 Windows 继承项目目录权限；Linux 仍限制为当前服务账号可访问。
        self.root.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
        self.database = self.root / "jobs.sqlite3"
        self.stopping = threading.Event()
        self.task = None
        self.executor = None
        self.worker_lock = None
        self.last_cleanup = 0
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL,
                progress TEXT NOT NULL DEFAULT '{}', error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                size INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO secrets VALUES ('download',?)", (secrets.token_hex(32),))
            self.key = db.execute("SELECT value FROM secrets WHERE name='download'").fetchone()[0].encode()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def valid_id(value):
        return bool(re.fullmatch(r"[a-f0-9]{32}", str(value)))

    def state(self, row):
        if row is None:
            return None
        request = json.loads(row["request"])
        try:
            filename = download_name(request, row["created"])
        except (ValueError, TypeError, AttributeError):
            filename = None
        result = {**dict(row), "request": request, "progress": json.loads(row["progress"]), "filename": filename}
        # 服务器端文件计划删除时间，及当前是否仍可下载（已下载的在保留期内可重新下载）。
        result["delete_at"] = self._delete_at(row["status"], row["updated"])
        result["available"] = self._downloadable(row["id"], row["status"], row["updated"])
        return result

    def _delete_at(self, status, updated):
        if status == "completed":
            return updated + self.settings.unclaimed_hours * 3600
        if status == "downloaded":
            return updated + self.settings.downloaded_hours * 3600
        return None

    def _downloadable(self, job_id, status, updated):
        if status not in ("completed", "downloaded"):
            return False
        # 以时间为准：即使定时清理还没运行，超过保留期也不再提供下载。
        if status == "downloaded" and time.time() >= self._delete_at(status, updated):
            return False
        return self.artifact(job_id) is not None

    def artifact(self, job_id):
        if not self.valid_id(job_id):
            return None
        for name in ARTIFACTS:
            path = self.root / job_id / name
            if path.is_file():
                return path
        return None

    def get(self, job_id):
        if not self.valid_id(job_id):
            return None
        with self.connection() as db:
            return self.state(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self):
        with self.connection() as db:
            return [self.state(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 20")]

    def enqueue(self, request):
        if not self.worker_available():
            raise ValueError("离线留档工作进程尚未就绪，请稍后重试。")
        self.cleanup()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone():
                raise ValueError("已有离线留档正在生成，请等待完成或取消后重试。")
            job_id, now = uuid.uuid4().hex, time.time()
            request = {**request, "requested_at": now}
            db.execute("INSERT INTO jobs(id,status,request,created,updated) VALUES (?,'queued',?,?,?)",
                       (job_id, json.dumps(request, default=str), now, now))
        return self.get(job_id)

    def worker_available(self):
        # 应用启动时持有进程锁，组件测试也可以直接同步消费任务。
        return self.task is None or not self.task.done()

    def cancel(self, job_id):
        with self.connection() as db:
            db.execute("""UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                       updated=? WHERE id=? AND status IN ('queued','running')""", (time.time(), job_id))
        return self.get(job_id)

    def delete(self, job_id):
        """管理员手动删除一条任务记录；服务器端若仍有文件（待下载 / 下载后保留期内）一并删除。

        生成中的任务须先取消，避免与工作进程同时操作任务目录。返回 None 表示记录不存在。
        """
        if not self.valid_id(job_id):
            return None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] in ACTIVE:
                raise ValueError("留档正在生成，请先取消任务，待状态变为“已取消”后再删除记录。")
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        # Windows 下文件被占用删不掉时，定时清理会按“无记录的遗留目录”再次删除。
        self._remove_files(job_id)
        return True

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='queued',progress='{}',updated=? WHERE status='running'", (time.time(),))
            db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE status='cancelling'", (time.time(),))

    def _claim(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row["id"]))
            return self.state(row)

    def _build(self, request, work, progress, check):
        from ics_agent.app.core.config import get_settings
        from .repository import messages
        app = get_settings()
        origins = [app.chatwoot_base_url, app.chatwoot_public_base_url,
                   *[v.strip() for v in self.settings.media_origins.split(",") if v.strip()]]
        replacements = {app.chatwoot_public_base_url: app.chatwoot_base_url} if app.chatwoot_public_base_url else {}
        # 每个附件按类别（图片 / 视频音频）传入下载上限，超限的保留原始链接。
        downloader = MediaDownloader(origins, replacements=replacements, timeout=self.settings.media_timeout_seconds,
                                     max_file_bytes=self.settings.max_file_bytes)
        # 快照信息在提取阶段填充，即使没有消息，也保留实际生成快照的时点。
        metadata = {}
        path = build_bundle(work, messages(request, check, self.settings, metadata=metadata),
                            self.settings, downloader, progress, check, scope=request, metadata=metadata)
        return path

    def callbacks(self, job_id, *, throttle=None, parent=None):
        """返回生成过程使用的 progress / check；子进程与进程内测试共用。"""
        last_check = 0

        def check():
            nonlocal last_check
            if self.stopping.is_set():
                raise Cancelled()
            if throttle is not None:
                throttle()
            now = time.monotonic()
            if now - last_check < 0.25:
                return
            last_check = now
            # 父进程（ICS 服务）已退出：停止生成，避免重启后出现两个进程写同一任务目录。
            if parent is not None and os.getppid() != parent:
                raise Cancelled()
            state = self.get(job_id)
            if not state or state["status"] != "running":
                raise Cancelled()

        def progress(**values):
            check()
            with self.connection() as db:
                old = json.loads(db.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
                db.execute("UPDATE jobs SET progress=?,updated=? WHERE id=?",
                           (json.dumps({**old, **values}), time.time(), job_id))
        return progress, check

    # ---- 子进程执行 ----

    def _child_command(self, job_id):
        command = [sys.executable, "-m", "quality.offline.worker", str(self.root), job_id, str(os.getpid())]
        # Linux 下以最低的 best-effort 磁盘 IO 优先级运行（不用 idle 类，避免持续 IO 时任务被饿死）；
        # -t：容器不允许设置时忽略失败，照常运行。
        ionice = shutil.which("ionice") if os.name != "nt" else None
        return [ionice, "-t", "-c", "2", "-n", "7", *command] if ionice else command

    def _child_options(self):
        options = {"cwd": os.getcwd(), "stdin": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
        # Linux 的 nice、内存上限由子进程启动时自行设置；多线程进程中不使用 preexec_fn。
        return options

    def _run_child(self, job_id, work):
        result_path = work / RESULT
        process = subprocess.Popen(self._child_command(job_id), **self._child_options())
        cancelling_since = None
        try:
            while True:
                try:
                    code = process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.stopping.is_set():
                    raise Cancelled()
                try:
                    state = self.get(job_id)
                except sqlite3.Error:
                    continue  # 任务库短暂繁忙（子进程正在写进度），下一轮再看
                if not state or state["status"] != "running":
                    cancelling_since = cancelling_since or time.monotonic()
                    if time.monotonic() - cancelling_since > CANCEL_GRACE_SECONDS:
                        raise Cancelled()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {}
        result_path.unlink(missing_ok=True)
        status = result.get("status")
        if status == "completed" and code == 0 and result.get("artifact") in ARTIFACTS:
            path = work / result["artifact"]
            if path.is_file():
                return path
        if status == "cancelled":
            raise Cancelled()
        if status == "limit":
            raise BundleLimit(str(result.get("message") or "留档生成超出资源限制。"))
        if code in (-9, 137) and not status:
            # 被系统 OOM 结束等情况：给出可操作的提示，而不是通用失败。
            raise BundleLimit("留档生成进程被系统强制结束（通常是内存不足），请缩小导出范围后重试。")
        raise RuntimeError(f"offline archive worker exited with {code}")

    def run_once(self, builder=None):
        job = self._claim()
        if not job:
            return False
        job_id = job["id"]
        work = self.root / job_id
        # 清理路径只使用程序生成的任务 ID，不接受用户指定的文件路径。
        if work.exists():
            shutil.rmtree(work)
        interrupted = False
        progress, check = self.callbacks(job_id)
        try:
            if builder is None and self.settings.isolated_process:
                path = self._run_child(job_id, work)
            else:
                path = (builder or self._build)(job["request"], work, progress, check)
            check()
            with self.connection() as db:
                # 发布完整产物时再次原子检查取消状态，防止取消与完成互相覆盖。
                updated = db.execute("UPDATE jobs SET status='completed',size=?,updated=? WHERE id=? AND status='running'",
                                     (path.stat().st_size, time.time(), job_id)).rowcount
            if not updated:
                raise Cancelled()
        except Cancelled:
            interrupted = self.stopping.is_set()
            with self.connection() as db:
                db.execute("UPDATE jobs SET status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE ? END,updated=? WHERE id=?",
                           ("queued" if interrupted else "cancelled", time.time(), job_id))
        except Exception as error:
            message = str(error) if isinstance(error, BundleLimit) else "留档生成失败，请检查归档连接、磁盘或服务日志后重试。"
            logger.warning("Offline archive generation failed: %s", type(error).__name__)
            with self.connection() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?", (message, time.time(), job_id))
        finally:
            # 记录可能已被管理员删除（取消后立即删除），此时同样清理目录。
            final = self.get(job_id)
            if (not final or final["status"] != "completed") and work.exists():
                shutil.rmtree(work, ignore_errors=True)
        return True

    # ---- 下载与删除 ----

    def _unavailable(self, job):
        if job and job["status"] == "downloaded":
            return (f"该留档已下载超过 {self.settings.downloaded_hours} 小时，服务器端文件已删除；"
                    "如需再次获取请重新生成。")
        if job and job["status"] == "expired":
            return "该留档超过保留时间未下载，服务器端文件已删除，请重新生成。"
        return "留档尚未完成或已过期。"

    def ticket(self, job_id):
        job = self.get(job_id)
        if not job or not job["available"]:
            raise ValueError(self._unavailable(job))
        expires = str(int(time.time()) + 300)
        signature = hmac.new(self.key, (job_id + ":" + expires).encode(), hashlib.sha256).hexdigest()
        return expires + "." + signature

    def download_path(self, job_id, ticket):
        if not self.valid_id(job_id):
            raise ValueError("无效下载")
        try:
            expires, signature = ticket.split(".", 1)
            expected = hmac.new(self.key, (job_id + ":" + expires).encode(), hashlib.sha256).hexdigest()
            if int(expires) < time.time() or not hmac.compare_digest(signature, expected):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("下载链接已失效，请重新点击下载。") from None
        job = self.get(job_id)
        path = self.artifact(job_id)
        if not job or not job["available"] or not path:
            raise ValueError(self._unavailable(job))
        if job["status"] == "completed":
            # 尚未完整下载过：从本次下载开始重新计算未领取保留时间，避免传输途中被清理。
            # 已下载的不顺延，保留期始终从首次下载完成时算起。
            with self.connection() as db:
                db.execute("UPDATE jobs SET updated=? WHERE id=? AND status='completed'", (time.time(), job_id))
        return path

    def _remove_files(self, job_id):
        path = self.root / job_id
        if not self.valid_id(job_id) or not path.exists():
            return True
        try:
            shutil.rmtree(path)
            return True
        except OSError as error:
            # Windows 下文件仍被其他下载占用时删不掉，留给定时清理重试。
            logger.warning("Offline archive files not removed yet: %s", type(error).__name__)
            return False

    def consume(self, job_id):
        """服务端已发完整个文件：标记为已下载并记录时间，文件再保留 downloaded_hours 供重新下载。

        “发完”不等于用户已保存（反向代理缓冲、浏览器取消另存、安全拦截都可能丢失），
        所以不立即删除；只有首次完整下载记录时间，重复下载不顺延保留期。
        """
        if not self.valid_id(job_id):
            return
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='downloaded',updated=? WHERE id=? AND status='completed'",
                       (time.time(), job_id))

    def cleanup(self):
        now = time.time()
        unclaimed = now - self.settings.unclaimed_hours * 3600
        downloaded = now - self.settings.downloaded_hours * 3600
        records = now - self.settings.retention_days * 86400
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='expired',updated=? WHERE status='completed' AND updated<?",
                       (now, unclaimed))
            rows = db.execute("SELECT id,status,updated FROM jobs").fetchall()
        known = set()
        for row in rows:
            known.add(row["id"])
            if row["status"] in ACTIVE or row["status"] == "completed":
                continue
            if row["status"] == "downloaded" and row["updated"] >= downloaded:
                continue  # 下载后的保留期内，仍可重新下载
            # 已下载超过保留期 / 已过期 / 已取消 / 失败：确保没有残留文件（含 Windows 下当时没删掉的）。
            removed = self._remove_files(row["id"])
            if removed and row["updated"] < records:
                with self.connection() as db:
                    db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        # 没有任务记录的任务目录（旧版本遗留、记录被删除等）一并清除；
        # 只处理一小时未变动的目录，避免与刚入队的新任务竞争。
        for path in self.root.iterdir():
            try:
                stale = path.is_dir() and now - path.stat().st_mtime > 3600
            except OSError:
                continue
            if stale and self.valid_id(path.name) and path.name not in known and self.get(path.name) is None:
                self._remove_files(path.name)
        self.last_cleanup = time.time()

    # ---- 后台工作线程 ----

    def _lock(self):
        stream = (self.root / "worker.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if not stream.tell():
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.worker_lock = stream
        return True

    async def start(self):
        if self.task is not None:
            return
        if not await asyncio.to_thread(self._lock):
            logger.warning("Offline archive worker already running for this directory")
            return
        self.stopping.clear()
        await asyncio.to_thread(self.recover)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-archive")
        loop = asyncio.get_running_loop()
        async def run():
            while not self.stopping.is_set():
                try:
                    if time.time() - self.last_cleanup > CLEANUP_INTERVAL_SECONDS:
                        await loop.run_in_executor(self.executor, self.cleanup)
                    worked = await loop.run_in_executor(self.executor, self.run_once)
                except Exception as error:
                    logger.warning("Offline archive worker unavailable: %s", type(error).__name__)
                    worked = False
                if not worked:
                    await asyncio.sleep(1)
        self.task = asyncio.create_task(run(), name="offline-archive-worker")

    async def close(self):
        self.stopping.set()
        if self.task:
            await self.task
            self.task = None
        if self.worker_lock:
            self.worker_lock.close()
            self.worker_lock = None
        if self.executor:
            self.executor.shutdown(wait=True)
            self.executor = None


@lru_cache
def get_offline_jobs():
    return OfflineJobs(OfflineSettings())
