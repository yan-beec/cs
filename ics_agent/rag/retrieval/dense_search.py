# 模块职责：使用 BGE-M3 dense embedding 在 ChromaDB 中检索知识库分块。

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from ics_agent.app.core.logging_config import configure_logging, log_event
from ics_agent.channels import DEFAULT_CHANNEL, get_channel
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
_channel_retrievers: dict[str, DenseRetriever] = {}
_retriever_lock = Lock()


@dataclass
class _EmbeddingRequest:
    """一个等待加入在线 embedding 微批次的 query。"""

    text: str
    future: asyncio.Future[list[float]]


class _EmbeddingBatchBroker:
    """在一个事件循环内收集 query，并批量调用共享 embedding 模型。"""

    def __init__(
        self,
        embed: Callable[[list[str]], list[list[float]]],
        *,
        max_batch_size: int,
        max_wait_ms: int,
    ) -> None:
        self._embed = embed
        self._max_batch_size = max_batch_size
        self._max_wait_seconds = max_wait_ms / 1000.0
        self._queue: asyncio.Queue[_EmbeddingRequest] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def submit(self, request: _EmbeddingRequest) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name="rag-embedding-micro-batch",
            )
        self._queue.put_nowait(request)

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            deadline = asyncio.get_running_loop().time() + self._max_wait_seconds

            while len(batch) < self._max_batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    request = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break
                batch.append(request)

            active_requests = [
                request for request in batch if not request.future.cancelled()
            ]
            if not active_requests:
                continue

            started_at = time.perf_counter()
            try:
                vectors = await asyncio.to_thread(
                    self._embed,
                    [request.text for request in active_requests],
                )
                if len(vectors) != len(active_requests):
                    raise RuntimeError(
                        "Embedding model returned an unexpected vector count."
                    )
            except asyncio.CancelledError:
                for request in active_requests:
                    if not request.future.done():
                        request.future.cancel()
                raise
            except Exception as exc:
                for request in active_requests:
                    if not request.future.done():
                        request.future.set_exception(exc)
            else:
                for request, vector in zip(active_requests, vectors):
                    if not request.future.done():
                        request.future.set_result(vector)
                log_event(
                    logger,
                    logging.INFO,
                    "knowledge_base.embedding.micro_batch.completed",
                    batch_size=len(active_requests),
                    elapsed_ms=round(
                        (time.perf_counter() - started_at) * 1000,
                        1,
                    ),
                )


class _EmbeddingBatchRegistry:
    """为每个事件循环保存一个微批处理器，避免跨 loop 复用 asyncio 对象。"""

    def __init__(
        self,
        embed: Callable[[list[str]], list[list[float]]],
        *,
        max_batch_size: int,
        max_wait_ms: int,
    ) -> None:
        self._embed = embed
        self._max_batch_size = max_batch_size
        self._max_wait_ms = max_wait_ms
        self._lock = Lock()
        self._brokers: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, _EmbeddingBatchBroker
        ] = weakref.WeakKeyDictionary()

    def for_current_loop(self) -> _EmbeddingBatchBroker:
        loop = asyncio.get_running_loop()
        broker = self._brokers.get(loop)
        if broker is None:
            with self._lock:
                broker = self._brokers.get(loop)
                if broker is None:
                    broker = _EmbeddingBatchBroker(
                        self._embed,
                        max_batch_size=self._max_batch_size,
                        max_wait_ms=self._max_wait_ms,
                    )
                    self._brokers[loop] = broker
        return broker


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
        self._embedding_batches = _EmbeddingBatchRegistry(
            self.embedder.embed,
            max_batch_size=(
                self.settings.knowledge_base_embedding_micro_batch_max_size
            ),
            max_wait_ms=(
                self.settings.knowledge_base_embedding_micro_batch_wait_ms
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

    def _collection_count(self) -> int:
        """跟随当前渠道的版本指针，并允许未导入资料的集合直接返回空结果。"""
        with vector_store_lock:
            active_name = _read_active_collection_name(self.persist_dir, self.collection_name)
            if active_name != self.active_collection_name:
                self.collection = self._client.get_collection(name=active_name)
                self.active_collection_name = active_name
            return self.collection.count()

    def _search_sync(
        self,
        query: str,
        *,
        top_k: int | None = None,
        query_embedding: list[float] | None = None,
    ) -> list[DenseSearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty.")
        result_limit = top_k if top_k is not None else self.top_k
        if result_limit < 1:
            raise ValueError("top_k must be at least 1.")

        if self._collection_count() < 1:
            return []
        if query_embedding is None:
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
                return []

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
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty.")
        if top_k is not None and top_k < 1:
            raise ValueError("top_k must be at least 1.")
        if await asyncio.to_thread(self._collection_count) < 1:
            return []
        query_embedding_future = asyncio.get_running_loop().create_future()
        self._embedding_batches.for_current_loop().submit(
            _EmbeddingRequest(
                text=normalized_query,
                future=query_embedding_future,
            )
        )
        try:
            query_embedding = await query_embedding_future
        except asyncio.CancelledError:
            query_embedding_future.cancel()
            raise

        return await asyncio.to_thread(
            self._search_sync,
            normalized_query,
            top_k=top_k,
            query_embedding=query_embedding,
        )


def get_dense_retriever(*, warm_up: bool = False, channel_key: str | None = None) -> DenseRetriever:
    """返回进程级稠密检索器单例。"""
    global _retriever
    key = get_channel(channel_key).key
    if key != DEFAULT_CHANNEL:
        with _retriever_lock:
            if key not in _channel_retrievers:
                from ics_agent.channels.bootstrap import ensure_vector_collection
                settings = get_rag_settings(key)
                ensure_vector_collection(settings)
                _channel_retrievers[key] = DenseRetriever(settings=settings)
            retriever = _channel_retrievers[key]
        if warm_up:
            retriever.warm_up()
        return retriever
    if _retriever is None:
        with _retriever_lock:
            if _retriever is None:
                from ics_agent.channels.bootstrap import ensure_vector_collection
                settings = get_rag_settings()
                ensure_vector_collection(settings)
                _retriever = DenseRetriever(settings=settings)
    if warm_up:
        _retriever.warm_up()
    return _retriever


def reset_dense_retriever() -> None:
    """清除单例，主要用于受控重建和测试。"""
    global _retriever
    with _retriever_lock:
        _retriever = None
        _channel_retrievers.clear()


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
