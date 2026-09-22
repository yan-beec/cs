# 模块职责：将 RAG MVP 节点按固定顺序连接为可异步执行的 LangGraph。

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

from langgraph.graph import END, START, StateGraph

from ics_agent.app.logging_config import log_event
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
_graph_lock = Lock()


def build_rag_graph() -> Any:
    """构建固定顺序的 RAG MVP 图，不包含条件路由。"""
    graph = StateGraph(RAGState)
    graph.add_node("intent_recognition", intent_recognition_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("reranker", reranker_node)
    graph.add_node("llm_reply", llm_reply_node)

    graph.add_edge(START, "intent_recognition")
    graph.add_edge("intent_recognition", "retrieval")
    graph.add_edge("retrieval", "reranker")
    graph.add_edge("reranker", "llm_reply")
    graph.add_edge("llm_reply", END)

    compiled_graph = graph.compile()
    log_event(
        logger,
        logging.INFO,
        "rag.graph.compiled",
        nodes="intent_recognition,retrieval,reranker,llm_reply",
    )
    return compiled_graph


def get_rag_graph(*, warm_up: bool = False) -> Any:
    """返回进程级编译图，可选地预热意图分类模型。"""
    global _graph
    if _graph is None:
        with _graph_lock:
            if _graph is None:
                _graph = build_rag_graph()
    if warm_up:
        get_intent_classifier(warm_up=True)
        get_retrieval_service(warm_up=True)
        get_reranker_service(warm_up=True)
    return _graph


async def run_rag_graph(query: str) -> RAGState:
    """执行一轮固定顺序的 RAG MVP 链路。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")
    return await get_rag_graph().ainvoke({"query": normalized_query})
