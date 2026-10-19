"""Optional query resources with bounded threads and independent recovery."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import logging
import threading

from .config import QuerySettings
from .agents import agent_name_index, archived_agent_name, enrich_round_agents
from .repository import QueryRepository
from .export import ExportCancelled, ExportFile, ExportNotFound, ExportRepository, ExportTooLarge

logger = logging.getLogger(__name__)


class ArchiveUnavailable(RuntimeError):
    pass


class ExportBusy(RuntimeError):
    pass


class QueryRuntime:
    def __init__(self):
        self.enabled = False
        self.ready = False
        self.status = "disabled"
        self.repository = None
        self.executor = None
        self.task = None
        self.slots = None
        self.settings = None
        self.export_repository = None
        self.export_executor = None
        self.export_slot = None
        self.export_cancellations = set()
        self.directory = None

    async def start(self):
        if self.task is not None:
            return
        try:
            self.settings = QuerySettings()
            self.enabled = self.settings.query_enabled
            if not self.enabled:
                return
            # Validate credentials before creating resources, without logging their values.
            self.settings.connection_kwargs
        except Exception as error:
            self.status = "configuration_error"
            logger.error("Archive query configuration unavailable: %s", type(error).__name__)
            return
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quality-query")
        self.slots = asyncio.Semaphore(2)
        self.status = "connecting"
        self.task = asyncio.create_task(self._recover(), name="quality-query-recovery")

    async def _execute(self, function, *args, **kwargs):
        from functools import partial

        if self.executor is None or self.slots is None:
            raise ArchiveUnavailable()
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.2)
        except asyncio.TimeoutError:
            raise ArchiveUnavailable("Archive query capacity is busy") from None
        future = asyncio.get_running_loop().run_in_executor(self.executor, partial(function, *args, **kwargs))
        # Cancellation must not release the concurrency slot while the DB thread still runs.
        slots = self.slots
        future.add_done_callback(lambda _: slots.release())
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.gather(future, return_exceptions=True)
            raise

    async def _recover(self):
        try:
            while True:
                try:
                    if self.repository is None:
                        repository = QueryRepository(self.settings)
                        try:
                            await self._execute(repository.open)
                        except BaseException:
                            # Closing pool threads may wait for a failed connection attempt.
                            # Keep that wait off the shared FastAPI event loop as well.
                            await self._execute(repository.close)
                            raise
                        self.repository = repository
                    else:
                        await self._execute(self.repository.ping)
                    self.ready = True
                    self.status = "ready"
                except asyncio.CancelledError:
                    raise
                except ArchiveUnavailable:
                    # A busy query executor is not a failed database probe.
                    # Keep serving after existing requests release their slots.
                    pass
                except Exception as error:
                    self.ready = False
                    self.status = "unavailable"
                    logger.warning("Archive query dependencies unavailable: %s", type(error).__name__)
                await asyncio.sleep(10)
        finally:
            self.ready = False

    async def call(self, method: str, *args, **kwargs):
        if not self.ready or self.repository is None:
            raise ArchiveUnavailable()
        try:
            result = await self._execute(getattr(self.repository, method), *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except ArchiveUnavailable:
            raise
        except Exception as error:
            self.ready = False
            self.status = "unavailable"
            logger.warning("Archive query failed: %s", type(error).__name__)
            raise ArchiveUnavailable() from None
        if method == "filter_options":
            return await self._directory_options(result)
        if method in {"list_rounds", "round_detail"} and result and ("items" in result or "round" in result):
            options = await self._directory_options({})
            names = agent_name_index(options.get("agents", []))
            if method == "list_rounds":
                result = {**result, "items": [enrich_round_agents(row, names) for row in result.get("items", [])]}
            else:
                result = {**result, "round": enrich_round_agents(result["round"], names)}
        if method == "overview":
            options = await self._directory_options({})
            names = agent_name_index(options.get("agents", []))
            agents = []
            for row in result.get("participating_agents", []):
                key = tuple(str(row.get(field)) for field in ("source_key", "account_id", "agent_id"))
                name = names.get(key)
                agents.append({**row, "agent_name": name or archived_agent_name(row.get("agent_name"), row.get("agent_id")),
                               "agent_name_source": "chatwoot" if name else "archive"})
            result = {**result, "participating_agents": agents}
        return result

    def get_directory(self):
        if self.directory is None:
            from ics_agent.app.core.config import get_settings
            from .directory import ChatwootDirectory

            source_key = self.settings.source_key if self.settings is not None else QuerySettings().source_key
            self.directory = ChatwootDirectory(source_key, get_settings())
        return self.directory

    async def directory_presence(self, *, force_refresh=False):
        try:
            return await self.get_directory().presence(force_refresh=force_refresh)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Archive presence directory unavailable: %s", type(error).__name__)
            return {"status": "unavailable", "sampled_at": None, "agents": []}

    async def presence(self):
        from quality.presence.runtime import get_presence_runtime

        return {**await self.directory_presence(), "collector": get_presence_runtime().state()}

    async def presence_daily(self, day=None, *, include_hidden=False):
        from quality.presence.runtime import get_presence_runtime

        return await get_presence_runtime().daily(day, **({"include_hidden": True} if include_hidden else {}))

    async def presence_agents(self, page=1, page_size=50, include_hidden=False):
        from quality.presence.runtime import get_presence_runtime

        return await get_presence_runtime().agents(page, page_size, include_hidden)

    async def presence_remove_agent(self, source_key, account_id, agent_id):
        from quality.presence.runtime import get_presence_runtime

        return await get_presence_runtime().remove_agent(source_key, account_id, agent_id)

    async def presence_timeline(self, day, agent_id):
        from quality.presence.runtime import get_presence_runtime

        return await get_presence_runtime().timeline(day, agent_id)

    async def _directory_options(self, options):
        try:
            return await self.get_directory().enrich(options)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Directory failure affects names, never archive query readiness.
            logger.warning("Archive directory unavailable: %s", type(error).__name__)
            return {**options, "directory": {"status": "unavailable"},
                    "directory_warning": "Chatwoot 目录暂不可用，筛选暂使用已归档的坐席和收件箱。"}

    async def export(self, mode, filters, **scope) -> ExportFile:
        if not self.ready or self.settings is None:
            raise ArchiveUnavailable()
        # Allocate lazily and use one dedicated export thread/connection. Large
        # downloads never consume the interactive query slots or the ICS pool.
        if self.export_executor is None:
            self.export_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-export")
            self.export_slot = asyncio.Semaphore(1)
        try:
            await asyncio.wait_for(self.export_slot.acquire(), timeout=0.2)
        except asyncio.TimeoutError:
            raise ExportBusy() from None
        cancelled = threading.Event()
        self.export_cancellations.add(cancelled)

        def work():
            if self.export_repository is None:
                repository = ExportRepository(self.settings)
                try:
                    repository.open()
                except BaseException:
                    repository.close()
                    raise
                self.export_repository = repository
            return self.export_repository.create_export(mode, filters, cancelled=cancelled, **scope)

        future = asyncio.get_running_loop().run_in_executor(self.export_executor, work)
        slot = self.export_slot
        def finished(_):
            slot.release()
            self.export_cancellations.discard(cancelled)
        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled.set()
            results = await asyncio.gather(future, return_exceptions=True)
            if results and isinstance(results[0], ExportFile):
                results[0].cleanup()
            raise
        except (ExportTooLarge, ExportNotFound):
            raise
        except Exception as error:
            # Export failures are local to this request, not query health failures.
            logger.warning("Archive export failed: %s", type(error).__name__)
            raise ArchiveUnavailable() from None

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.directory is not None:
            await self.directory.close()
            self.directory = None
        for cancelled in self.export_cancellations:
            cancelled.set()
        if self.export_executor:
            await asyncio.to_thread(self.export_executor.shutdown, wait=True, cancel_futures=True)
            self.export_executor = None
        if self.export_repository:
            await asyncio.to_thread(self.export_repository.close)
            self.export_repository = None
        self.export_slot = None
        if self.executor:
            # SQL/connect/pool waits are bounded. Drain running queries before closing the pool.
            await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
            self.executor = None
        if self.repository:
            await asyncio.to_thread(self.repository.close)
            self.repository = None
        self.slots = None
        self.ready = False


@lru_cache
def get_query_runtime() -> QueryRuntime:
    return QueryRuntime()
