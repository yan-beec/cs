# 模块职责：定义 RAG MVP 图的共享状态和各节点间的数据契约。

from __future__ import annotations

from typing import Any, TypedDict

from ics_agent.rag.reranker import RerankedDocument
from ics_agent.rag.retrieval import RetrievedDocument


class RAGState(TypedDict, total=False):
    """RAG 图在各节点之间传递的状态。"""

    # 输入和未来路由字段。
    query: str
    route: str
    channel_key: str

    # 意图识别节点输出。
    intent_id: int
    intent_label: str
    intent_probabilities: dict[str, float]
    intent_confidence: float

    # 检索和精排节点输出。
    retrieved_documents: list[RetrievedDocument]
    reranked_documents: list[RerankedDocument]
    # 本轮实际交给 LLM 的文档：精排结果或直接召回的 Top5。
    context_documents: list[RetrievedDocument | RerankedDocument]

    # 大模型回复节点输出。
    llm_response: Any
    answer: str
    answer_override: str

    # PostgreSQL checkpoint 对话记忆。
    memory_enabled: bool
    recent_messages: list[dict[str, Any]]
    memory_history: list[dict[str, Any]]
    summary_pending_messages: list[dict[str, Any]]
    conversation_summary: str
    conversation_turn_count: int
    summary_turn_count: int
    memory_summary_triggered: bool

    # 节点异常信息，保留给后续降级路由使用。
    error: str
