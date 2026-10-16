"""Recover a failed control-schema initialization without using the ICS pool."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging

from psycopg_pool import ConnectionPool

from ics_agent.faq.repositories import _PooledPostgresConnection
from .state import ConversationControlRepository

logger = logging.getLogger(__name__)
RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 60


def _recovery_pool(settings):
    common = {
        "connect_timeout": 3,
        "application_name": "ics-control-schema-recovery",
        "options": "-c statement_timeout=2000 -c lock_timeout=1000",
    }
    source = {"conninfo": settings.postgres_dsn, "kwargs": common} if settings.postgres_dsn else {
        "kwargs": {
            **common, "host": settings.postgres_host, "port": settings.postgres_port,
            "dbname": settings.postgres_database, "user": settings.postgres_user,
            "password": settings.postgres_password, "sslmode": settings.postgres_sslmode,
        },
    }
    return ConnectionPool(
        **source, min_size=0, max_size=1, timeout=1, max_waiting=1,
        num_workers=1, open=False,
    )


class _RecoveryConnections:
    def __init__(self, pool):
        self.pool = pool

    def acquire(self):
        return _PooledPostgresConnection(self.pool, self.pool.getconn(timeout=1))


class ConversationControlSchemaRecovery:
    """Allocate temporary resources only after the ordinary startup attempt fails."""

    def __init__(self):
        self.task = None
        self.executor = None
        self.pool = None
        self.status = "idle"

    async def start(self, settings):
        if self.task is not None:
            return
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ics-schema-recovery")
        self.status = "retrying"
        self.task = asyncio.create_task(self._run(settings), name="ics-control-schema-recovery")

    def _initialize(self, settings):
        if self.pool is None:
            pool = _recovery_pool(settings)
            try:
                pool.open(wait=False)
            except BaseException:
                pool.close()
                raise
            self.pool = pool
        ConversationControlRepository(_RecoveryConnections(self.pool)).ensure_table()

    def _close_pool(self):
        if self.pool is not None:
            try:
                self.pool.close()
            finally:
                self.pool = None

    async def _run(self, settings):
        failures = 0
        try:
            while True:
                await asyncio.sleep(min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(failures, 4)))
                future = asyncio.get_running_loop().run_in_executor(self.executor, self._initialize, settings)
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    # Finish or roll back the active transaction before closing its pool.
                    await asyncio.gather(future, return_exceptions=True)
                    raise
                except Exception as error:
                    failures += 1
                    logger.warning("Chatwoot control schema recovery unavailable: %s", type(error).__name__)
                else:
                    self.status = "ready"
                    logger.info("Chatwoot control schema recovered")
                    return
        finally:
            if self.status != "ready":
                self.status = "stopped"
            await self._release()

    async def _release(self):
        executor = self.executor
        if executor is None:
            return
        future = asyncio.get_running_loop().run_in_executor(executor, self._close_pool)
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.gather(future, return_exceptions=True)
            raise
        except Exception as error:
            logger.warning("Chatwoot control schema recovery cleanup unavailable: %s", type(error).__name__)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        # Cancellation before the task's first turn skips its finally block.
        await self._release()
        if self.status != "ready":
            self.status = "stopped"
