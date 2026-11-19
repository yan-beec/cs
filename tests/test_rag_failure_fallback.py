import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ics_agent.app.chatwoot import routes
from ics_agent.app.chatwoot.state import ConversationControl
from ics_agent.app.core.models import ChatwootWebhookEvent
from ics_agent.rag.graph import nodes
from ics_agent.rag.graph.nodes import LLM_REPLY_FALLBACK_ANSWER


def _control() -> ConversationControl:
    return ConversationControl(
        account_id="1",
        conversation_id="56",
        active_thread_id="chatwoot:1:56",
        last_chatwoot_status="pending",
        ai_enabled=True,
        handoff_required=False,
        handoff_reason=None,
        handoff_at=None,
        assigned_team_id=None,
        assigned_agent_id=None,
        last_user_message_id="1201",
        last_ai_message_id=None,
        last_handoff_message_id=None,
        handoff_conversation_updated=False,
        handoff_label_applied=False,
        handoff_note_created=False,
        handoff_assignment_applied=False,
        handoff_notice_sent=False,
    )


class RagFailureFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_llm_reply_reaches_webhook_as_maintenance_answer(self) -> None:
        async def run_mocked_rag_query(content, *, thread_id):
            return await nodes.llm_reply_node(
                {
                    "query": content,
                    "intent_label": "账号",
                    "reranked_documents": [],
                }
            )

        call_cloud_llm = AsyncMock(return_value=Mock(content=None))
        with (
            patch.object(routes, "_run_rag_query", new=run_mocked_rag_query),
            patch.object(nodes, "call_cloud_llm", call_cloud_llm),
            patch("ics_agent.retry.reconnect.asyncio.sleep", new=AsyncMock()),
        ):
            result = await routes._build_webhook_answer(
                content="如何修改手机号？",
                thread_id="chatwoot:1:56",
                event_key="empty-reply-1",
            )

        self.assertEqual(
            result["answer"],
            "不好意思，我刚刚卡了一下，没能及时回复您。您可以：\n"
            "1）重新发送；\n"
            "2）换个说法再问一次；\n"
            "3）回复“转人工”转接人工客服。\n"
            "感谢您的耐心～",
        )
        self.assertIsNone(result["llm_response"])
        self.assertEqual(call_cloud_llm.await_count, 2)

    async def test_build_webhook_answer_returns_maintenance_fallback(self) -> None:
        with patch.object(
            routes,
            "_run_rag_query",
            new=AsyncMock(side_effect=RuntimeError("retrieval unavailable")),
        ):
            result = await routes._build_webhook_answer(
                content="如何修改手机号？",
                thread_id="chatwoot:1:56",
                event_key="failure-1",
            )

        self.assertEqual(result["route"], "rag_fallback")
        self.assertEqual(result["faq_route"], "rag_fallback")
        self.assertEqual(result["answer"], LLM_REPLY_FALLBACK_ANSWER)
        self.assertIn("retrieval unavailable", result["rag_error"])

    async def test_stalled_rag_query_times_out_to_maintenance_fallback(self) -> None:
        async def stalled_query(*args, **kwargs):
            await asyncio.sleep(1)

        with (
            patch.object(routes, "rag_settings", Mock(webhook_rag_timeout_seconds=0.01)),
            patch.object(routes, "_run_rag_query", new=stalled_query),
        ):
            result = await routes._build_webhook_answer(
                content="如何修改手机号？",
                thread_id="chatwoot:1:56",
                event_key="timeout-1",
            )

        self.assertEqual(result["route"], "rag_fallback")
        self.assertEqual(result["faq_route"], "rag_fallback")
        self.assertEqual(result["answer"], LLM_REPLY_FALLBACK_ANSWER)
        self.assertIn("TimeoutError", result["rag_error"])

    async def test_failed_rag_webhook_sends_fallback_and_releases_job(self) -> None:
        control = _control()
        repository = Mock()
        repository.record_user_message = Mock(return_value=control)
        repository.get = Mock(return_value=control)
        repository.mark_ai_message = Mock()
        client = Mock()
        client.send_message.return_value = {"id": "reply-1"}
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1201,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "open"},
            message_type="incoming",
            content="如何修改手机号？",
        )

        with (
            patch.object(routes, "conversation_control_repository", repository),
            patch.object(
                routes,
                "_refresh_handoff_from_chatwoot_status",
                new=AsyncMock(return_value=control),
            ),
            patch.object(routes, "_can_auto_reply", return_value=True),
            patch.object(routes, "_set_ai_chatwoot_status", new=AsyncMock()),
            patch.object(routes, "_update_chatwoot_typing", new=AsyncMock()),
            patch.object(routes, "get_chatwoot_client", return_value=client),
            patch.object(
                routes,
                "_run_rag_query",
                new=AsyncMock(side_effect=RuntimeError("retrieval unavailable")),
            ),
        ):
            result = await routes._process_webhook_job_unlocked(
                routes.WebhookJob(
                    event_key="failure-2",
                    event=event,
                    partition_key="1:56",
                )
            )

        self.assertEqual(result["route"], "rag_fallback")
        self.assertTrue(result["chatwoot_reply"]["sent"])
        client.send_message.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            content=LLM_REPLY_FALLBACK_ANSWER,
        )
        repository.mark_ai_message.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            message_id="reply-1",
        )


if __name__ == "__main__":
    unittest.main()
