"""应用使用 FAQ 优先策略的统一查询编排。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ics_agent.app.core.logging_config import log_event
from ics_agent.faq.models import FAQResult
from ics_agent.faq.service import get_faq_service
from ics_agent.rag.graph import run_rag_graph

logger = logging.getLogger(__name__)


def _document_summaries(state: Mapping[str, Any]) -> list[dict[str, object]]:
    """将重排结果转换为 API 使用的 JSON 安全结构。"""
    summaries: list[dict[str, object]] = []
    for document in state.get("reranked_documents", []) or []:
        metadata = getattr(document, "metadata", {}) or {}
        summaries.append(
            {
                "rank": getattr(document, "rerank_rank", None),
                "score": getattr(document, "rerank_score", None),
                "source": metadata.get("source", ""),
                "chunk_id": getattr(document, "chunk_id", ""),
                "text": getattr(document, "text", "")[:500],
            }
        )
    return summaries


def _rag_payload(
    query: str,
    state: Mapping[str, Any],
) -> dict[str, object]:
    """将 RAG 图状态规范化为公开查询响应。"""
    route = str(state.get("route") or "rag")
    if route == "chitchat":
        retrieved_documents: list[Any] = []
        reranked_documents: list[Any] = []
        documents: list[dict[str, object]] = []
    else:
        retrieved_documents = state.get("retrieved_documents", []) or []
        reranked_documents = state.get("reranked_documents", []) or []
        documents = _document_summaries(state)
    return {
        "route": route,
        "query": query,
        "answer": state.get("answer", ""),
        "intent_id": state.get("intent_id"),
        "intent_label": state.get("intent_label"),
        "intent_probabilities": state.get("intent_probabilities"),
        "intent_confidence": state.get("intent_confidence"),
        "retrieved_count": len(retrieved_documents),
        "reranked_count": len(reranked_documents),
        "documents": documents,
    }


def _faq_payload(
    query: str,
    faq_result: FAQResult,
) -> dict[str, object]:
    """为直接 FAQ 答案构建相同的公开响应结构。"""
    return {
        "route": "faq",
        "query": query,
        "answer": faq_result.answer or "",
        "intent_id": None,
        "intent_label": None,
        "intent_probabilities": None,
        "intent_confidence": None,
        "retrieved_count": 0,
        "reranked_count": 0,
        "documents": [],
    }


def _attach_faq_fields(
    payload: dict[str, object],
    faq_result: FAQResult,
    *,
    rag_query: str | None,
) -> dict[str, object]:
    """附加 FAQ 诊断信息，同时不暴露 RAG 内部对象。"""
    faq_diagnostics = {
        "route": faq_result.route,
        "matched": faq_result.route == "faq",
        "query": faq_result.query,
        "answer": faq_result.answer,
        "matched_question": faq_result.matched_question,
        "question_id": faq_result.question_id,
        "bm25_score": faq_result.bm25_score,
        "match_degree": faq_result.match_degree,
        "rag_query": faq_result.rag_query,
    }
    payload.update(
        {
            "faq": faq_diagnostics,
            "faq_route": faq_result.route,
            "faq_matched": faq_result.route == "faq",
            "matched_question": faq_result.matched_question,
            "question_id": faq_result.question_id,
            "bm25_score": faq_result.bm25_score,
            "match_degree": faq_result.match_degree,
            "rag_query": rag_query,
        }
    )
    return payload


async def _query_faq(query: str) -> FAQResult:
    """运行同步 FAQ 服务，同时不阻塞事件循环。"""
    try:
        return await asyncio.to_thread(get_faq_service().query, query)
    except ValueError:
        raise
    except Exception as exc:
        # FAQ 是快速路径；其基础设施故障不能阻止较慢的 RAG
        # 降级路径回答同一个用户问题。
        log_event(
            logger,
            logging.WARNING,
            "query.faq.failed",
            error_type=type(exc).__name__,
            fallback="rag",
        )
        return FAQResult(route="rag", query=query, rag_query=query)


async def run_query(
    query: str,
    *,
    thread_id: str | None = None,
) -> dict[str, object]:
    """先通过 FAQ、必要时再通过 RAG 执行一次应用查询。

    公开字段约定：
    - ``route`` is the final answer route: ``faq``, ``chitchat`` or ``rag``.
    - ``faq_route`` preserves the FAQ service route: ``faq`` or ``rag``.
    - ``answer`` is always the answer returned to the caller.
    - ``faq`` contains the public FAQ match diagnostics.
    - ``rag_query`` is the query passed to the RAG graph, or ``None`` when
      FAQ answered directly.
    """
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")

    faq_result = await _query_faq(normalized_query)
    if (
        faq_result.route == "faq"
        and faq_result.answer is not None
    ):
        if thread_id:
            await run_rag_graph(
                normalized_query,
                thread_id=thread_id,
                answer_override=faq_result.answer,
            )
        payload = _faq_payload(normalized_query, faq_result)
        return _attach_faq_fields(
            payload,
            faq_result,
            rag_query=None,
        )

    rag_query = (faq_result.rag_query or normalized_query).strip()
    if not rag_query:
        raise ValueError("RAG query cannot be empty.")

    log_event(
        logger,
        logging.INFO,
        "query.route",
        route="rag",
        faq_route=faq_result.route,
    )
    rag_state = await run_rag_graph(rag_query, thread_id=thread_id)
    payload = _rag_payload(normalized_query, rag_state)
    return _attach_faq_fields(
        payload,
        faq_result,
        rag_query=rag_query,
    )


__all__ = ["run_query"]
