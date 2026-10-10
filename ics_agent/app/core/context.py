"""共享应用上下文：配置、日志、仓库和跨模块常量。"""

from __future__ import annotations

import logging

from .config import get_settings
from ics_agent.app.chatwoot.state import (
    get_contact_block_repository,
    get_conversation_control_repository,
)
from .logging_config import configure_logging, log_event
from ics_agent.app.chatwoot.jobs import get_webhook_idempotency_repository
from ics_agent.rag.config import get_rag_settings

settings = get_settings()
rag_settings = get_rag_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

webhook_repository = get_webhook_idempotency_repository()
conversation_control_repository = get_conversation_control_repository()
contact_block_repository = get_contact_block_repository()

EMOJI_ACK_MESSAGE = "如果有什么需要帮助，可以直接问我哦，我会尽力帮您解答的~"
HANDOFF_NOTICE_MESSAGE = "正在为您转接人工客服，请稍等..."
IMAGE_HANDOFF_NOTICE_MESSAGE = (
    "观察到用户需要人工协助，正在转接人工客服，请稍后"
)
HANDOFF_NO_AGENT_MESSAGE = (
    "当前人工坐席繁忙，您可以先继续描述问题，我会尽力帮您解答。"
    "如仍需人工客服，请稍后再次尝试「转人工」。"
)
MUTED_HANDOFF_MESSAGE = "非常抱歉，当前人工座席繁忙，请稍后再试。"
HANDOFF_RELEASE_STATUSES = {"resolved"}
AI_CHATWOOT_STATUS = "pending"
CHATWOOT_TYPING_HEARTBEAT_SECONDS = 20.0
CONVERSATION_STATUS_EVENTS = {
    "conversation.status_changed",
    "conversation.status.changed",
    "conversation.updated",
    "conversation.resolved",
    "conversation.pending",
    "conversation.opened",
    "conversation.reopened",
}
CONTACT_BLOCK_EVENTS = {"contact.updated"}

log_event(
    logger,
    logging.INFO,
    "app.startup",
    environment=settings.app_env,
    log_level=settings.log_level,
)
