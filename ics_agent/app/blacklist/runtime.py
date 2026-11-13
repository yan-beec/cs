"""黑名单模块的生命周期：独立连接池 + 后台补记任务，失败不影响应用启动。"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from .repository import BlacklistRepository
from .service import BlacklistService

logger = logging.getLogger(__name__)


class BlacklistRuntime:
    def __init__(self):
        self.service: BlacklistService | None = None
        self.repository: BlacklistRepository | None = None
        self.ready = False
        self.status = "disabled"
        self.task: asyncio.Task | None = None

    async def start(self, settings) -> None:
        if self.task is not None or not settings.chatwoot_blacklist_enabled:
            return
        self.status = "starting"
        self.task = asyncio.create_task(self._run(settings), name="ics-blacklist")

    async def _run(self, settings) -> None:
        failures = 0
        try:
            while True:
                try:
                    if not self.ready:
                        repository = self.repository or BlacklistRepository(settings)
                        self.repository = repository
                        await asyncio.to_thread(repository.open)
                        await asyncio.to_thread(repository.ensure_table)
                        self.service = BlacklistService(settings, repository, on_blocked=_end_handoffs)
                        self.ready = True
                        self.status = "ready"
                    await self.service.reconcile_once()
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failures += 1
                    if not self.ready:
                        self.status = "unavailable"
                    logger.warning("事件=黑名单.后台任务失败 错误类型=%s", type(error).__name__)
                await asyncio.sleep(min(300, settings.chatwoot_blacklist_reconcile_seconds * 2 ** min(failures, 3)))
        finally:
            self.ready = False

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.repository is not None:
            try:
                await asyncio.to_thread(self.repository.close)
            except Exception as error:
                logger.warning("事件=黑名单.关闭失败 错误类型=%s", type(error).__name__)
            self.repository = None
        self.service = None
        self.status = "stopped"


async def _end_handoffs(**kwargs) -> None:
    # 延迟导入：避免黑名单模块加载时拉起整个 webhook 编排。
    from ics_agent.app.chatwoot.routes import end_blacklisted_handoffs
    await end_blacklisted_handoffs(**kwargs)


@lru_cache
def get_blacklist_runtime() -> BlacklistRuntime:
    return BlacklistRuntime()
