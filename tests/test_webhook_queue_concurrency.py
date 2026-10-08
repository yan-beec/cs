import asyncio
import unittest
from unittest.mock import Mock, patch

from ics_agent.app.chatwoot.jobs import WebhookJob, WebhookJobQueue
from ics_agent.rag.config import RAGSettings


class WebhookQueueConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_different_conversations_are_not_blocked_by_same_key_waiter(
        self,
    ) -> None:
        long_started = asyncio.Event()
        release_long = asyncio.Event()
        other_started = asyncio.Event()
        calls: list[str] = []

        async def processor(job: WebhookJob) -> dict[str, object]:
            calls.append(job.event_key)
            if job.event_key == "long":
                long_started.set()
                await release_long.wait()
            if job.event_key == "other":
                other_started.set()
            return {"event_key": job.event_key}

        repository = Mock()
        settings = RAGSettings(
            _env_file=None,
            webhook_worker_count=2,
            webhook_queue_max_size=10,
            webhook_queue_shutdown_timeout_seconds=1,
        )
        queue = WebhookJobQueue(
            processor=processor,
            repository=repository,
        )

        with patch(
            "ics_agent.app.chatwoot.jobs.get_rag_settings",
            return_value=settings,
        ):
            await queue.start()
            queue.enqueue_nowait(
                WebhookJob(
                    event_key="long",
                    event={},
                    partition_key="account:conversation-1",
                )
            )
            await asyncio.wait_for(long_started.wait(), timeout=1)

            queue.enqueue_nowait(
                WebhookJob(
                    event_key="same-key",
                    event={},
                    partition_key="account:conversation-1",
                )
            )
            queue.enqueue_nowait(
                WebhookJob(
                    event_key="other",
                    event={},
                    partition_key="account:conversation-2",
                )
            )

            await asyncio.wait_for(other_started.wait(), timeout=1)
            self.assertEqual(calls[:2], ["long", "other"])

            release_long.set()
            await queue.stop()

        repository.mark_processing.assert_any_call("long")
        repository.mark_processing.assert_any_call("same-key")
        repository.mark_processing.assert_any_call("other")


if __name__ == "__main__":
    unittest.main()
