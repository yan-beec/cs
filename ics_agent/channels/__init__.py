"""渠道配置：同一查询流程使用各自的 FAQ、知识库和提示词。"""

from .config import (
    DEFAULT_CHANNEL,
    ChannelConfig,
    channel_thread_id,
    get_channel,
    list_channels,
    resolve_chatwoot_channel,
)

__all__ = [
    "DEFAULT_CHANNEL", "ChannelConfig", "channel_thread_id", "get_channel",
    "list_channels", "resolve_chatwoot_channel",
]
