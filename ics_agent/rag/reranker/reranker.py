# 模块职责：使用本地 BGE-Reranker-v2-M3 对 dense 召回文档进行异步 cross-encoder 精排。

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Semaphore
from typing import Any, Sequence

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings
from ics_agent.rag.retrieval.retriever import (
    RetrievedDocument,
    aretrieve_documents,
)


logger = logging.getLogger(__name__)
_service: RerankerService | None = None
_service_lock = Lock()


def _resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class RerankedDocument:
    """单个经过 cross-encoder 精排后的知识库文档。"""

    chunk_id: str
    text: str
    metadata: dict[str, Any]
    retrieval_rank: int
    rerank_rank: int
    distance: float
    rerank_score: float

    @classmethod
    def from_retrieved_document(
        cls,
        document: RetrievedDocument,
        *,
        rerank_rank: int,
        rerank_score: float,
    ) -> "RerankedDocument":
        return cls(
            chunk_id=document.chunk_id,
            text=document.text,
            metadata=document.metadata,
            retrieval_rank=document.rank,
            rerank_rank=rerank_rank,
            distance=document.distance,
            rerank_score=rerank_score,
        )


class CrossEncoderModel:
    """BGE cross-encoder 模型封装，模型只在首次推理时加载。"""

    _model_cache: dict[tuple[str, str, bool], Any] = {}
    _model_cache_lock = Lock()

    def __init__(
        self,
        settings: RAGSettings | None = None,
    ) -> None:
        self.settings = settings or get_rag_settings()
        self.model_dir = _resolve_project_path(
            self.settings.rag_reranker_model_dir
        )
        if not self.model_dir.is_dir():
            raise FileNotFoundError(
                f"Reranker model directory does not exist: {self.model_dir}"
            )
        self.batch_size = self.settings.rag_reranker_batch_size
        self.max_length = self.settings.rag_reranker_max_length
        self.use_fp16 = self.settings.rag_reranker_use_fp16
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._device: Any | None = None

    def _load(self) -> None:
        if self._model is not None and self._tokenizer is not None:
            return
        try:
            import torch
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Torch and Transformers are required for the BGE reranker. "
                "Install requirements.txt in the cs environment."
            ) from exc

        device_name = "cuda" if torch.cuda.is_available() else "cpu"
        fp16_enabled = self.use_fp16 and device_name == "cuda"
        cache_key = (str(self.model_dir), device_name, fp16_enabled)
        with self._model_cache_lock:
            cached = self._model_cache.get(cache_key)
            if cached is None:
                tokenizer = AutoTokenizer.from_pretrained(
                    str(self.model_dir),
                    local_files_only=True,
                )
                model = AutoModelForSequenceClassification.from_pretrained(
                    str(self.model_dir),
                    local_files_only=True,
                )
                device = torch.device(device_name)
                model.to(device)
                model.eval()
                if fp16_enabled:
                    model.half()
                cached = (tokenizer, model, device)
                self._model_cache[cache_key] = cached
                loaded = True
            else:
                loaded = False

        self._tokenizer, self._model, self._device = cached
        log_event(
            logger,
            logging.INFO,
            (
                "rag.reranker.model.loaded"
                if loaded
                else "rag.reranker.model.reused"
            ),
            model_dir=self.model_dir,
            device=device_name,
            fp16=fp16_enabled,
            max_length=self.max_length,
        )

    def warm_up(self) -> None:
        """在应用启动时加载 tokenizer 和 cross-encoder 模型。"""
        self._load()

    def score(
        self,
        query: str,
        documents: Sequence[RetrievedDocument],
    ) -> list[float]:
        """同步计算 query-document relevance scores，供线程池调用。"""
        self._load()
        assert self._tokenizer is not None
        assert self._model is not None
        assert self._device is not None

        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("Torch is required for reranker inference.") from exc

        scores: list[float] = []
        for start in range(0, len(documents), self.batch_size):
            batch = documents[start:start + self.batch_size]
            pairs = [[query, document.text] for document in batch]
            inputs = self._tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            inputs = {
                key: value.to(self._device)
                for key, value in inputs.items()
            }
            with torch.inference_mode():
                logits = self._model(**inputs, return_dict=True).logits
            scores.extend(logits.view(-1).float().cpu().tolist())
        return scores


