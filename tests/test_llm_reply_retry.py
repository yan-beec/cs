import unittest
from unittest.mock import AsyncMock, patch

from ics_agent.rag.graph import nodes


class _LLMResponse:
    def __init__(self, content: object) -> None:
        self.content = content


class LLMReplyRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_llm_reply_retries_with_backoff_then_succeeds(self) -> None:
        call_cloud_llm = AsyncMock(
            side_effect=[
                RuntimeError("temporary outage"),
                RuntimeError("still unavailable"),
                _LLMResponse("重连后回复成功"),
            ]
        )
        sleep = AsyncMock()

        with (
            patch.object(nodes, "call_cloud_llm", call_cloud_llm),
            patch("ics_agent.retry.reconnect.asyncio.sleep", sleep),
        ):
            result = await nodes.llm_reply_node(
                {
                    "query": "怎么提现？",
                    "intent_label": "提现",
                    "reranked_documents": [],
                }
            )

        self.assertEqual(result["answer"], "重连后回复成功")
        self.assertEqual(call_cloud_llm.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [1.0, 3.0],
        )

    async def test_empty_replies_retry_then_return_maintenance_fallback(self) -> None:
        responses = {
            "empty_string": _LLMResponse(""),
            "whitespace": _LLMResponse(" \t\r\n\u3000"),
            "empty_content_blocks": _LLMResponse([]),
            "null_content": _LLMResponse(None),
            "null_response": None,
            "non_text_content_blocks": _LLMResponse(
                [{"type": "tool_use", "id": "call-1", "name": "lookup"}]
            ),
            "empty_text_content_blocks": _LLMResponse(
                [{"type": "text", "text": "  "}, {"type": "text", "text": None}]
            ),
        }

        for case, response in responses.items():
            with self.subTest(case=case):
                call_cloud_llm = AsyncMock(return_value=response)
                sleep = AsyncMock()
                with (
                    patch.object(nodes, "call_cloud_llm", call_cloud_llm),
                    patch("ics_agent.retry.reconnect.asyncio.sleep", sleep),
                ):
                    result = await nodes.llm_reply_node(
                        {
                            "query": "怎么提现？",
                            "intent_label": "提现",
                            "reranked_documents": [],
                        }
                    )

                self.assertEqual(
                    result["answer"],
                    "当前智能客服正在维护，请您发送「转人工」申请人工客服协助！",
                )
                self.assertIsNone(result["llm_response"])
                self.assertIn("empty reply", result["error"])
                self.assertEqual(call_cloud_llm.await_count, 3)
                self.assertEqual(
                    [call.args[0] for call in sleep.await_args_list],
                    [1.0, 3.0],
                )

    async def test_empty_reply_can_recover_on_retry(self) -> None:
        response = _LLMResponse("重试后回复成功")
        call_cloud_llm = AsyncMock(
            side_effect=[_LLMResponse(" \n"), response]
        )
        sleep = AsyncMock()

        with (
            patch.object(nodes, "call_cloud_llm", call_cloud_llm),
            patch("ics_agent.retry.reconnect.asyncio.sleep", sleep),
        ):
            result = await nodes.llm_reply_node(
                {
                    "query": "怎么提现？",
                    "intent_label": "提现",
                    "reranked_documents": [],
                }
            )

        self.assertEqual(result["answer"], "重试后回复成功")
        self.assertIs(result["llm_response"], response)
        self.assertNotIn("error", result)
        self.assertEqual(call_cloud_llm.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_valid_text_blocks_return_answer_without_retry(self) -> None:
        response = _LLMResponse(
            [
                {"type": "text", "text": "第一步：打开设置。"},
                {"type": "tool_use", "id": "call-1", "name": "lookup"},
                "第二步：选择账号安全。",
            ]
        )
        call_cloud_llm = AsyncMock(return_value=response)
        sleep = AsyncMock()

        with (
            patch.object(nodes, "call_cloud_llm", call_cloud_llm),
            patch("ics_agent.retry.reconnect.asyncio.sleep", sleep),
        ):
            result = await nodes.llm_reply_node(
                {
                    "query": "如何修改手机号？",
                    "intent_label": "账号",
                    "reranked_documents": [],
                }
            )

        self.assertEqual(
            result["answer"],
            "第一步：打开设置。\n第二步：选择账号安全。",
        )
        self.assertIs(result["llm_response"], response)
        self.assertNotIn("error", result)
        call_cloud_llm.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_llm_reply_returns_fallback_after_retry_exhausted(self) -> None:
        call_cloud_llm = AsyncMock(side_effect=RuntimeError("provider down"))
        sleep = AsyncMock()

        with (
            patch.object(nodes, "call_cloud_llm", call_cloud_llm),
            patch("ics_agent.retry.reconnect.asyncio.sleep", sleep),
        ):
            result = await nodes.llm_reply_node(
                {
                    "query": "怎么提现？",
                    "intent_label": "提现",
                    "reranked_documents": [],
                }
            )

        self.assertEqual(result["answer"], nodes.LLM_REPLY_FALLBACK_ANSWER)
        self.assertIsNone(result["llm_response"])
        self.assertEqual(call_cloud_llm.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [1.0, 3.0],
        )


if __name__ == "__main__":
    unittest.main()
