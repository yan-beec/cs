"""排队模块独立生命周期及十分钟后台清理。"""
import asyncio
import logging
from datetime import datetime, timezone
from functools import lru_cache
from .repository import HandoffQueueRepository
from .service import HandoffQueueService
logger = logging.getLogger(__name__)

class HandoffQueueRuntime:

    def __init__(self):
        self.repository = None
        self.service = None
        self.ready = False
        self.task = None

    async def start(self, settings):
        if self.task is None:
            self.task = asyncio.create_task(self._run(settings), name='ics-handoff-queue')

    async def _run(self, settings):
        try:
            while True:
                try:
                    if not self.ready:
                        self.repository = self.repository or HandoffQueueRepository(settings)
                        await asyncio.to_thread(self.repository.open)
                        await asyncio.to_thread(self.repository.ensure_tables)
                        self.service = HandoffQueueService(self.repository)
                        await self.service.current_config(force=True)
                        self.ready = True
                    await asyncio.to_thread(self.repository.cleanup, datetime.now(timezone.utc))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning('webhook.handoff.queue_runtime_failed', exc_info=True)
                await asyncio.sleep(600 if self.ready else 30)
        finally:
            self.ready = False

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.repository is not None:
            try:
                await asyncio.to_thread(self.repository.close)
            except Exception:
                logger.warning('webhook.handoff.queue_close_failed', exc_info=True)
            self.repository = None
        self.service = None
        self.ready = False

@lru_cache
def get_handoff_queue_runtime():
    return HandoffQueueRuntime()
