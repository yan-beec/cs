"""Verify reranker selection through the compiled graph and generated prompt."""

import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from ics_agent.rag import main as rag_main
from ics_agent.rag.config import RAGSettings
from ics_agent.rag.graph import edges, nodes
from ics_agent.rag.memory import nodes as memory_nodes
from ics_agent.rag.prompt import build_dynamic_prompt
from ics_agent.rag.reranker import RerankedDocument
from ics_agent.rag.retrieval import RetrievedDocument


QUERY = "怎么提现？"


def _documents(count=10, *, prefix="dense"):
    return [
        RetrievedDocument(
            chunk_id=f"{prefix}-{rank}",
            text=f"{prefix}-document-content-{rank:02d}",
            metadata={"source": f"{prefix}-source.md"},
            rank=rank,
            distance=rank / 100,
        )
        for rank in range(1, count + 1)
    ]


def _reranked(documents):
    return [
        RerankedDocument.from_retrieved_document(
            document,
            rerank_rank=rank,
            rerank_score=10.0 - rank,
        )
        for rank, document in enumerate(documents, start=1)
    ]


class RerankerToggleTest(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def _dependencies(self, *, enabled, documents, reranked=None, intent="提现"):
        settings = RAGSettings(
            _env_file=None,
            rag_reranker_enabled=enabled,
            knowledge_base_retrieval_top_k=10,
            rag_reranker_input_top_k=10,
            rag_reranker_output_top_k=3,
            rag_memory_summary_interval=100,
        )
        classifier = SimpleNamespace(
            predict=AsyncMock(return_value=(0, intent, {intent: 1.0}, 1.0))
        )
        retrieve = AsyncMock(return_value=documents)
        rerank = AsyncMock(return_value=reranked or [])
        cloud = AsyncMock(return_value=AIMessage(content="测试回答"))
        with (
            patch.object(nodes, "get_rag_settings", return_value=settings),
            patch.object(edges, "get_rag_settings", return_value=settings),
            patch.object(memory_nodes, "get_rag_settings", return_value=settings),
            patch.object(nodes, "get_settings", return_value=SimpleNamespace(llm_timeout=10)),
            patch.object(nodes, "get_intent_classifier", return_value=classifier),
            patch.object(nodes, "aretrieve_documents", retrieve),
            patch.object(nodes, "arerank_documents", rerank),
            patch.object(nodes, "call_cloud_llm", cloud),
        ):
            yield SimpleNamespace(
                settings=settings,
                classifier=classifier,
                retrieve=retrieve,
                rerank=rerank,
                cloud=cloud,
            )

    def test_setting_defaults_to_enabled_and_reads_environment_toggle(self):
        with patch.dict(os.environ):
            os.environ.pop("RAG_RERANKER_ENABLED", None)
            self.assertTrue(RAGSettings(_env_file=None).rag_reranker_enabled)
            for value, expected in (("false", False), ("true", True)):
                with self.subTest(value=value):
                    os.environ["RAG_RERANKER_ENABLED"] = value
                    self.assertIs(
                        RAGSettings(_env_file=None).rag_reranker_enabled,
                        expected,
                    )

    async def test_enabled_graph_preserves_rerank_order_and_output_limit(self):
        documents = _documents()
        ranked = _reranked([documents[7], documents[1], documents[4]])
        with self._dependencies(enabled=True, documents=documents, reranked=ranked) as deps:
            result = await edges.build_rag_graph().ainvoke({"query": QUERY})

        deps.retrieve.assert_awaited_once_with(QUERY, top_k=10)
        deps.rerank.assert_awaited_once_with(QUERY, documents, top_k=3)
        self.assertEqual(result["reranked_documents"], ranked)
        self.assertEqual(result["context_documents"], ranked)
        prompt = deps.cloud.await_args.args[0]
        self.assertLess(prompt.index(ranked[0].text), prompt.index(ranked[1].text))
        self.assertLess(prompt.index(ranked[1].text), prompt.index(ranked[2].text))
        self.assertNotIn(documents[0].text, prompt)
        payload = rag_main._rag_payload(QUERY, result)
        self.assertEqual(payload["retrieved_count"], 10)
        self.assertEqual(payload["reranked_count"], 3)
        self.assertEqual([doc["chunk_id"] for doc in payload["documents"]], [doc.chunk_id for doc in ranked])
        self.assertEqual([doc["score"] for doc in payload["documents"]], [doc.rerank_score for doc in ranked])

    async def test_disabled_graph_sends_dense_top_five_to_prompt_and_api(self):
        documents = _documents(5)
        with self._dependencies(enabled=False, documents=documents) as deps:
            result = await edges.build_rag_graph().ainvoke({"query": QUERY})

        deps.retrieve.assert_awaited_once_with(QUERY, top_k=5)
        deps.rerank.assert_not_awaited()
        self.assertEqual(result["context_documents"], documents)
        self.assertEqual(result["reranked_documents"], [])
        prompt = deps.cloud.await_args.args[0]
        positions = [prompt.index(document.text) for document in documents]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(prompt.count("[参考文档"), 5)
        payload = rag_main._rag_payload(QUERY, result)
        self.assertEqual(payload["retrieved_count"], 5)
        self.assertEqual(payload["reranked_count"], 0)
        self.assertEqual([doc["chunk_id"] for doc in payload["documents"]], [doc.chunk_id for doc in documents])
        self.assertEqual([doc["rank"] for doc in payload["documents"]], [1, 2, 3, 4, 5])
        self.assertTrue(all(doc["score"] is None for doc in payload["documents"]))

    async def test_disabled_graph_handles_fewer_than_five_and_empty_results(self):
        for count in (2, 0):
            with self.subTest(count=count):
                documents = _documents(count)
                with self._dependencies(enabled=False, documents=documents) as deps:
                    result = await edges.build_rag_graph().ainvoke({"query": QUERY})

                deps.retrieve.assert_awaited_once_with(QUERY, top_k=5)
                deps.rerank.assert_not_awaited()
                self.assertEqual(result["context_documents"], documents)
                self.assertEqual(result["answer"], "测试回答")
                prompt = deps.cloud.await_args.args[0]
                self.assertEqual(prompt.count("[参考文档"), count)
                if not count:
                    self.assertIn("没有检索到可用的知识库内容。", prompt)
                self.assertEqual(len(rag_main._rag_payload(QUERY, result)["documents"]), count)

    async def test_disabling_reranker_clears_previous_checkpoint_documents(self):
        old_documents = _documents(prefix="old")
        ranked = _reranked(old_documents[:3])
        fresh_documents = _documents(5, prefix="fresh")
        config = {"configurable": {"thread_id": "reranker-switch"}}
        with self._dependencies(enabled=True, documents=old_documents, reranked=ranked) as deps:
            graph = edges.build_rag_graph(checkpointer=InMemorySaver())
            await graph.ainvoke({"query": QUERY, "memory_enabled": True}, config=config)
            deps.settings.rag_reranker_enabled = False
            deps.retrieve.return_value = fresh_documents
            result = await graph.ainvoke(
                {"query": "在哪里查看收益？", "memory_enabled": True},
                config=config,
            )

        self.assertEqual(deps.rerank.await_count, 1)
        self.assertEqual(result["reranked_documents"], [])
        self.assertEqual(result["context_documents"], fresh_documents)
        prompt = deps.cloud.await_args.args[0]
        for document in old_documents:
            self.assertNotIn(document.text, prompt)
        for document in fresh_documents:
            self.assertIn(document.text, prompt)

    async def test_chitchat_clears_previous_dense_context_and_skips_retrieval(self):
        documents = _documents(5)
        config = {"configurable": {"thread_id": "dense-then-chitchat"}}
        with self._dependencies(enabled=False, documents=documents) as deps:
            graph = edges.build_rag_graph(checkpointer=InMemorySaver())
            await graph.ainvoke({"query": QUERY, "memory_enabled": True}, config=config)
            deps.classifier.predict.return_value = (1, "闲聊", {"闲聊": 1.0}, 1.0)
            result = await graph.ainvoke(
                {"query": "你好", "memory_enabled": True},
                config=config,
            )

        self.assertEqual(deps.retrieve.await_count, 1)
        deps.rerank.assert_not_awaited()
        self.assertEqual(result["retrieved_documents"], [])
        self.assertEqual(result["reranked_documents"], [])
        self.assertEqual(result["context_documents"], [])
        for document in documents:
            self.assertNotIn(document.text, deps.cloud.await_args.args[0])
        self.assertEqual(rag_main._rag_payload("你好", result)["documents"], [])

    def test_warmup_loads_reranker_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                settings = RAGSettings(_env_file=None, rag_reranker_enabled=enabled)
                with (
                    patch.object(edges, "get_rag_settings", return_value=settings),
                    patch.object(edges, "_graphs", {}),
                    patch.object(edges, "_graph", None),
                    patch.object(edges, "get_intent_classifier") as intent,
                    patch.object(edges, "get_retrieval_service") as retrieve,
                    patch.object(edges, "get_reranker_service") as rerank,
                ):
                    edges.get_rag_graph(warm_up=True)

                intent.assert_called_once_with(warm_up=True)
                retrieve.assert_called_once_with(warm_up=True)
                if enabled:
                    rerank.assert_called_once_with(warm_up=True)
                else:
                    rerank.assert_not_called()

    def test_prompt_and_api_support_legacy_state_but_respect_empty_context(self):
        ranked = _reranked(_documents(1))
        state = {"query": QUERY, "reranked_documents": ranked}
        self.assertIn(ranked[0].text, build_dynamic_prompt(state))
        self.assertEqual(len(rag_main._rag_payload(QUERY, state)["documents"]), 1)

        state["context_documents"] = []
        prompt = build_dynamic_prompt(state)
        self.assertNotIn(ranked[0].text, prompt)
        self.assertIn("没有检索到可用的知识库内容。", prompt)
        self.assertEqual(rag_main._rag_payload(QUERY, state)["documents"], [])

    async def test_faq_answer_override_still_skips_models_for_both_settings(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                with self._dependencies(enabled=enabled, documents=_documents()) as deps:
                    graph = edges.build_rag_graph(checkpointer=InMemorySaver())
                    result = await graph.ainvoke(
                        {"query": QUERY, "answer_override": "FAQ 回复", "memory_enabled": True},
                        config={"configurable": {"thread_id": f"faq-{enabled}"}},
                    )

                deps.classifier.predict.assert_not_awaited()
                deps.retrieve.assert_not_awaited()
                deps.rerank.assert_not_awaited()
                deps.cloud.assert_not_awaited()
                self.assertEqual(result["recent_messages"][-1]["content"], "FAQ 回复")


if __name__ == "__main__":
    unittest.main()
