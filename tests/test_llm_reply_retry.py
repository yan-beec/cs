import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ics_agent.rag.graph import nodes


class _LLMResponse:
    def __init__(self, content: object) -> None:
        self.content = content


def _state() -> dict[str, object]:
    return {
        "query": "怎么提现？",
        "intent_label": "提现",
        "reranked_documents": [],
    }


class LLMReplyFallbackTest(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def _settings_patches(self, *, llm_timeout: float = 10.0):
        with (
            patch.object(
                nodes,
                "get_settings",
                return_value=SimpleNamespace(llm_timeout=llm_timeout),
            ),
            patch.object(
                nodes,
                "get_rag_settings",
                return_value=SimpleNamespace(webhook_rag_timeout_seconds=30.0),
            ),
        ):
            yield

    async def test_primary_success_does_not_call_fallback(self) -> None:
        response = _LLMResponse("主模型回复成功")
        call_cloud_llm = AsyncMock(return_value=response)
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches():
            result = await nodes.llm_reply_node(_state())

        self.assertEqual(result["answer"], "主模型回复成功")
        call_cloud_llm.assert_awaited_once()
        self.assertFalse(call_cloud_llm.await_args.kwargs["use_fallback_model"])
        self.assertEqual(call_cloud_llm.await_args.kwargs["timeout"], 10.0)
        self.assertEqual(call_cloud_llm.await_args.kwargs["max_retries"], 0)

    async def test_primary_error_immediately_calls_fallback_model(self) -> None:
        response = _LLMResponse("备用模型回复成功")
        call_cloud_llm = AsyncMock(
            side_effect=[RuntimeError("primary unavailable"), response]
        )
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches():
            result = await nodes.llm_reply_node(_state())

        self.assertEqual(result["answer"], "备用模型回复成功")
        self.assertEqual(call_cloud_llm.await_count, 2)
        first, second = call_cloud_llm.await_args_list
        self.assertFalse(first.kwargs["use_fallback_model"])
        self.assertTrue(second.kwargs["use_fallback_model"])
        self.assertEqual(first.kwargs["timeout"], 10.0)
        self.assertLessEqual(second.kwargs["timeout"], 30.1)
        self.assertEqual(second.kwargs["max_retries"], 0)

    async def test_primary_timeout_after_ten_seconds_switches_to_fallback(self) -> None:
        async def call_with_timeout(*args, **kwargs):
            if not kwargs["use_fallback_model"]:
                await asyncio.sleep(10)
            return _LLMResponse("备用模型及时回复")

        call_cloud_llm = AsyncMock(side_effect=call_with_timeout)
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches(llm_timeout=0.01):
            result = await nodes._call_llm_reply("prompt", total_timeout=0.1)

        self.assertEqual(result.content, "备用模型及时回复")
        self.assertEqual(call_cloud_llm.await_count, 2)
        self.assertFalse(
            call_cloud_llm.await_args_list[0].kwargs["use_fallback_model"]
        )
        self.assertTrue(
            call_cloud_llm.await_args_list[1].kwargs["use_fallback_model"]
        )

    async def test_empty_primary_reply_switches_to_fallback(self) -> None:
        call_cloud_llm = AsyncMock(
            side_effect=[_LLMResponse("  "), _LLMResponse("备用模型非空回复")]
        )
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches():
            result = await nodes.llm_reply_node(_state())

        self.assertEqual(result["answer"], "备用模型非空回复")
        self.assertEqual(call_cloud_llm.await_count, 2)

    async def test_text_content_blocks_are_joined_without_retry(self) -> None:
        response = _LLMResponse(
            [
                {"type": "text", "text": "第一步：打开设置。"},
                {"type": "tool_use", "id": "call-1"},
                "第二步：选择账号安全。",
            ]
        )
        call_cloud_llm = AsyncMock(return_value=response)
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches():
            result = await nodes.llm_reply_node(_state())

        self.assertEqual(
            result["answer"],
            "第一步：打开设置。\n第二步：选择账号安全。",
        )
        call_cloud_llm.assert_awaited_once()

    async def test_fallback_failure_returns_maintenance_answer(self) -> None:
        call_cloud_llm = AsyncMock(
            side_effect=[RuntimeError("primary down"), RuntimeError("fallback down")]
        )
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches():
            result = await nodes.llm_reply_node(_state())

        self.assertEqual(result["answer"], nodes.LLM_REPLY_FALLBACK_ANSWER)
        self.assertIsNone(result["llm_response"])
        self.assertEqual(call_cloud_llm.await_count, 2)

    async def test_primary_timeout_cap_remains_ten_seconds(self) -> None:
        call_cloud_llm = AsyncMock(return_value=_LLMResponse("主模型回复"))
        with patch.object(nodes, "call_cloud_llm", call_cloud_llm), self._settings_patches(llm_timeout=60.0):
            await nodes._call_llm_reply("prompt", total_timeout=30.0)

        self.assertEqual(call_cloud_llm.await_args.kwargs["timeout"], 10.0)


if __name__ == "__main__":
    unittest.main()
