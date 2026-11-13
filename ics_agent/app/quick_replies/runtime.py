"""快捷回复模块的生命周期：独立连接池，建表失败时定期重试，不影响应用启动。"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from .repository import QuickRepliesRepository
from .service import QuickRepliesService

logger = logging.getLogger(__name__)


class QuickRepliesRuntime:
    def __init__(self):
        self.service: QuickRepliesService | None = None
        self.repository: QuickRepliesRepository | None = None
        self.ready = False
        self.status = "disabled"
        self.task: asyncio.Task | None = None

    async def start(self, settings) -> None:
        if self.task is not None or not settings.chatwoot_quick_replies_enabled:
            return
        self.status = "starting"
        self.task = asyncio.create_task(self._run(settings), name="ics-quick-replies")

    async def _run(self, settings) -> None:
        failures = 0
        while not self.ready:
            try:
                repository = self.repository or QuickRepliesRepository(settings)
                self.repository = repository
                await asyncio.to_thread(repository.open)
                await asyncio.to_thread(repository.ensure_table)
                self.service = QuickRepliesService(settings, repository)
                self.ready = True
                self.status = "ready"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                self.status = "unavailable"
                logger.warning("事件=快捷回复.启动失败 错误类型=%s", type(error).__name__)
                await asyncio.sleep(min(300, 10 * 2 ** min(failures, 4)))

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.ready = False
        if self.repository is not None:
            try:
                await asyncio.to_thread(self.repository.close)
            except Exception as error:
                logger.warning("事件=快捷回复.关闭失败 错误类型=%s", type(error).__name__)
            self.repository = None
        self.service = None
        self.status = "stopped"


@lru_cache
def get_quick_replies_runtime() -> QuickRepliesRuntime:
    return QuickRepliesRuntime()
