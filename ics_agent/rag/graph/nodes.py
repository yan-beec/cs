# 模块职责：实现 RAG MVP 图的意图识别、召回、精排和大模型回复节点。

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from ics_agent.app.config import PROJECT_ROOT
from ics_agent.app.logging_config import log_event
from ics_agent.rag.config import RAGSettings, get_rag_settings
from ics_agent.llm_factory import call_cloud_llm
from ics_agent.rag.prompt import SYSTEM_PROMPT, build_dynamic_prompt
from ics_agent.rag.reranker import arerank_documents
from ics_agent.rag.retrieval import aretrieve_documents

from .state import RAGState


logger = logging.getLogger(__name__)


class IntentClassifierModel:
    """加载并复用微调后的 BERT 意图分类模型。"""

    _model_cache: dict[tuple[str, str], Any] = {}
    _model_cache_lock = Lock()

    def __init__(self, settings: RAGSettings | None = None) -> None:
        self.settings = settings or get_rag_settings()
        self.model_dir = self._resolve_project_path(
            self.settings.rag_output_dir
        )
        if not self.model_dir.is_dir():
            raise FileNotFoundError(
                f"Intent classifier model directory does not exist: {self.model_dir}"
            )
        self.max_length = self.settings.rag_max_length
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._device: Any | None = None
        self._id_to_label: dict[int, str] = {}

    @staticmethod
    def _resolve_project_path(path_value: str | Path) -> Path:
        path = Path(path_value)
        return path if path.is_absolute() else PROJECT_ROOT / path

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
                "Torch and Transformers are required for intent recognition. "
                "Install requirements.txt in the cs environment."
            ) from exc

        device_name = "cuda" if torch.cuda.is_available() else "cpu"
        cache_key = (str(self.model_dir), device_name)
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
                cached = (tokenizer, model, device)
                self._model_cache[cache_key] = cached
                loaded = True
            else:
                loaded = False

        self._tokenizer, self._model, self._device = cached
        self._id_to_label = self._load_label_mapping(self.model_dir)
        log_event(
            logger,
            logging.INFO,
            (
                "rag.graph.intent_model.loaded"
                if loaded
                else "rag.graph.intent_model.reused"
            ),
            model_dir=self.model_dir,
            device=device_name,
            label_count=len(self._id_to_label),
        )

    @staticmethod
    def _load_label_mapping(model_dir: Path) -> dict[int, str]:
        labels_path = model_dir / "labels.json"
        if labels_path.is_file():
            payload = json.loads(labels_path.read_text(encoding="utf-8"))
            mapping = payload.get("id2label") or {}
            return {
                int(label_id): str(label)
                for label_id, label in mapping.items()
            }

        config_path = model_dir / "config.json"
        if not config_path.is_file():
            raise FileNotFoundError(
                f"Intent classifier label metadata does not exist: {model_dir}"
            )
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return {
            int(label_id): str(label)
            for label_id, label in (payload.get("id2label") or {}).items()
        }

    def warm_up(self) -> None:
        """在应用启动时加载意图模型。"""
        self._load()

    def _predict_sync(
        self,
        query: str,
    ) -> tuple[int, str, dict[str, float], float]:
        self._load()
        assert self._tokenizer is not None
        assert self._model is not None
        assert self._device is not None

        import torch

        encoded = self._tokenizer(
            [query],
            truncation=True,
            max_length=self.max_length,
            padding=True,
            return_tensors="pt",
        )
        encoded = {
            key: value.to(self._device)
            for key, value in encoded.items()
        }
        with torch.inference_mode():
            probabilities = torch.softmax(
                self._model(**encoded).logits,
                dim=-1,
            )[0]
        predicted_id = int(probabilities.argmax().item())
        probability_values = probabilities.cpu().tolist()
        probability_map = {
            self._id_to_label.get(index, str(index)): float(probability)
            for index, probability in enumerate(probability_values)
        }
        return (
            predicted_id,
            self._id_to_label.get(predicted_id, str(predicted_id)),
            probability_map,
            float(probability_values[predicted_id]),
        )

    async def predict(
        self,
        query: str,
    ) -> tuple[int, str, dict[str, float], float]:
        """异步执行意图分类，不阻塞事件循环。"""
        return await asyncio.to_thread(self._predict_sync, query)


_intent_model: IntentClassifierModel | None = None
_intent_model_lock = Lock()


def get_intent_classifier(*, warm_up: bool = False) -> IntentClassifierModel:
    """返回进程级意图分类模型单例。"""
    global _intent_model
    if _intent_model is None:
        with _intent_model_lock:
            if _intent_model is None:
                _intent_model = IntentClassifierModel()
    if warm_up:
        _intent_model.warm_up()
    return _intent_model


async def intent_recognition_node(state: RAGState) -> dict[str, Any]:
    """识别用户意图，暂不基于结果做条件分支。"""
    query = state.get("query", "").strip()
    if not query:
        raise ValueError("RAG state query cannot be empty.")

    intent_id, intent_label, probabilities, confidence = await (
        get_intent_classifier().predict(query)
    )
    log_event(
        logger,
        logging.INFO,
        "rag.graph.node.intent.completed",
        intent_label=intent_label,
        confidence=round(confidence, 4),
    )
    return {
        "intent_id": intent_id,
        "intent_label": intent_label,
        "intent_probabilities": probabilities,
        "intent_confidence": confidence,
        "route": "rag",
    }


async def retrieval_node(state: RAGState) -> dict[str, Any]:
    """使用 BGE-M3 从 ChromaDB 召回 dense Top-20。"""
    query = state.get("query", "").strip()
    if not query:
        raise ValueError("RAG state query cannot be empty.")

    settings = get_rag_settings()
    documents = await aretrieve_documents(
        query,
        top_k=settings.knowledge_base_retrieval_top_k,
    )
    log_event(
        logger,
        logging.INFO,
        "rag.graph.node.retrieval.completed",
        candidates=len(documents),
    )
    return {"retrieved_documents": documents}


async def reranker_node(state: RAGState) -> dict[str, Any]:
    """对召回结果执行 cross-encoder 精排并保留 Top-5。"""
    query = state.get("query", "").strip()
    documents = state.get("retrieved_documents", [])
    if not query:
        raise ValueError("RAG state query cannot be empty.")

    reranked_documents = await arerank_documents(
        query,
        documents,
        top_k=get_rag_settings().rag_reranker_output_top_k,
    )
    log_event(
        logger,
        logging.INFO,
        "rag.graph.node.reranker.completed",
        candidates=len(documents),
        results=len(reranked_documents),
    )
    return {"reranked_documents": reranked_documents}


def _response_content(response: Any) -> str:
    """Extract text from a LangChain response while tolerating provider formats."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    return str(content)


async def llm_reply_node(state: RAGState) -> dict[str, Any]:
    """基于精排后的 Top-5 文档调用云端 LLM 生成回复。"""
    query = state.get("query", "").strip()
    if not query:
        raise ValueError("RAG state query cannot be empty.")

    response = await call_cloud_llm(
        build_dynamic_prompt(state),
        system_prompt=SYSTEM_PROMPT,
    )
    answer = _response_content(response)
    log_event(
        logger,
        logging.INFO,
        "rag.graph.node.llm_reply.completed",
        answer_length=len(answer),
    )
    return {
        "llm_response": response,
        "answer": answer,
    }
