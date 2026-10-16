"""Cached Chatwoot directories, with their own bounded execution capacity."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

from ics_agent.app.chatwoot.client import ChatwootClient
from ics_agent.channels import list_channels


class ChatwootDirectory:
    REFRESH_SECONDS = 300
    RETRY_SECONDS = 60
    REQUEST_TIMEOUT_SECONDS = 2
    RESPONSE_MAX_BYTES = 2 * 1024 * 1024
    MAX_ITEMS = 2000
    WAIT_SECONDS = 5

    def __init__(self, source_key, settings, *, client=None, clock=time.monotonic):
        self.source_key = source_key
        self.account_id = self._positive_id(settings.chatwoot_account_id)
        self.configured = bool(self.account_id and source_key and (
            settings.chatwoot_contact_api_token.strip() or settings.chatwoot_api_token.strip()
        ))
        self.client = client or ChatwootClient(settings)
        self.clock = clock
        self.next_refresh = 0
        self.executor = None
        self.refresh = None
        self.cached = {"agents": [], "inboxes": []}
        self.status = "unconfigured" if not self.configured else "unavailable"

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
            name = str(item.get("name") or item.get("available_name") or f"#{identifier}")[:200]
            if kind == "agents":
                normalized[identifier] = {**scope, "agent_id": str(identifier), "agent_name": name}
            else:
                normalized[identifier] = {**scope, "inbox_id": identifier, "name": name,
                                          "channel_key": bindings.get(identifier)}
        return list(normalized.values())

    async def enrich(self, options):
        if self.configured and self.refresh is None and self.clock() >= self.next_refresh:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quality-directory")
            # One refresh is shared by every concurrent request. No unbounded executor queue.
            self.refresh = asyncio.get_running_loop().run_in_executor(self.executor, self._fetch)
            self.next_refresh = self.clock() + self.RETRY_SECONDS
        if self.refresh is not None:
            pending = self.refresh
            try:
                result = await asyncio.wait_for(asyncio.shield(pending), timeout=self.WAIT_SECONDS)
            except asyncio.TimeoutError:
                self.status = "stale" if any(self.cached.values()) else "unavailable"
            else:
                for kind, items in result.items():
                    if items is not None:
                        self.cached[kind] = items
                success = all(items is not None for items in result.values())
                self.status = "ready" if success else "stale" if any(self.cached.values()) else "unavailable"
                self.next_refresh = self.clock() + (self.REFRESH_SECONDS if success else self.RETRY_SECONDS)
                if self.refresh is pending:
                    self.refresh = None
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
        if self.refresh is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.refresh), timeout=self.WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.refresh = None
        if self.executor is not None:
            self.executor.shutdown(wait=False)
            self.executor = None
