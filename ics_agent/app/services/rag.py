"""FAQ-first RAG 查询服务封装。"""

from __future__ import annotations

import logging
import time

from ics_agent.app.core.logging_config import log_event
from ics_agent.rag.main import run_query

logger = logging.getLogger(__name__)


async def _run_rag_query(
    query: str,
    *,
    thread_id: str | None = None,
) -> dict[str, object]:
    """执行 FAQ-first 查询编排，并统一记录请求耗时和异常。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")

    started_at = time.perf_counter()
    try:
        result = await run_query(normalized_query, thread_id=thread_id)
    except Exception as exc:
        logger.exception(
            "事件=应用.查询.失败 错误类型=%s",
            type(exc).__name__,
        )
        raise

    log_event(
        logger,
        logging.INFO,
        "app.query.completed",
        query_length=len(normalized_query),
        route=result["route"],
        faq_route=result["faq_route"],
        intent_label=result["intent_label"],
        retrieved_count=result["retrieved_count"],
        reranked_count=result["reranked_count"],
        elapsed_seconds=round(time.perf_counter() - started_at, 3),
    )
    return result
