"""Cached Chatwoot directories, with their own bounded execution capacity."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import time

from ics_agent.app.chatwoot.client import ChatwootClient
from ics_agent.channels import list_channels


class ChatwootDirectory:
    REFRESH_SECONDS = 60
    RETRY_SECONDS = 60
    REQUEST_TIMEOUT_SECONDS = 2
    RESPONSE_MAX_BYTES = 2 * 1024 * 1024
    MAX_ITEMS = 2000
    WAIT_SECONDS = 5

    def __init__(self, source_key, settings, *, client=None, clock=time.monotonic, utcnow=None):
        self.source_key = source_key
        self.account_id = self._positive_id(settings.chatwoot_account_id)
        self.configured = bool(self.account_id and source_key and (
            settings.chatwoot_contact_api_token.strip() or settings.chatwoot_api_token.strip()
        ))
        self.client = client or ChatwootClient(settings)
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.next_refresh = 0
        self.executor = None
        self.refresh = None
        self.initialized = False
        self.closed = False
        self.cached = {"agents": [], "inboxes": []}
        self.status = "unconfigured" if not self.configured else "unavailable"
        self.agents_sampled_at = None
        self.agents_sampled_clock = None
        self.agents_fetch_succeeded = False

    @staticmethod
    def _positive_id(value):
        # bool is not an upstream resource identifier, despite being an int in Python.
        if isinstance(value, bool):
            return None
        try:
            identifier = int(str(value))
        except (ValueError, TypeError):
            return None
        return identifier if 0 < identifier <= 2**63 - 1 else None

    def _fetch(self):
        result = {}
        for kind, method in (("agents", self.client.list_agents), ("inboxes", self.client.list_inboxes)):
            try:
                items = method(account_id=str(self.account_id), timeout_seconds=self.REQUEST_TIMEOUT_SECONDS,
                               max_response_bytes=self.RESPONSE_MAX_BYTES, strict_response=True)
                if not isinstance(items, list) or len(items) > self.MAX_ITEMS:
                    raise ValueError("Invalid or oversized Chatwoot directory")
                result[kind] = self._normalize(kind, items)
                if kind == "agents":
                    # Capture the actual successful observation, before the independent inbox read.
                    result["agents_sampled_at"] = self.utcnow().astimezone(timezone.utc).isoformat()
                    result["agents_sampled_clock"] = self.clock()
            except Exception:
                # Preserve the last good directory; never return upstream errors, tokens or bodies.
                result[kind] = None
        return result

    def _normalize(self, kind, items):
        bindings = {int(inbox): channel.key for channel in list_channels()
                    for account, inbox in channel.inboxes if int(account) == self.account_id}
        normalized = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = self._positive_id(item.get("id"))
            if identifier is None:
                continue
            scope = {"source_key": self.source_key, "account_id": self.account_id}
            observed_name = next((value.strip() for value in (item.get("name"), item.get("available_name"))
                if isinstance(value, str) and value.strip()), None)
            name = (observed_name or f"#{identifier}")[:200]
            if kind == "agents":
                availability = item.get("availability_status") or item.get("availability")
                availability = availability.strip().lower() if isinstance(availability, str) else "unknown"
                normalized[identifier] = {**scope, "agent_id": str(identifier), "agent_name": name,
                    "agent_name_source": "chatwoot" if observed_name else "unobserved",
                    "availability_status": availability if availability in {"online", "busy", "offline"} else "unknown"}
            else:
                normalized[identifier] = {**scope, "inbox_id": identifier, "name": name,
                                          "channel_key": bindings.get(identifier)}
        return list(normalized.values())

    def _apply_refresh(self, pending):
        """只接收当前任务的结果；关闭后仍回收异常，避免遗留任务警告。"""
        try:
            result = pending.result() if not pending.cancelled() else None
        except Exception:
            result = None
        if self.closed or self.refresh is not pending:
            return
        result = result or {"agents": None, "inboxes": None}
        for kind in ("agents", "inboxes"):
            if result[kind] is not None:
                self.cached[kind] = result[kind]
                self.initialized = True
        self.agents_fetch_succeeded = result["agents"] is not None
        if self.agents_fetch_succeeded:
            self.agents_sampled_at = result["agents_sampled_at"]
            self.agents_sampled_clock = result["agents_sampled_clock"]
        success = all(result[kind] is not None for kind in ("agents", "inboxes"))
        self.status = "ready" if success else "stale" if self.initialized else "unavailable"
        self.next_refresh = (self.agents_sampled_clock + self.REFRESH_SECONDS
                             if self.agents_fetch_succeeded else self.clock() + self.RETRY_SECONDS)
        self.refresh = None

    async def _refresh_if_needed(self, *, force=False, wait=True):
        if self.closed:
            return
        if self.configured and self.refresh is None and (force or self.clock() >= self.next_refresh):
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-directory")
            # 页面与状态采集共享唯一刷新；结果独立于等待者写回。
            self.refresh = asyncio.get_running_loop().run_in_executor(self.executor, self._fetch)
            self.refresh.add_done_callback(self._apply_refresh)
            self.next_refresh = self.clock() + self.RETRY_SECONDS
        if wait and self.refresh is not None:
            pending = self.refresh
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=self.WAIT_SECONDS)
            except asyncio.TimeoutError:
                if not self.closed and self.refresh is pending:
                    self.status = "stale" if self.initialized else "unavailable"
                    self.agents_fetch_succeeded = False
            except Exception:
                # 刷新失败的状态由完成回调统一处理，查询仍返回已有目录。
                pass

    async def presence(self, *, force_refresh=False):
        """Return real agent observations; forcing never invents a new sample time.

        A history collector may bypass the cache, but still shares an in-flight
        request. It must deduplicate using sampled_at's minute, not its own clock.
        """
        await self._refresh_if_needed(force=force_refresh)
        if not self.configured:
            status = "unconfigured"
        elif self.agents_sampled_at is None:
            status = "unavailable"
        elif (not self.agents_fetch_succeeded
              or self.clock() - self.agents_sampled_clock >= self.REFRESH_SECONDS):
            status = "stale"
        else:
            status = "ready"
        fields = ("source_key", "account_id", "agent_id", "agent_name", "agent_name_source", "availability_status")
        return {
            "status": status, "source_key": self.source_key, "account_id": self.account_id,
            "sampled_at": self.agents_sampled_at,
            "agents": [{key: row[key] for key in fields} for row in sorted(
                self.cached["agents"], key=lambda item: (item["agent_name"], item["agent_id"]),
            )],
        }

    async def enrich(self, options):
        await self._refresh_if_needed(wait=not self.initialized)
        result = dict(options)
        sources = {(row["source_key"], row["account_id"]): dict(row) for row in options.get("sources", [])}
        if self.configured:
            sources[(self.source_key, self.account_id)] = {
                "source_key": self.source_key, "account_id": self.account_id,
            }
        result["sources"] = sorted(sources.values(), key=lambda row: (row["source_key"], row["account_id"]))
        for kind, id_field in (("agents", "agent_id"), ("inboxes", "inbox_id")):
            # Historical/deleted seats remain searchable. Live names take precedence only within their source.
            merged = {(row["source_key"], row["account_id"], str(row[id_field])): dict(row)
                      for row in options.get(kind, [])}
            for row in self.cached[kind]:
                merged[(row["source_key"], row["account_id"], str(row[id_field]))] = dict(row)
            result[kind] = sorted(merged.values(), key=lambda row: (
                row["source_key"], row["account_id"], row.get("agent_name") or row.get("name") or "",
                str(row[id_field]),
            ))
        result["directory"] = {"status": self.status}
        result["directory_warning"] = {
            "unconfigured": "未配置 Chatwoot 账户或用户 API Token，筛选暂使用已归档的坐席和收件箱。",
            "unavailable": "暂时无法读取 Chatwoot 坐席和收件箱，筛选暂使用已归档记录，请检查连接和用户 API Token 权限。",
            "stale": "部分 Chatwoot 目录暂未刷新，筛选使用最近成功同步的目录及已归档记录。",
        }.get(self.status)
        return result

    async def close(self):
        self.closed = True
        if self.refresh is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.refresh), timeout=self.WAIT_SECONDS)
            except Exception:
                pass
            self.refresh = None
        if self.executor is not None:
            self.executor.shutdown(wait=False)
            self.executor = None
