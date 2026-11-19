"""Minute observations with one DB worker and a strictly bounded retry buffer."""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from functools import lru_cache, partial
import logging
from zoneinfo import ZoneInfo

from .config import PresenceSettings
from .retention import CLEANUP_BATCH_SIZE, CLEANUP_MAX_BATCHES, retention_metadata

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")


class PresenceBusy(RuntimeError):
    pass


class PresenceRuntime:
    PERIOD_SECONDS = 60
    MAX_PENDING_BATCHES = 5

    def __init__(self):
        self.enabled = False
        self.status = "disabled"
        self.settings = None
        self.query_runtime = None
        self.repository = None
        self.executor = None
        self.slot = None
        self.task = None
        self.pending = deque()
        self.last_saved_at = None
        self.last_observed_minute = None
        self.dropped_batches = 0
        self.cleanup_status = "disabled"
        self.cleanup_day = None
        self.cleanup_batches = 0
        self.cleanup_deleted_samples = 0
        self.cleanup_deleted_periods = 0
        self.cleanup_last_success_at = None
        self.cleanup_cutoff_at = None
        self._collector_pressure = False

    def state(self):
        return {"enabled": self.enabled, "status": self.status,
                "last_saved_at": self.last_saved_at, "pending_batches": len(self.pending),
                "dropped_batches": self.dropped_batches,
                "cleanup": {"status": self.cleanup_status,
                            "day": self.cleanup_day.isoformat() if self.cleanup_day else None,
                            "batches": self.cleanup_batches,
                            "deleted_samples": self.cleanup_deleted_samples,
                            "deleted_periods": self.cleanup_deleted_periods,
                            "last_success_at": self.cleanup_last_success_at,
                            "cutoff_at": self.cleanup_cutoff_at}}

    def _status(self, value):
        if value != self.status and value not in {"ready", "disabled", "connecting"}:
            logger.warning("Archive presence collector status: %s", value)
        self.status = value

    async def start(self, query_runtime):
        if self.task is not None:
            return
        self.query_runtime = query_runtime
        try:
            self.settings = PresenceSettings()
            self.enabled = self.settings.presence_enabled
            if not self.enabled:
                return
            self.settings.connection_kwargs
        except Exception as error:
            self._status("configuration_error")
            logger.warning("Archive presence configuration unavailable: %s", type(error).__name__)
            return
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-presence")
        self.slot = asyncio.Semaphore(1)
        self.status = "connecting"
        self.cleanup_status = "waiting"
        self.task = asyncio.create_task(self._run(), name="quality-presence-collector")

    async def _execute(self, function, *args, wait_seconds=0.2, **kwargs):
        if self.executor is None or self.slot is None:
            raise PresenceBusy()
        try:
            await asyncio.wait_for(self.slot.acquire(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            raise PresenceBusy() from None
        future = asyncio.get_running_loop().run_in_executor(self.executor, partial(function, *args, **kwargs))
        slot = self.slot
        future.add_done_callback(lambda _: slot.release())
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Do not release capacity while a cancelled HTTP request's DB work runs.
            await asyncio.gather(future, return_exceptions=True)
            raise

    def _repository_call(self, method, *args, **kwargs):
        if self.repository is None:
            from .repository import PresenceRepository

            repository = PresenceRepository(self.settings)
            try:
                repository.open()
            except BaseException:
                repository.close()
                raise
            self.repository = repository
        return getattr(self.repository, method)(*args, **kwargs)

    def _enqueue(self, snapshot):
        if snapshot.get("status") != "ready":
            return False
        rows = snapshot.get("agents")
        if not isinstance(rows, list) or len(rows) > self.settings.presence_max_agents:
            self._status("capacity_exceeded")
            return False
        sampled = datetime.fromisoformat(snapshot["sampled_at"])
        if sampled.tzinfo is None:
            raise ValueError("Observation must carry a timezone")
        minute = sampled.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if self.last_observed_minute is not None and minute <= self.last_observed_minute:
            return False
        # Only retain normalized fields, bounded by both agent and batch limits.
        fields = ("source_key", "account_id", "agent_id", "agent_name", "agent_name_source", "availability_status")
        batch = {key: snapshot[key] for key in ("status", "source_key", "account_id", "sampled_at")}
        batch["agents"] = [{key: row.get(key) for key in fields} for row in rows]
        if len(self.pending) >= self.MAX_PENDING_BATCHES:
            self.pending.popleft()
            self.dropped_batches += 1
            logger.warning("Archive presence retry buffer full; oldest observation left as a visible gap")
        self.pending.append(batch)
        self.last_observed_minute = minute
        return True

    async def _flush(self, *, max_batches=MAX_PENDING_BATCHES):
        processed = 0
        while self.pending and processed < max_batches:
            batch = self.pending[0]
            result = await self._execute(self._repository_call, "append", batch, wait_seconds=1)
            self.pending.popleft()
            self.last_saved_at = result.get("sampled_at", batch["sampled_at"])
            processed += 1

    async def _tick(self):
        started = asyncio.get_running_loop().time()
        self._collector_pressure = False
        observed_status = "upstream_unavailable"
        try:
            snapshot = await self.query_runtime.directory_presence(force_refresh=True)
            if snapshot.get("status") == "unconfigured":
                observed_status = "unconfigured"
            elif snapshot.get("status") == "ready":
                accepted = self._enqueue(snapshot)
                observed_status = "ready" if accepted or self.status != "capacity_exceeded" else "capacity_exceeded"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Archive presence observation unavailable: %s", type(error).__name__)
        # An oversized upstream roster does not consume DB capacity and must
        # not indefinitely block cleanup of already persisted observations.
        self._collector_pressure = len(self.pending) > 1
        try:
            await self._flush()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._status("database_unavailable")
            logger.warning("Archive presence persistence unavailable: %s", type(error).__name__)
            return
        self._status(observed_status)
        self._collector_pressure = self._collector_pressure or (
            asyncio.get_running_loop().time() - started >= self.PERIOD_SECONDS / 2)
        await self._maybe_cleanup(datetime.now(timezone.utc))

    async def _maybe_cleanup(self, now):
        """Use one idle slot; the DB owns schedule, cutoff and daily budget."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Presence cleanup requires a timezone-aware datetime")
        if not self.enabled:
            self.cleanup_status = "disabled"
            return
        if self.pending or self._collector_pressure:
            self.cleanup_status = "deferred"
            return
        if self.query_runtime is None or self.executor is None or self.slot is None:
            self.cleanup_status = "unavailable"
            return
        try:
            directory = self.query_runtime.get_directory()
            if not directory.configured:
                self.cleanup_status = "unconfigured"
                return
            self.cleanup_status = "running"
            result = await self._execute(self._repository_call, "cleanup",
                                         self.settings.source_key, directory.account_id)
            deleted_samples, deleted_periods = result["deleted_samples"], result["deleted_periods"]
            if any(type(count) is not int or not 0 <= count <= CLEANUP_BATCH_SIZE
                   for count in (deleted_samples, deleted_periods)):
                raise ValueError("Presence cleanup returned an invalid batch count")
            if result.get("cleanup_status") not in {"waiting", "pending", "completed", "limited"}:
                raise ValueError("Presence cleanup returned an invalid state")
            batches = result["batches"]
            totals = (result["total_deleted_samples"], result["total_deleted_periods"])
            if type(batches) is not int or not 0 <= batches <= CLEANUP_MAX_BATCHES or any(
                type(count) is not int or not 0 <= count <= batches * CLEANUP_BATCH_SIZE for count in totals
            ) or type(result["cleanup_day"]) is not date:
                raise ValueError("Presence cleanup returned an invalid durable budget")
        except asyncio.CancelledError:
            self.cleanup_status = "cancelled"
            raise
        except PresenceBusy:
            self.cleanup_status = "busy"
            return
        except Exception as error:
            self.cleanup_status = "failed"
            logger.warning("Archive presence cleanup unavailable: %s", type(error).__name__)
            return
        self.cleanup_day = result["cleanup_day"]
        self.cleanup_batches = batches
        self.cleanup_deleted_samples, self.cleanup_deleted_periods = totals
        self.cleanup_last_success_at = result["last_success_at"]
        self.cleanup_cutoff_at = result["cutoff_at"]
        self.cleanup_status = result["cleanup_status"]

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._status("unavailable")
                logger.warning("Archive presence tick unavailable: %s", type(error).__name__)
            next_tick += self.PERIOD_SECONDS
            if next_tick <= loop.time():
                # Recover without a burst of catch-up requests or fabricated samples.
                next_tick = loop.time() + self.PERIOD_SECONDS
            await asyncio.sleep(max(0, next_tick - loop.time()))

    async def daily(self, day: date | None = None, *, include_hidden=False):
        return await self._history("daily", day, **({"include_hidden": True} if include_hidden else {}))

    async def agents(self, page=1, page_size=50, include_hidden=False):
        return await self._management("list_agents", page=page, page_size=page_size, include_hidden=include_hidden)

    async def remove_agent(self, source_key, account_id, agent_id):
        return await self._management("remove_agent", agent_id,
                                      expected_scope=(source_key, account_id), now=datetime.now(timezone.utc))

    async def _management(self, method, *args, expected_scope=None, **kwargs):
        base = {"agents": [], "collector": self.state()}
        if not self.enabled:
            return {**base, "status": "disabled"}
        if self.query_runtime is None or self.executor is None:
            return {**base, "status": "unavailable"}
        try:
            directory = self.query_runtime.get_directory()
            if not directory.configured:
                return {**base, "status": "unconfigured"}
            scope = (self.settings.source_key, directory.account_id)
            if expected_scope is not None and tuple(map(str, expected_scope)) != tuple(map(str, scope)):
                return {**base, "status": "scope_changed"}
            result = await self._execute(self._repository_call, method, *scope, *args, **kwargs)
            return {**base, **result}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Archive presence roster management unavailable: %s", type(error).__name__)
            return {**base, "status": "unavailable"}

    async def timeline(self, day: date | None, agent_id: int):
        return await self._history("timeline", day, agent_id)

    async def _history(self, method, day, *scope, **kwargs):
        now = datetime.now(timezone.utc)
        day = day or now.astimezone(BEIJING).date()
        metadata = retention_metadata(now)
        base = {**metadata, "day": day.isoformat(), "timezone": "Asia/Shanghai", "agents": [],
                "monitoring_started_at": None, "through_at": None, "collector": self.state()}
        if not self.enabled:
            return {**base, "status": "disabled"}
        if day < date.fromisoformat(metadata["oldest_available_day"]):
            return {**base, "status": "expired"}
        if self.query_runtime is None or self.executor is None:
            return {**base, "status": "unavailable"}
        try:
            directory = self.query_runtime.get_directory()
            if not directory.configured:
                return {**base, "status": "unconfigured"}
            result = await self._execute(self._repository_call, method, day,
                                         self.settings.source_key, directory.account_id, *scope,
                                         now, **kwargs)
            return {**base, **result, "collector": self.state()}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Archive presence history query unavailable: %s", type(error).__name__)
            return {**base, "status": "unavailable"}

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.pending and self.executor is not None:
            try:
                # Shutdown never drains five delayed batches through a slow DB.
                await self._flush(max_batches=1)
            except Exception as error:
                logger.warning("Archive presence final persistence unavailable: %s", type(error).__name__)
        if self.executor is not None:
            await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
            self.executor = None
        if self.repository is not None:
            await asyncio.to_thread(self.repository.close)
            self.repository = None
        if self.pending:
            self.dropped_batches += len(self.pending)
            logger.warning("Archive presence stopped with unsaved observations; daily coverage will show gaps")
            self.pending.clear()
        self.slot = None
        self.status = "stopped"
        self.cleanup_status = "stopped"


@lru_cache
def get_presence_runtime() -> PresenceRuntime:
    return PresenceRuntime()
