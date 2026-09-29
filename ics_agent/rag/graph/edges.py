# 模块职责：将 RAG MVP 节点按固定顺序连接为可异步执行的 LangGraph。

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

from langgraph.graph import END, START, StateGraph

from ics_agent.app.core.logging_config import log_event
from ics_agent.rag.memory import (
    memory_finalize_node,
    memory_prepare_node,
    route_after_memory_prepare,
)
from ics_agent.rag.reranker import get_reranker_service
from ics_agent.rag.retrieval import get_retrieval_service

from .nodes import (
    get_intent_classifier,
    intent_recognition_node,
    llm_reply_node,
    reranker_node,
    retrieval_node,
)
from .state import RAGState


logger = logging.getLogger(__name__)
_graph: Any | None = None
_graphs: dict[int, Any] = {}
_graph_lock = Lock()


def build_rag_graph(*, checkpointer: Any | None = None) -> Any:
    """构建带可选 PostgreSQL checkpoint 的 RAG 图。"""
    graph = StateGraph(RAGState)
    graph.add_node("memory_prepare", memory_prepare_node)
    graph.add_node("intent_recognition", intent_recognition_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("reranker", reranker_node)
    graph.add_node("llm_reply", llm_reply_node)
    graph.add_node("memory_finalize", memory_finalize_node)

    graph.add_edge(START, "memory_prepare")
    graph.add_conditional_edges(
        "memory_prepare",
        route_after_memory_prepare,
        {
            "answer_override": "memory_finalize",
            "rag": "intent_recognition",
        },
    )
    graph.add_conditional_edges(
        "intent_recognition",
        route_after_intent_recognition,
        {
            "chitchat": "llm_reply",
            "rag": "retrieval",
        },
    )
    graph.add_edge("retrieval", "reranker")
    graph.add_edge("reranker", "llm_reply")
    graph.add_edge("llm_reply", "memory_finalize")
    graph.add_edge("memory_finalize", END)

    compiled_graph = graph.compile(checkpointer=checkpointer)
    log_event(
        logger,
        logging.INFO,
        "rag.graph.compiled",
        nodes="memory_prepare,intent_recognition,retrieval,reranker,llm_reply,memory_finalize",
        checkpoint_enabled=checkpointer is not None,
    )
    return compiled_graph


def route_after_intent_recognition(state: RAGState) -> str:
    """闲聊意图跳过检索和精排，其余意图进入完整 RAG 链路。"""
    if state.get("route") == "chitchat":
        return "chitchat"
    return "rag"


def get_rag_graph(
    *,
    warm_up: bool = False,
    checkpointer: Any | None = None,
) -> Any:
    """返回进程级编译图，可选地预热意图分类模型。"""
    global _graph
    graph_key = 0 if checkpointer is None else id(checkpointer)
    if graph_key not in _graphs:
        with _graph_lock:
            if graph_key not in _graphs:
                _graphs[graph_key] = build_rag_graph(
                    checkpointer=checkpointer
                )
    _graph = _graphs[graph_key]
    if warm_up:
        get_intent_classifier(warm_up=True)
        get_retrieval_service(warm_up=True)
        get_reranker_service(warm_up=True)
    return _graph


async def run_rag_graph(
    query: str,
    *,
    thread_id: str | None = None,
    answer_override: str | None = None,
) -> RAGState:
    """执行一轮 RAG 链路，并按 thread_id 持久化对话记忆。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")

    from ics_agent.rag.memory import get_conversation_memory_manager

    memory_manager = get_conversation_memory_manager()
    checkpointer = None
    if thread_id:
        try:
            checkpointer = await memory_manager.startup()
            await memory_manager.start_cleanup_task()
            await memory_manager.touch_thread(thread_id)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "rag.memory.unavailable",
                error_type=type(exc).__name__,
            )

    memory_enabled = checkpointer is not None and bool(thread_id)
    graph_input: dict[str, Any] = {
        "query": normalized_query,
        "memory_enabled": memory_enabled,
        "answer_override": answer_override or "",
    }

    config = None
    if memory_enabled:
        config = {
            "configurable": {
                "thread_id": thread_id,
            }
        }

    return await get_rag_graph(checkpointer=checkpointer).ainvoke(
        graph_input,
        config=config,
    )
