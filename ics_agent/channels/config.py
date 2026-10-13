"""从项目数据目录读取渠道资源映射，不修改进程级共享配置。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_CONFIG_PATH = PROJECT_ROOT / "database/channels.json"
DEFAULT_CHANNEL = "xingliao"


@dataclass(frozen=True)
class ChannelConfig:
    key: str
    name: str
    faq_table: str
    kb_collection: str
    prompt_name: str
    faq_workbook: str
    knowledge_dir: str
    chunks_path: str
    prompt_path: str
    website_token: str = ""
    inboxes: tuple[tuple[str, str], ...] = ()


@lru_cache(maxsize=1)
def list_channels() -> tuple[ChannelConfig, ...]:
    """配置在进程内保持一致，变更渠道引用或 Inbox 映射后重启服务。"""
    payload = json.loads(CHANNEL_CONFIG_PATH.read_text(encoding="utf-8"))
    channels: list[ChannelConfig] = []
    keys: set[str] = set()
    bindings: set[tuple[str, str]] = set()
    resources: set[tuple[str, str]] = set()
    for item in payload["channels"]:
        key = str(item["key"]).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in keys:
            raise ValueError(f"Invalid or duplicate channel key: {key}")
        keys.add(key)
        for field in ("faq_table", "kb_collection", "prompt_name"):
            value = str(item[field])
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", value):
                raise ValueError(f"Invalid {field} for channel {key}")
            resource = (field, value)
            if resource in resources:
                raise ValueError(f"Duplicate channel resource: {value}")
            resources.add(resource)
        inboxes: list[tuple[str, str]] = []
        for inbox in item.get("inboxes", []):
            binding = (str(inbox["account_id"]), str(inbox["inbox_id"]))
            if not all(value.isdecimal() and int(value) > 0 for value in binding):
                raise ValueError(f"Invalid Chatwoot inbox binding for {key}")
            if binding in bindings:
                raise ValueError(f"Duplicate Chatwoot inbox binding: {binding}")
            bindings.add(binding)
            inboxes.append(binding)
        channels.append(ChannelConfig(
            key=key, name=item["name"], faq_table=item["faq_table"],
            kb_collection=item["kb_collection"], prompt_name=item["prompt_name"],
            faq_workbook=item["faq_workbook"], knowledge_dir=item["knowledge_dir"],
            chunks_path=item["chunks_path"], prompt_path=item["prompt_path"],
            website_token=str(item.get("website_token", "")).strip(),
            inboxes=tuple(inboxes),
        ))
    if DEFAULT_CHANNEL not in keys:
        raise ValueError("The default xingliao channel must be configured.")
    return tuple(channels)


def get_channel(channel_key: str | None = None) -> ChannelConfig:
    key = DEFAULT_CHANNEL if channel_key is None else channel_key.strip()
    for channel in list_channels():
        if channel.key == key:
            return channel
    raise ValueError(f"Unknown channel: {key}")


def resolve_chatwoot_channel(account_id: str, inbox_id: int | None) -> str:
    binding = (str(account_id), str(inbox_id))
    for channel in list_channels():
        if binding in channel.inboxes:
            return channel.key
    # 保持现有 Inbox 行为；新 Inbox 须在配置中明确绑定渠道后再接入。
    return DEFAULT_CHANNEL


def channel_thread_id(thread_id: str | None, channel_key: str | None) -> str | None:
    """保留兴聊原有记忆 ID，其他渠道增加命名空间，避免 API 重用 ID 串话。"""
    key = get_channel(channel_key).key
    if not thread_id or key == DEFAULT_CHANNEL:
        return thread_id
    prefix = f"channel:{key}:"
    return thread_id if thread_id.startswith(prefix) else f"{prefix}{thread_id}"
