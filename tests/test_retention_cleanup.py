"""Retention cleanup must keep up with daily volume instead of deleting one batch per day."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ics_agent.app.chatwoot.jobs import WebhookJobQueue
from ics_agent.rag.memory.manager import ConversationMemoryManager


def test_webhook_idempotency_cleanup_drains_until_a_partial_batch():
    queue = WebhookJobQueue.__new__(WebhookJobQueue)
    queue._repository = Mock()
    queue._repository.cleanup_expired.side_effect = [1000, 1000, 1000, 7]
    settings = SimpleNamespace(webhook_idempotency_cleanup_batch_size=1000,
                               webhook_idempotency_retention_days=7)
    with patch("ics_agent.app.chatwoot.jobs.asyncio.sleep", new=AsyncMock()):
        deleted = asyncio.run(queue._drain_expired(settings))
    assert deleted == 3007
    assert queue._repository.cleanup_expired.call_count == 4


def test_webhook_idempotency_cleanup_runs_once_at_startup_before_waiting():
    queue = WebhookJobQueue.__new__(WebhookJobQueue)
    queue._drain_expired = AsyncMock(return_value=0)
    settings = SimpleNamespace(webhook_idempotency_cleanup_interval_seconds=86400)

    async def stop(*_):
        raise asyncio.CancelledError

    with patch("ics_agent.app.chatwoot.jobs.get_rag_settings", return_value=settings), \
            patch("ics_agent.app.chatwoot.jobs.asyncio.sleep", side_effect=stop):
        try:
            asyncio.run(queue._cleanup_loop())
        except asyncio.CancelledError:
            pass
    queue._drain_expired.assert_awaited_once()


def test_memory_cleanup_drains_full_batches_and_stops_on_a_short_or_failed_batch():
    manager = ConversationMemoryManager()
    manager.cleanup_expired = AsyncMock(side_effect=[500, 500, 120])
    settings = SimpleNamespace(rag_memory_cleanup_batch_size=500)
    with patch("ics_agent.rag.memory.manager.get_rag_settings", return_value=settings), \
            patch("ics_agent.rag.memory.manager.asyncio.sleep", new=AsyncMock()):
        assert asyncio.run(manager._drain_expired()) == 1120
    assert manager.cleanup_expired.await_count == 3
