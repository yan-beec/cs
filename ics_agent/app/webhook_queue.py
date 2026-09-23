"""Webhook 后台队列、超时控制和指数退避重试。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ics_agent.app.logging_config import log_event
from ics_agent.app.webhook_idempotency import (
    WebhookIdempotencyRepository,
)
from ics_agent.rag.config import get_rag_settings


logger = logging.getLogger(__name__)
WebhookProcessor = Callable[["WebhookJob"], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class WebhookJob:
    """待处理的 Chatwoot Webhook 任务。"""

    event_key: str
    event: Any


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
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """启动 worker 和幂等记录清理任务。"""
        settings = get_rag_settings()
        if self._queue is not None:
            return
        self._queue = asyncio.Queue(maxsize=settings.webhook_queue_max_size)
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
                self._queue.join(),
                timeout=settings.webhook_queue_shutdown_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log_event(
                logger,
                logging.WARNING,
                "webhook.queue.shutdown_timeout",
                pending=self._queue.qsize(),
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
        self._cleanup_task = None
        self._queue = None
        log_event(logger, logging.INFO, "webhook.queue.stopped")

    def enqueue_nowait(self, job: WebhookJob) -> None:
        """将任务放入队列；队列满时由调用方返回可重试响应。"""
        if self._queue is None or self._stopping:
            raise WebhookQueueFull("Webhook queue is not accepting jobs.")
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise WebhookQueueFull("Webhook queue is full.") from exc

    async def _worker_loop(self, worker_index: int) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                await self._process_with_retry(job, worker_index)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "事件=Webhook队列.任务.未处理异常 worker=%s key=%s",
                    worker_index,
                    job.event_key,
                )
            finally:
                self._queue.task_done()

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
        while True:
            settings = get_rag_settings()
            await asyncio.sleep(
                settings.webhook_idempotency_cleanup_interval_seconds
            )
            try:
                await asyncio.to_thread(
                    self._repository.cleanup_expired,
                    retention_days=settings.webhook_idempotency_retention_days,
                    batch_size=settings.webhook_idempotency_cleanup_batch_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "webhook.idempotency.cleanup.failed",
                    error_type=type(exc).__name__,
                )
