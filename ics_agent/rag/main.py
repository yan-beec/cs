"""Unified FAQ-first query orchestration for the application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ics_agent.app.logging_config import log_event
from ics_agent.faq.models import FAQResult
from ics_agent.faq.service import get_faq_service
from ics_agent.rag.graph import run_rag_graph

logger = logging.getLogger(__name__)


def _document_summaries(state: Mapping[str, Any]) -> list[dict[str, object]]:
    """Convert reranker results into the JSON-safe shape used by the API."""
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
    """Normalize a RAG graph state into the public query response."""
    retrieved_documents = state.get("retrieved_documents", []) or []
    reranked_documents = state.get("reranked_documents", []) or []
    return {
        "route": "rag",
        "query": query,
        "answer": state.get("answer", ""),
        "intent_id": state.get("intent_id"),
        "intent_label": state.get("intent_label"),
        "intent_probabilities": state.get("intent_probabilities"),
        "intent_confidence": state.get("intent_confidence"),
        "retrieved_count": len(retrieved_documents),
        "reranked_count": len(reranked_documents),
        "documents": _document_summaries(state),
    }


def _faq_payload(
    query: str,
    faq_result: FAQResult,
) -> dict[str, object]:
    """Build the same public response shape for a direct FAQ answer."""
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
    """Attach FAQ diagnostics without exposing RAG-internal objects."""
    faq_diagnostics = {
        "route": faq_result.route,
        "matched": faq_result.route in {"cache", "faq"},
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
            "faq_matched": faq_result.route in {"cache", "faq"},
            "matched_question": faq_result.matched_question,
            "question_id": faq_result.question_id,
            "bm25_score": faq_result.bm25_score,
            "match_degree": faq_result.match_degree,
            "rag_query": rag_query,
        }
    )
    return payload


async def _query_faq(query: str) -> FAQResult:
    """Run the synchronous FAQ service without blocking the event loop."""
    try:
        return await asyncio.to_thread(get_faq_service().query, query)
    except ValueError:
        raise
    except Exception as exc:
        # FAQ is the fast path. Its infrastructure failures must not prevent
        # the slower RAG fallback from answering the same user query.
        log_event(
            logger,
            logging.WARNING,
            "query.faq.failed",
            error_type=type(exc).__name__,
            fallback="rag",
        )
        return FAQResult(route="rag", query=query, rag_query=query)


async def run_query(query: str) -> dict[str, object]:
    """Run one application query through FAQ first, then RAG if needed.

    Public field contract:
    - ``route`` is the final answer route: ``faq`` or ``rag``.
    - ``faq_route`` preserves the FAQ service route: ``cache``, ``faq`` or
      ``rag``.
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
        faq_result.route in {"cache", "faq"}
        and faq_result.answer is not None
    ):
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
    rag_state = await run_rag_graph(rag_query)
    payload = _rag_payload(normalized_query, rag_state)
    return _attach_faq_fields(
        payload,
        faq_result,
        rag_query=rag_query,
    )


__all__ = ["run_query"]
