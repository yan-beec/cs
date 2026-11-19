"""One optional background thread; no archive work in the handoff path."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
import logging
import threading
import time

from .config import AssignmentSyncSettings
from .repository import AssignmentSyncRepository

logger = logging.getLogger(__name__)
POLL_SECONDS = 5
MAX_BACKOFF_SECONDS = 60
QUEUE_COUNTS_SECONDS = 60


class AssignmentSyncRuntime:
    def __init__(self):
        self.task = None
        self.executor = None
        self.repository = None
        self.stopping = threading.Event()
        self.status = "disabled"
        self.pending_count = None
        self.blocked_count = None
        self.queue_pending_count = None
        self.queue_blocked_count = None
        self.counts_sampled_at = None
        self._next_counts_at = 0

    async def start(self):
        if self.task is not None:
            return
        try:
            settings = AssignmentSyncSettings()
        except Exception as error:
            self.status = "configuration_error"
            logger.warning("Archive assignment configuration unavailable: %s", type(error).__name__)
            return
        if not settings.assignment_sync_enabled:
            return
        self.stopping.clear()
        self.pending_count = self.blocked_count = self.counts_sampled_at = None
        self.queue_pending_count = self.queue_blocked_count = None
        self._next_counts_at = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-assignment")
        self.status = "connecting"
        self.task = asyncio.create_task(self._run(settings), name="quality-assignment-sync")

    def _work(self, settings):
        if self.repository is None:
            from ics_agent.app.core.config import get_settings

            repository = AssignmentSyncRepository(settings, get_settings())
            try:
                repository.open()
            except BaseException:
                repository.close()
                raise
            self.repository = repository
        try:
            delivered = self.repository.sync_once(self.stopping)
        finally:
            # Include durable changes even if a later row fails in this pass.
            blocked = self.repository.last_batch_blocked
            completed = self.repository.last_batch_delivered
            queue_blocked = getattr(self.repository, "last_batch_queue_blocked", 0)
            queue_completed = getattr(self.repository, "last_batch_queue_delivered", 0)
            queue_blocked = queue_blocked if isinstance(queue_blocked, int) else 0
            queue_completed = queue_completed if isinstance(queue_completed, int) else 0
            self.queue_blocked_count = (self.queue_blocked_count or 0) + queue_blocked
            if self.queue_pending_count is not None:
                self.queue_pending_count = max(0, self.queue_pending_count - queue_blocked - queue_completed)
            blocked -= queue_blocked
            completed -= queue_completed
            self.blocked_count = (self.blocked_count or 0) + blocked
            if self.pending_count is not None:
                self.pending_count = max(0, self.pending_count - blocked - completed)
        # A slow or failed health sample must not prevent durable deliveries.
        if time.monotonic() >= self._next_counts_at:
            counts = self.repository.queue_counts()
            self.pending_count = counts["pending_count"]
            self.blocked_count = counts["blocked_count"]
            queue_counts = self.repository.queue_evidence_counts()
            self.queue_pending_count = queue_counts["pending_count"]
            self.queue_blocked_count = queue_counts["blocked_count"]
            self.counts_sampled_at = datetime.now(timezone.utc).isoformat()
            self._next_counts_at = time.monotonic() + QUEUE_COUNTS_SECONDS
        return delivered

    async def _run(self, settings):
        failures = 0
        try:
            while not self.stopping.is_set():
                future = asyncio.get_running_loop().run_in_executor(self.executor, self._work, settings)
                try:
                    await asyncio.shield(future)
                    failures = 0
                    self.status = "degraded" if self.blocked_count or self.queue_blocked_count or self.queue_pending_count is None else "ready"
                except asyncio.CancelledError:
                    # Do not close pools while their sole worker still commits.
                    self.stopping.set()
                    await asyncio.gather(future, return_exceptions=True)
                    raise
                except Exception as error:
                    failures += 1
                    self.status = "unavailable"
                    # Exception text may contain database credentials or SQL.
                    logger.warning("Archive assignment synchronization unavailable: %s", type(error).__name__)
                delay = min(MAX_BACKOFF_SECONDS, POLL_SECONDS * 2 ** min(failures, 4))
                await asyncio.sleep(delay)
        finally:
            self.status = "stopped"

    async def close(self):
        self.stopping.set()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.executor is not None:
            try:
                if self.repository is not None:
                    await asyncio.get_running_loop().run_in_executor(self.executor, self.repository.close)
            except Exception as error:
                logger.warning("Archive assignment shutdown unavailable: %s", type(error).__name__)
            finally:
                self.repository = None
                self.executor.shutdown(wait=False, cancel_futures=True)
                self.executor = None


@lru_cache
def get_assignment_sync_runtime() -> AssignmentSyncRuntime:
    return AssignmentSyncRuntime()


async def start_assignment_sync():
    await get_assignment_sync_runtime().start()


async def stop_assignment_sync():
    await get_assignment_sync_runtime().close()
