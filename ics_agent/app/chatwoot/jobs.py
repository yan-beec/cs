"""Webhook 幂等登记、后台队列、超时控制和重试。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ics_agent.app.core.logging_config import log_event
from ics_agent.faq.repositories import PostgreSQLConnectionPool
from ics_agent.rag.config import get_rag_settings


logger = logging.getLogger(__name__)


WEBHOOK_TABLE_NAME = "chatwoot_webhook_idempotency"


@dataclass(frozen=True)
class WebhookClaim:
    """Webhook 幂等登记结果。"""

    accepted: bool
    status: str
    attempts: int
    first_seen_at: datetime | None = None


def payload_hash(raw_body: bytes) -> str:
    """返回请求体哈希，供没有事件 ID 的事件稳定去重。"""
    return hashlib.sha256(raw_body).hexdigest()


class WebhookIdempotencyRepository:
    """使用现有 PostgreSQL 连接池保存 Webhook 处理状态。"""

    def __init__(self, connections: PostgreSQLConnectionPool | None = None):
        self._connections = connections or PostgreSQLConnectionPool()

    def ensure_table(self) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {WEBHOOK_TABLE_NAME} (
                        event_key TEXT PRIMARY KEY,
                        event_id TEXT,
                        event_type TEXT NOT NULL DEFAULT '',
                        payload_hash TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN (
                                'queued', 'processing', 'completed', 'failed'
                            )),
                        attempts INTEGER NOT NULL DEFAULT 1,
                        last_error TEXT,
                        result JSONB,
                        first_seen_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMPTZ
                    )
                    """.strip()
                )
                cursor.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS
                    {WEBHOOK_TABLE_NAME}_updated_idx
                    ON {WEBHOOK_TABLE_NAME} (updated_at)
                    """.strip()
                )
        finally:
            connection.close()

    def claim(
        self,
        *,
        event_key: str,
        event_id: str | None,
        event_type: str,
        body_hash: str,
        stale_after_seconds: int,
    ) -> WebhookClaim:
        """登记一个事件；成功事件永远不会再次入队。"""
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    INSERT INTO {WEBHOOK_TABLE_NAME} (
                        event_key, event_id, event_type, payload_hash
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (event_key) DO NOTHING
                    RETURNING status, attempts, first_seen_at
                    """.strip(),
                    (event_key, event_id, event_type, body_hash),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    return WebhookClaim(
                        accepted=True,
                        status=str(inserted[0]),
                        attempts=int(inserted[1]),
                        first_seen_at=inserted[2],
                    )

                cursor.execute(
                    f"""
                    SELECT status, attempts, first_seen_at
                    FROM {WEBHOOK_TABLE_NAME}
                    WHERE event_key = %s
                    FOR UPDATE
                    """.strip(),
                    (event_key,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError(
                        "Webhook idempotency row disappeared during claim."
                    )

                status = str(existing[0])
                attempts = int(existing[1])
                if status in {"queued", "completed"}:
                    return WebhookClaim(
                        accepted=False,
                        status=status,
                        attempts=attempts,
                        first_seen_at=existing[2],
                    )

                # failed 事件允许再次被 Chatwoot 投递时重新入队；
                # processing 超过陈旧时间时允许回收 worker 崩溃遗留状态。
                cursor.execute(
                    f"""
                    UPDATE {WEBHOOK_TABLE_NAME}
                    SET
                        status = 'queued',
                        event_id = %s,
                        event_type = %s,
                        payload_hash = %s,
                        attempts = attempts + 1,
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP,
                        completed_at = NULL
                    WHERE event_key = %s
                      AND (
                          status = 'failed'
                          OR (
                              status = 'processing'
                              AND updated_at < CURRENT_TIMESTAMP
                                  - (%s * INTERVAL '1 second')
                          )
                      )
                    RETURNING status, attempts, first_seen_at
                    """.strip(),
                    (
                        event_id,
                        event_type,
                        body_hash,
                        event_key,
                        stale_after_seconds,
                    ),
                )
                reclaimed = cursor.fetchone()
                if reclaimed is None:
                    return WebhookClaim(
                        accepted=False,
                        status=status,
                        attempts=attempts,
                        first_seen_at=existing[2],
                    )
                return WebhookClaim(
                    accepted=True,
                    status=str(reclaimed[0]),
                    attempts=int(reclaimed[1]),
                    first_seen_at=reclaimed[2],
                )
        finally:
            connection.close()

    def mark_processing(self, event_key: str) -> None:
        self._update_status(
            event_key,
            status="processing",
            last_error=None,
        )

    def mark_completed(
        self,
        event_key: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    UPDATE {WEBHOOK_TABLE_NAME}
                    SET
                        status = 'completed',
                        result = %s,
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE event_key = %s
                    """.strip(),
                    (Jsonb(result) if result is not None else None, event_key),
                )
        finally:
            connection.close()

    def mark_failed(self, event_key: str, error: str) -> None:
        self._update_status(
            event_key,
            status="failed",
            last_error=error[:2000],
        )

    def get(self, event_key: str) -> dict[str, Any] | None:
        connection = self._connections.acquire()
        try:
            cursor = connection.cursor()
            cursor.execute(
                f"""
                SELECT event_key, event_id, event_type, status, attempts,
                       last_error, result, first_seen_at, updated_at,
                       completed_at
                FROM {WEBHOOK_TABLE_NAME}
                WHERE event_key = %s
                """.strip(),
                (event_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "event_key": row[0],
                "event_id": row[1],
                "event_type": row[2],
                "status": row[3],
                "attempts": row[4],
                "last_error": row[5],
                "result": row[6],
                "first_seen_at": row[7].isoformat() if row[7] else None,
                "updated_at": row[8].isoformat() if row[8] else None,
                "completed_at": row[9].isoformat() if row[9] else None,
            }
        finally:
            connection.close()

    def cleanup_expired(self, *, retention_days: int, batch_size: int) -> int:
        """清理已完成或最终失败且超过保留期的幂等记录。"""
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    DELETE FROM {WEBHOOK_TABLE_NAME}
                    WHERE event_key IN (
                        SELECT event_key
                        FROM {WEBHOOK_TABLE_NAME}
                        -- 进程崩溃时丢在内存队列里的 queued/processing 记录不会再被处理，
                        -- 超过保留期后与终态记录一起清理，避免表只增不减。
                        WHERE status IN ('completed', 'failed', 'queued', 'processing')
                          AND updated_at < CURRENT_TIMESTAMP
                              - (%s * INTERVAL '1 day')
                        ORDER BY updated_at
                        LIMIT %s
                    )
                    """.strip(),
                    (retention_days, batch_size),
                )
                deleted = cursor.rowcount
            if deleted:
                log_event(
                    logger,
                    logging.INFO,
                    "webhook.idempotency.cleanup.completed",
                    deleted=deleted,
                )
            return deleted
        finally:
            connection.close()

    def _update_status(
        self,
        event_key: str,
        *,
        status: str,
        last_error: str | None,
    ) -> None:
        connection = self._connections.acquire()
        try:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    f"""
                    UPDATE {WEBHOOK_TABLE_NAME}
                    SET status = %s,
                        last_error = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE event_key = %s
                    """.strip(),
                    (status, last_error, event_key),
                )
        finally:
            connection.close()


_REPOSITORY = WebhookIdempotencyRepository()


def get_webhook_idempotency_repository() -> WebhookIdempotencyRepository:
    """返回进程级 Webhook 幂等仓库。"""
    return _REPOSITORY


WebhookProcessor = Callable[["WebhookJob"], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class WebhookJob:
    """待处理的 Chatwoot Webhook 任务。"""

    event_key: str
    event: Any
    partition_key: str | None = None
    # Keep the fallback time stable across worker retries.
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WebhookQueueFull(RuntimeError):
    """后台队列已达到上限。"""


class WebhookJobQueue:
    """单进程异步队列；幂等状态持久化在 PostgreSQL。"""

    def __init__(
        self,
        processor: WebhookProcessor,
        repository: WebhookIdempotencyRepository,
    ) -> None:
        self._processor = processor
        self._repository = repository
        self._queue: asyncio.Queue[WebhookJob] | None = None
        # ``asyncio.Queue`` only counts items waiting for a worker. Workers
        # hand jobs off to independent tasks immediately, so queue.qsize()
        # alone would allow an unbounded number of in-flight jobs. Keep an
        # admission count for every job that has not finished yet.
        self._pending_jobs = 0
        self._max_pending_jobs = 0
        self._workers: list[asyncio.Task[None]] = []
        self._processing_slots: asyncio.Semaphore | None = None
        self._job_tasks: set[asyncio.Task[None]] = set()
        self._job_tails: dict[str, asyncio.Task[None]] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """启动 worker 和幂等记录清理任务。"""
        settings = get_rag_settings()
        if self._queue is not None:
            return
        self._queue = asyncio.Queue(maxsize=settings.webhook_queue_max_size)
        self._max_pending_jobs = settings.webhook_queue_max_size
        self._pending_jobs = 0
        self._processing_slots = asyncio.Semaphore(
            settings.webhook_worker_count
        )
        self._stopping = False
        self._workers = [
            asyncio.create_task(
                self._worker_loop(index),
                name=f"webhook-worker-{index}",
            )
            for index in range(settings.webhook_worker_count)
        ]
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="webhook-idempotency-cleanup",
        )
        log_event(
            logger,
            logging.INFO,
            "webhook.queue.started",
            worker_count=len(self._workers),
            max_size=settings.webhook_queue_max_size,
        )

    async def stop(self) -> None:
        """等待有限时间排空队列，然后停止后台任务。"""
        if self._queue is None:
            return
        settings = get_rag_settings()
        self._stopping = True
        try:
            await asyncio.wait_for(
                self._wait_for_drain(),
                timeout=settings.webhook_queue_shutdown_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log_event(
                logger,
                logging.WARNING,
                "webhook.queue.shutdown_timeout",
                pending=self._queue.qsize(),
                active=len(self._job_tasks),
            )
            active_tasks = tuple(self._job_tasks)
            for task in active_tasks:
                task.cancel()
            if active_tasks:
                await asyncio.gather(
                    *active_tasks,
                    return_exceptions=True,
                )
        for task in self._workers:
            task.cancel()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        cleanup_tasks = (
            [self._cleanup_task] if self._cleanup_task is not None else []
        )
        await asyncio.gather(
            *self._workers,
            *cleanup_tasks,
            return_exceptions=True,
        )
        self._workers.clear()
        self._job_tasks.clear()
        self._job_tails.clear()
        self._pending_jobs = 0
        self._max_pending_jobs = 0
        self._processing_slots = None
        self._cleanup_task = None
        self._queue = None
        log_event(logger, logging.INFO, "webhook.queue.stopped")

    def enqueue_nowait(self, job: WebhookJob) -> None:
        """将任务放入队列；队列满时由调用方返回可重试响应。"""
        if self._queue is None or self._stopping:
            raise WebhookQueueFull("Webhook queue is not accepting jobs.")
        if self._pending_jobs >= self._max_pending_jobs:
            raise WebhookQueueFull("Webhook queue is full.")
        # enqueue_nowait is called on the event loop thread, so the admission
        # check and increment are atomic with respect to other callers.
        self._pending_jobs += 1
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            self._pending_jobs -= 1
            raise WebhookQueueFull("Webhook queue is full.") from exc

    async def _worker_loop(self, worker_index: int) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                self._schedule_job(job, worker_index)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._pending_jobs = max(0, self._pending_jobs - 1)
                logger.exception(
                    "事件=Webhook队列.任务.未处理异常 worker=%s key=%s",
                    worker_index,
                    job.event_key,
                )
            finally:
                self._queue.task_done()

    def _schedule_job(self, job: WebhookJob, worker_index: int) -> None:
        """调度任务链，避免 worker 阻塞等待同一会话的前序任务。"""
        previous = (
            self._job_tails.get(job.partition_key)
            if job.partition_key
            else None
        )
        task = asyncio.create_task(
            self._run_scheduled_job(
                job,
                worker_index,
                previous=previous,
            ),
            name=f"webhook-job-{job.event_key}",
        )
        self._job_tasks.add(task)
        if job.partition_key:
            self._job_tails[job.partition_key] = task
        task.add_done_callback(
            lambda finished: self._job_finished(
                job.partition_key,
                finished,
            )
        )

    def _job_finished(
        self,
        partition_key: str | None,
        task: asyncio.Task[None],
    ) -> None:
        self._job_tasks.discard(task)
        self._pending_jobs = max(0, self._pending_jobs - 1)
        if (
            partition_key
            and self._job_tails.get(partition_key) is task
        ):
            self._job_tails.pop(partition_key, None)
        with suppress(asyncio.CancelledError):
            try:
                task.result()
            except Exception:
                logger.exception(
                    "事件=Webhook队列.任务.未处理异常 key=%s",
                    task.get_name(),
                )

    async def _run_scheduled_job(
        self,
        job: WebhookJob,
        worker_index: int,
        *,
        previous: asyncio.Task[None] | None,
    ) -> None:
        """等待同会话前序任务，再占用一个实际处理槽位。"""
        if previous is not None:
            try:
                await previous
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                logger.warning(
                    "事件=Webhook队列.前序任务被取消 key=%s",
                    job.event_key,
                )
            except Exception:
                logger.exception(
                    "事件=Webhook队列.前序任务异常 key=%s",
                    job.event_key,
                )

        if self._processing_slots is None:
            raise RuntimeError("Webhook processing slots are not started.")
        async with self._processing_slots:
            await self._process_with_retry(job, worker_index)

    async def _wait_for_drain(self) -> None:
        assert self._queue is not None
        await self._queue.join()
        active_tasks = tuple(self._job_tasks)
        if active_tasks:
            await asyncio.gather(
                *active_tasks,
                return_exceptions=True,
            )

    async def _process_with_retry(
        self,
        job: WebhookJob,
        worker_index: int,
    ) -> None:
        settings = get_rag_settings()
        last_error = "unknown_error"
        for attempt in range(1, settings.webhook_max_retries + 2):
            try:
                await asyncio.to_thread(
                    self._repository.mark_processing,
                    job.event_key,
                )
                result = await asyncio.wait_for(
                    self._processor(job),
                    timeout=settings.webhook_processing_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log_event(
                    logger,
                    logging.WARNING,
                    "webhook.queue.attempt_failed",
                    event_key=job.event_key,
                    worker=worker_index,
                    attempt=attempt,
                    max_attempts=settings.webhook_max_retries + 1,
                    error_type=type(exc).__name__,
                )
                if attempt > settings.webhook_max_retries:
                    await asyncio.to_thread(
                        self._repository.mark_failed,
                        job.event_key,
                        last_error,
                    )
                    return
                delay = min(
                    settings.webhook_retry_initial_delay_seconds
                    * settings.webhook_retry_backoff_multiplier ** (attempt - 1),
                    settings.webhook_retry_max_delay_seconds,
                )
                await asyncio.sleep(delay)
                continue

            try:
                await asyncio.to_thread(
                    self._repository.mark_completed,
                    job.event_key,
                    result,
                )
            except Exception as exc:
                # 业务处理已经完成，但状态落库失败时不能留下永久
                # processing；后续 Chatwoot 重试仍可重新接管该事件。
                log_event(
                    logger,
                    logging.ERROR,
                    "webhook.queue.completion_persist_failed",
                    event_key=job.event_key,
                    error_type=type(exc).__name__,
                )
                try:
                    await asyncio.to_thread(
                        self._repository.mark_failed,
                        job.event_key,
                        f"completion_persist_failed: {exc}",
                    )
                except Exception:
                    logger.exception(
                        "事件=Webhook队列.失败状态.持久化失败 key=%s",
                        job.event_key,
                    )
                return
            log_event(
                logger,
                logging.INFO,
                "webhook.queue.completed",
                event_key=job.event_key,
                worker=worker_index,
                attempt=attempt,
            )
            return

    async def _cleanup_loop(self) -> None:
        # 启动后先清理一次：按天间隔时，频繁重启的服务以前一次都清不到。
        while True:
            settings = get_rag_settings()
            try:
                await self._drain_expired(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "webhook.idempotency.cleanup.failed",
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(
                settings.webhook_idempotency_cleanup_interval_seconds
            )

    async def _drain_expired(self, settings) -> int:
        """分批删到不足一批为止；每批之间短暂让出，单轮最多 200 批。

        每天的 webhook 远多于一批（默认 1000 行），只删一批会让表持续膨胀。
        """
        batch_size = settings.webhook_idempotency_cleanup_batch_size
        total = 0
        for _ in range(200):
            deleted = await asyncio.to_thread(
                self._repository.cleanup_expired,
                retention_days=settings.webhook_idempotency_retention_days,
                batch_size=batch_size,
            )
            total += deleted
            if deleted < batch_size:
                break
            await asyncio.sleep(0.2)
        return total
