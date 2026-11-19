"""验证共用流程时 FAQ、检索、提示词和记忆仍按渠道隔离。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ics_agent.channels import config
from ics_agent.channels.prompts import get_system_prompt
from ics_agent.faq import service as faq_service
from ics_agent.faq.config import get_faq_settings
from ics_agent.faq.models import FAQResult
from ics_agent.faq.sync import FAQDatabaseSyncResult, FAQExcelRow
from ics_agent.rag import main as rag_main
from ics_agent.rag.config import RAGSettings, get_rag_settings
from ics_agent.rag.graph import edges, nodes
from ics_agent.rag.retrieval import RetrievedDocument

KEYS = ("xingliao", "yanliao", "xingyingtong", "wechat")


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_faq_settings.cache_clear()
    get_rag_settings.cache_clear()
    yield
    get_faq_settings.cache_clear()
    get_rag_settings.cache_clear()


def test_resources_and_relocated_paths_are_distinct():
    for key in KEYS:
        channel = config.get_channel(key)
        assert channel.faq_table == f"faq_channel_{key}"
        assert channel.kb_collection == f"kb_channel_{key}"
        assert channel.prompt_name == f"prompt_{key}"
        assert channel.knowledge_dir == f"database/channel_{key}/knowledge_base"
        assert get_faq_settings(key).faq_source_xlsx_path == channel.faq_workbook
        assert get_rag_settings(key).knowledge_base_vector_collection == channel.kb_collection
    assert config.get_channel().key == "xingliao"
    assert get_rag_settings().rag_intent_dataset_path == "database/channel_xingliao/intent_dataset_4000.csv"
    with pytest.raises(ValueError, match="Unknown channel"):
        config.get_channel("missing")


def test_legacy_environment_paths_are_upgraded_without_affecting_other_channels(monkeypatch):
    for name, value in {
        "FAQ_SOURCE_TABLE": "faq_qna_optimized",
        "FAQ_SOURCE_XLSX_PATH": "database/最新问答对.xlsx",
        "KNOWLEDGE_BASE_INPUT_DIR": "database/knowledge_base",
        "KNOWLEDGE_BASE_VECTOR_COLLECTION": "knowledge_base",
        "RAG_INTENT_DATASET_PATH": "database/intent_dataset_4000.csv",
    }.items():
        monkeypatch.setenv(name, value)
    assert get_faq_settings().faq_source_table == "faq_channel_xingliao"
    assert get_rag_settings().knowledge_base_input_dir == config.get_channel().knowledge_dir
    assert get_rag_settings().knowledge_base_vector_collection == "kb_channel_xingliao"
    assert get_faq_settings("wechat").faq_source_table == "faq_channel_wechat"
    assert get_rag_settings("wechat").knowledge_base_input_dir == config.get_channel("wechat").knowledge_dir


def test_inbox_binding_uses_both_account_and_inbox(monkeypatch):
    channels = [replace(channel, inboxes=(("1", "10"),) if channel.key == "yanliao" else ())
                for channel in config.list_channels()]
    monkeypatch.setattr(config, "list_channels", lambda: tuple(channels))
    assert config.resolve_chatwoot_channel("1", 10) == "yanliao"
    assert config.resolve_chatwoot_channel("2", 10) == "xingliao"
    assert config.resolve_chatwoot_channel("1", None) == "xingliao"


def test_same_faq_question_and_id_return_channel_specific_answers_concurrently(monkeypatch):
    monkeypatch.setattr(faq_service, "_service", None)
    monkeypatch.setattr(faq_service, "_channel_services", {})
    monkeypatch.setattr(faq_service, "ensure_faq_table", Mock())

    def repository(*, settings):
        return SimpleNamespace(
            fetch_questions=lambda include_disabled=False: [(1, "如何联系客服")],
            fetch_answers=lambda ids: {1: settings.faq_table_name},
        )

    monkeypatch.setattr(faq_service, "FAQRepository", repository)
    services = [faq_service.get_faq_service(key) for key in KEYS]

    async def query_all():
        return await asyncio.gather(*(asyncio.to_thread(s.query, "如何联系客服") for s in services))

    answers = asyncio.run(query_all())
    assert [answer.answer for answer in answers] == [f"faq_channel_{key}" for key in KEYS]
    assert all(answer.route == "faq" for answer in answers)
    assert len({id(s) for s in services}) == 4
    assert faq_service.get_faq_service() is services[0]
    assert faq_service.get_faq_service("wechat") is services[3]
    assert all(service.query("完全无关的火星问题").route == "rag" for service in services)


def test_empty_faq_channel_falls_back_without_borrowing_default_answers(monkeypatch):
    monkeypatch.setattr(faq_service, "ensure_faq_table", Mock())
    repository = Mock(fetch_questions=Mock(return_value=[]))
    service = faq_service.FAQService(faq_repository=repository, settings=get_faq_settings("wechat"))
    result = service.query("如何联系客服")
    assert result.route == "rag"
    assert result.answer is None
    repository.fetch_answers.assert_not_called()


def test_faq_refresh_binds_database_and_runtime_to_the_same_channel(monkeypatch):
    from ics_agent.faq import sync

    monkeypatch.setattr(faq_service, "ensure_faq_table", Mock())
    synchronizer = Mock(sync=Mock(return_value=FAQDatabaseSyncResult(1, 0, (1,), True, "v1")))
    factory = Mock(return_value=synchronizer)
    monkeypatch.setattr(sync, "FAQQnaSynchronizer", factory)
    settings = get_faq_settings("yanliao")
    service = faq_service.FAQService(settings=settings)
    service._apply_excel_refresh_sync([FAQExcelRow("如何联系客服", "言聊答案", 0, 1)], "input.xlsx", "v1")
    factory.assert_called_once_with(settings=settings)
    assert service.query("如何联系客服").answer == "言聊答案"


@pytest.mark.parametrize("faq_hit", [False, True])
def test_channel_reaches_faq_and_graph_including_direct_answer_memory(monkeypatch, faq_hit):
    query = "同一个问题"
    result = FAQResult(route="faq" if faq_hit else "rag", query=query,
                       answer="言聊 FAQ" if faq_hit else None, rag_query=query)
    faq = Mock(query=Mock(return_value=result))
    factory = Mock(return_value=faq)
    graph = AsyncMock(return_value={"answer": "言聊 RAG", "route": "rag"})
    monkeypatch.setattr(rag_main, "get_faq_service", factory)
    monkeypatch.setattr(rag_main, "run_rag_graph", graph)
    response = asyncio.run(rag_main.run_query(query, channel_key="yanliao", thread_id="shared"))
    factory.assert_called_once_with("yanliao")
    assert graph.await_args.kwargs["channel_key"] == "yanliao"
    assert graph.await_args.kwargs["thread_id"] == "shared"
    if faq_hit:
        assert graph.await_args.kwargs["answer_override"] == "言聊 FAQ"
    assert response["answer"] == ("言聊 FAQ" if faq_hit else "言聊 RAG")


def test_one_graph_routes_concurrent_channels_to_their_own_documents_and_prompts(monkeypatch):
    settings = RAGSettings(_env_file=None, rag_reranker_enabled=False)
    monkeypatch.setattr(nodes, "get_rag_settings", lambda: settings)
    monkeypatch.setattr(edges, "get_rag_settings", lambda: settings)
    monkeypatch.setattr(nodes, "get_intent_classifier", lambda: SimpleNamespace(
        predict=AsyncMock(return_value=(0, "咨询问题", {"咨询问题": 1.0}, 1.0))))
    calls = []

    async def retrieve(query, *, top_k, channel_key="xingliao"):
        return [RetrievedDocument("same-id", f"仅 {channel_key} 的知识内容", {}, 1, 0.0)]

    async def llm(prompt, *, system_prompt, **kwargs):
        key = next(key for key in KEYS if f"仅 {key} 的知识内容" in prompt)
        calls.append((key, prompt, system_prompt))
        return SimpleNamespace(content=key)

    monkeypatch.setattr(nodes, "aretrieve_documents", retrieve)
    monkeypatch.setattr(nodes, "call_cloud_llm", llm)
    graph = edges.build_rag_graph()

    async def run_all():
        return await asyncio.gather(*(graph.ainvoke({"query": "同一个问题", "channel_key": key})
                                      for key in KEYS))

    results = asyncio.run(run_all())
    assert [result["answer"] for result in results] == list(KEYS)
    for key, prompt, system_prompt in calls:
        assert system_prompt == get_system_prompt(key)
        assert all(f"仅 {other} 的知识内容" not in prompt for other in KEYS if other != key)
        if key != "xingliao":
            assert "兴聊" not in prompt


def test_memory_keeps_legacy_default_id_and_namespaces_other_channels(monkeypatch):
    import ics_agent.rag.memory as memory

    saver = InMemorySaver()
    manager = SimpleNamespace(startup=AsyncMock(return_value=saver),
                              start_cleanup_task=AsyncMock(), touch_thread=AsyncMock())
    graph = SimpleNamespace(ainvoke=AsyncMock(return_value={}))
    monkeypatch.setattr(memory, "get_conversation_memory_manager", lambda: manager)
    monkeypatch.setattr(edges, "get_rag_graph", lambda **kwargs: graph)

    async def run_all():
        for key in KEYS:
            await edges.run_rag_graph("问题", thread_id="same-thread", channel_key=key)
        await edges.run_rag_graph("默认渠道下一轮", thread_id="same-thread")

    asyncio.run(run_all())
    calls = graph.ainvoke.await_args_list
    assert [call.kwargs["config"]["configurable"]["thread_id"] for call in calls] == [
        "same-thread", "channel:yanliao:same-thread", "channel:xingyingtong:same-thread",
        "channel:wechat:same-thread", "same-thread",
    ]
    assert [call.args[0]["channel_key"] for call in calls] == [*KEYS, "xingliao"]


def test_fallback_model_uses_the_same_channel_prompt(monkeypatch):
    cloud = AsyncMock(side_effect=[RuntimeError("primary failed"), SimpleNamespace(content="回复")])
    monkeypatch.setattr(nodes, "call_cloud_llm", cloud)
    prompt = get_system_prompt("xingyingtong")
    asyncio.run(nodes._call_llm_reply("问题", system_prompt=prompt, total_timeout=5))
    assert cloud.await_count == 2
    assert all(call.kwargs["system_prompt"] == prompt for call in cloud.await_args_list)
