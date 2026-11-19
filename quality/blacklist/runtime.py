"""单个后台线程投递黑名单历史；归档不可用时 ICS 侧记录保留待送达，不影响客户端操作。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import logging
import threading

from .config import BlacklistSyncSettings
from .repository import BlacklistSyncRepository

logger = logging.getLogger(__name__)
POLL_SECONDS = 5
MAX_BACKOFF_SECONDS = 60


class BlacklistSyncRuntime:
    def __init__(self):
        self.task = None
        self.executor = None
        self.repository = None
        self.stopping = threading.Event()
        self.status = "disabled"

    async def start(self):
        if self.task is not None:
            return
        try:
            settings = BlacklistSyncSettings()
        except Exception as error:
            self.status = "configuration_error"
            logger.warning("Archive blacklist configuration unavailable: %s", type(error).__name__)
            return
        if not settings.blacklist_sync_enabled:
            return
        self.stopping.clear()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-blacklist")
        self.status = "connecting"
        self.task = asyncio.create_task(self._run(settings), name="quality-blacklist-sync")

    def _work(self, settings, source):
        if self.repository is None:
            repository = BlacklistSyncRepository(settings, source)
            try:
                repository.open()
            except BaseException:
                repository.close()
                raise
            self.repository = repository
        return self.repository.sync_once(self.stopping)

    async def _run(self, settings):
        from ics_agent.app.blacklist.runtime import get_blacklist_runtime

        failures = 0
        try:
            while not self.stopping.is_set():
                source = get_blacklist_runtime()
                if not source.ready or source.repository is None:
                    self.status = "waiting_source"
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                future = asyncio.get_running_loop().run_in_executor(self.executor, self._work, settings, source.repository)
                try:
                    await asyncio.shield(future)
                    failures = 0
                    self.status = "degraded" if self.repository and self.repository.last_batch_blocked else "ready"
                except asyncio.CancelledError:
                    self.stopping.set()
                    await asyncio.gather(future, return_exceptions=True)
                    raise
                except Exception as error:
                    failures += 1
                    self.status = "unavailable"
                    logger.warning("Archive blacklist synchronization unavailable: %s", type(error).__name__)
                await asyncio.sleep(min(MAX_BACKOFF_SECONDS, POLL_SECONDS * 2 ** min(failures, 4)))
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
                logger.warning("Archive blacklist shutdown unavailable: %s", type(error).__name__)
            finally:
                self.repository = None
                self.executor.shutdown(wait=False, cancel_futures=True)
                self.executor = None


@lru_cache
def get_blacklist_sync_runtime() -> BlacklistSyncRuntime:
    return BlacklistSyncRuntime()


async def start_blacklist_sync():
    await get_blacklist_sync_runtime().start()


async def stop_blacklist_sync():
    await get_blacklist_sync_runtime().close()
