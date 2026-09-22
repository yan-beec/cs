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

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import configure_logging, log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings
from knowledge_base_in.build_chromadb import (
    BGEM3Embedder,
    _load_chromadb,
    _resolve_project_path,
)


logger = logging.getLogger(__name__)
_retriever: DenseRetriever | None = None
_retriever_lock = Lock()


@dataclass(frozen=True)
class DenseSearchResult:
    """One dense-ranked knowledge-base result."""

    chunk_id: str
    text: str
    metadata: dict[str, Any]
    rank: int
    distance: float


class DenseRetriever:
    """BGE-M3 dense retriever backed by a persistent ChromaDB collection."""

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
        client = chromadb.PersistentClient(path=str(self.persist_dir))
        try:
            self.collection = client.get_collection(name=self.collection_name)
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
        )
        log_event(
            logger,
            logging.INFO,
            "knowledge_base.dense_retriever.ready",
            collection=self.collection_name,
            top_k=self.top_k,
        )

    def warm_up(self) -> None:
        """Load the embedding model during application startup."""
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

        collection_count = self.collection.count()
        if collection_count < 1:
            raise RuntimeError(
                f"ChromaDB collection is empty: {self.collection_name}"
            )

        query_embedding = self.embedder.embed([normalized_query])[0]
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
    """Return the process-level dense retriever singleton."""
    global _retriever
    if _retriever is None:
        with _retriever_lock:
            if _retriever is None:
                _retriever = DenseRetriever(settings=get_rag_settings())
    if warm_up:
        _retriever.warm_up()
    return _retriever


def reset_dense_retriever() -> None:
    """Clear the singleton, mainly for controlled rebuilds and tests."""
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