class RerankerService:
    """异步 BGE cross-encoder 精排服务。"""

    def __init__(
        self,
        model: CrossEncoderModel | None = None,
        *,
        settings: RAGSettings | None = None,
    ) -> None:
        self.settings = settings or get_rag_settings()
        self.input_top_k = self.settings.rag_reranker_input_top_k
        self.output_top_k = self.settings.rag_reranker_output_top_k
        self._model = model or CrossEncoderModel(self.settings)
        self._inference_slots = Semaphore(
            self.settings.rag_reranker_max_concurrency
        )
        if self.input_top_k < self.output_top_k:
            raise ValueError(
                "RAG_RERANKER_INPUT_TOP_K must be greater than or equal to "
                "RAG_RERANKER_OUTPUT_TOP_K."
            )
        log_event(
            logger,
            logging.INFO,
            "rag.reranker.ready",
            input_top_k=self.input_top_k,
            output_top_k=self.output_top_k,
            batch_size=self.settings.rag_reranker_batch_size,
        )

    def warm_up(self) -> None:
        """同步预热模型，建议仅在应用启动阶段调用。"""
        self._model.warm_up()

    def _rerank_sync(
        self,
        query: str,
        documents: Sequence[RetrievedDocument],
        output_top_k: int,
    ) -> list[RerankedDocument]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty.")
        if not documents:
            return []
        candidates = list(documents[:self.input_top_k])
        with self._inference_slots:
            scores = self._model.score(normalized_query, candidates)
        ranked = sorted(
            zip(candidates, scores),
            key=lambda item: (-item[1], item[0].rank, item[0].chunk_id),
        )[:output_top_k]
        return [
            RerankedDocument.from_retrieved_document(
                document,
                rerank_rank=rank,
                rerank_score=float(score),
            )
            for rank, (document, score) in enumerate(ranked, start=1)
        ]

    async def arerank(
        self,
        query: str,
        documents: Sequence[RetrievedDocument],
        *,
        top_k: int | None = None,
    ) -> list[RerankedDocument]:
        """异步对 dense Top-20 召回结果精排并返回 Top-5。"""
        output_top_k = top_k if top_k is not None else self.output_top_k
        if output_top_k < 1:
            raise ValueError("top_k must be at least 1.")
        if output_top_k > self.input_top_k:
            raise ValueError(
                "top_k cannot be greater than RAG_RERANKER_INPUT_TOP_K."
            )
        results = await asyncio.to_thread(
            self._rerank_sync,
            query,
            documents,
            output_top_k,
        )
        log_event(
            logger,
            logging.INFO,
            "rag.reranker.completed",
            query_length=len(query.strip()),
            candidates=min(len(documents), self.input_top_k),
            results=len(results),
        )
        return results


def get_reranker_service(*, warm_up: bool = False) -> RerankerService:
    """返回进程级 reranker 服务单例。"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = RerankerService()
    if warm_up:
        _service.warm_up()
    return _service


async def arerank_documents(
    query: str,
    documents: Sequence[RetrievedDocument],
    *,
    top_k: int | None = None,
) -> list[RerankedDocument]:
    """对传入的 dense 召回文档执行异步 cross-encoder 精排。"""
    return await get_reranker_service().arerank(
        query,
        documents,
        top_k=top_k,
    )


async def retrieve_and_rerank(
    query: str,
) -> list[RerankedDocument]:
    """执行 dense Top-20 召回，再执行 cross-encoder Top-5 精排。"""
    documents = await aretrieve_documents(
        query,
        top_k=get_rag_settings().rag_reranker_input_top_k,
    )
    return await arerank_documents(query, documents)
