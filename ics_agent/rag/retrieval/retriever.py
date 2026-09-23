# 模块职责：复用全局 BGE-M3 常驻单例，将用户 query 向量化并从 ChromaDB 召回文档。

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock
from typing import Any

from ics_agent.app.logging_config import log_event
from ics_agent.rag.config import get_rag_settings
from .dense_search import (
    DenseSearchResult,
    DenseRetriever,
    get_dense_retriever,
)


logger = logging.getLogger(__name__)
_service: RetrievalService | None = None
_service_lock = Lock()


@dataclass(frozen=True)
class RetrievedDocument:
    """RAG 检索阶段返回的单个知识库文档。"""

    chunk_id: str
    text: str
    metadata: dict[str, Any]
    rank: int
    distance: float

    @classmethod
    def from_dense_result(
        cls,
        result: DenseSearchResult,
    ) -> "RetrievedDocument":
        return cls(
            chunk_id=result.chunk_id,
            text=result.text,
            metadata=result.metadata,
            rank=result.rank,
            distance=result.distance,
        )


class RetrievalService:
    """RAG 知识库稠密检索服务。"""

    def __init__(
        self,
        retriever: DenseRetriever | None = None,
        *,
        top_k: int | None = None,
    ) -> None:
        settings = get_rag_settings()
        self.top_k = (
            top_k
            if top_k is not None
            else settings.knowledge_base_retrieval_top_k
        )
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1.")

        # 复用 knowledge_base_in 中的进程级检索器和 BGE-M3 模型单例。
        self._retriever = retriever or get_dense_retriever()
        log_event(
            logger,
            logging.INFO,
            "rag.retrieval.ready",
            top_k=self.top_k,
            collection=self._retriever.collection_name,
        )

    def warm_up(self) -> None:
        """在应用启动阶段加载 BGE-M3，避免首个请求触发模型加载。"""
        self._retriever.warm_up()

    async def aretrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        """异步召回 dense Top-K 文档，不阻塞事件循环。"""
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty.")
        result_limit = top_k if top_k is not None else self.top_k
        if result_limit < 1:
            raise ValueError("top_k must be at least 1.")

        dense_results = await self._retriever.search(
            normalized_query,
            top_k=result_limit,
        )
        results = [
            RetrievedDocument.from_dense_result(result)
            for result in dense_results
        ]
        log_event(
            logger,
            logging.INFO,
            "rag.retrieval.completed",
            query_length=len(normalized_query),
            top_k=result_limit,
            results=len(results),
        )
        return results


def get_retrieval_service(*, warm_up: bool = False) -> RetrievalService:
    """返回进程级 RAG 检索服务单例。"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = RetrievalService()
    if warm_up:
        _service.warm_up()
    return _service


async def aretrieve_documents(
    query: str,
    *,
    top_k: int | None = None,
) -> list[RetrievedDocument]:
    """通过全局检索服务异步召回文档。"""
    return await get_retrieval_service().aretrieve(query, top_k=top_k)
