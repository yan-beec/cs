# 模块职责：使用 BGE-M3 dense embedding 在 ChromaDB 中检索知识库分块。

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from threading import Lock
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from ics_agent.app.core.config import PROJECT_ROOT
from ics_agent.app.core.logging_config import configure_logging, log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings
from knowledge_base_in.build_chromadb import (
    BGEM3Embedder,
    _load_chromadb,
    _read_active_collection_name,
    _resolve_project_path,
)
from knowledge_base_in.vector_store_lock import vector_store_lock


logger = logging.getLogger(__name__)
_retriever: DenseRetriever | None = None
_retriever_lock = Lock()


@dataclass(frozen=True)
class DenseSearchResult:
    """一个经过稠密检索排序的知识库结果。"""

    chunk_id: str
    text: str
    metadata: dict[str, Any]
    rank: int
    distance: float


class DenseRetriever:
    """基于持久化 ChromaDB 集合的 BGE-M3 稠密检索器。"""

    def __init__(
        self,
        settings: RAGSettings | None = None,
        *,
        persist_dir: str | Path | None = None,
        collection_name: str | None = None,
        model_dir: str | Path | None = None,
        embedding_batch_size: int | None = None,
        embedding_max_length: int | None = None,
        top_k: int | None = None,
    ) -> None:
        self.settings = settings or get_rag_settings()
        self.persist_dir = _resolve_project_path(
            persist_dir or self.settings.knowledge_base_vector_db_dir
        )
        self.collection_name = (
            collection_name or self.settings.knowledge_base_vector_collection
        )
        self.top_k = (
            top_k
            if top_k is not None
            else self.settings.knowledge_base_retrieval_top_k
        )
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1.")

        chromadb = _load_chromadb()
        from chromadb.config import Settings as ChromaSettings
        from knowledge_base_in.chroma_telemetry import build_chroma_settings

        client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=build_chroma_settings(ChromaSettings),
        )
        self._client = client
        with vector_store_lock:
            self.active_collection_name = _read_active_collection_name(
                self.persist_dir,
                self.collection_name,
            )
            try:
                self.collection = client.get_collection(
                    name=self.active_collection_name,
                )
            except ValueError as exc:
                raise RuntimeError(
                    "Dense ChromaDB collection is unavailable. Build the vector "
                    "index before searching."
                ) from exc

        self.embedder = BGEM3Embedder(
            model_dir or self.settings.knowledge_base_embedding_model_dir,
            max_length=(
                embedding_max_length
                if embedding_max_length is not None
                else self.settings.knowledge_base_embedding_max_length
            ),
            batch_size=(
                embedding_batch_size
                if embedding_batch_size is not None
                else self.settings.knowledge_base_embedding_batch_size
            ),
            cpu_threads=self.settings.knowledge_base_embedding_cpu_threads,
            cpu_interop_threads=(
                self.settings.knowledge_base_embedding_cpu_interop_threads
            ),
        )
        log_event(
            logger,
            logging.INFO,
            "knowledge_base.dense_retriever.ready",
            collection=self.collection_name,
            top_k=self.top_k,
        )

    def warm_up(self) -> None:
        """在应用启动期间加载向量模型。"""
        self.embedder.warm_up()

    def _search_sync(
        self,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[DenseSearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty.")
        result_limit = top_k if top_k is not None else self.top_k
        if result_limit < 1:
            raise ValueError("top_k must be at least 1.")

        query_embedding = self.embedder.embed([normalized_query])[0]
        with vector_store_lock:
            active_collection_name = _read_active_collection_name(
                self.persist_dir,
                self.collection_name,
            )
            if active_collection_name != self.active_collection_name:
                # A rebuild publishes a new collection by atomically updating
                # the pointer file.  Refresh this long-lived retriever before
                # touching the collection so it can follow that switch.
                self.collection = self._client.get_collection(
                    name=active_collection_name,
                )
                self.active_collection_name = active_collection_name
            collection_count = self.collection.count()
            if collection_count < 1:
                raise RuntimeError(
                    f"ChromaDB collection is empty: {self.collection_name}"
                )

            result = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=min(result_limit, collection_count),
                include=["documents", "metadatas", "distances"],
            )

        results: list[DenseSearchResult] = []
        for index, chunk_id in enumerate(result["ids"][0], start=1):
            results.append(
                DenseSearchResult(
                    chunk_id=str(chunk_id),
                    text=result["documents"][0][index - 1],
                    metadata=result["metadatas"][0][index - 1] or {},
                    rank=index,
                    distance=float(result["distances"][0][index - 1]),
                )
            )
        log_event(
            logger,
            logging.INFO,
            "knowledge_base.dense.search.completed",
            query_length=len(normalized_query),
            results=len(results),
        )
        return results

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[DenseSearchResult]:
        """异步执行 embedding 和 ChromaDB 查询。"""
        return await asyncio.to_thread(
            self._search_sync,
            query,
            top_k=top_k,
        )


def get_dense_retriever(*, warm_up: bool = False) -> DenseRetriever:
    """返回进程级稠密检索器单例。"""
    global _retriever
    if _retriever is None:
        with _retriever_lock:
            if _retriever is None:
                _retriever = DenseRetriever(settings=get_rag_settings())
    if warm_up:
        _retriever.warm_up()
    return _retriever


def reset_dense_retriever() -> None:
    """清除单例，主要用于受控重建和测试。"""
    global _retriever
    with _retriever_lock:
        _retriever = None


def main() -> None:
    settings = get_rag_settings()
    parser = argparse.ArgumentParser(
        description="Search the BGE-M3 dense knowledge base in ChromaDB.",
    )
    parser.add_argument("query", help="User query.")
    parser.add_argument(
        "--top-k",
        type=int,
        default=settings.knowledge_base_retrieval_top_k,
        help="Number of dense results to return.",
    )
    args = parser.parse_args()
    configure_logging()
    retriever = DenseRetriever(settings=settings, top_k=args.top_k)
    results = asyncio.run(retriever.search(args.query))
    print(
        json.dumps(
            [asdict(result) for result in results],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
