# 模块职责：集中定义意图识别、知识库、向量检索和重排的 RAG 配置。

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ics_agent.app.config import PROJECT_ROOT


class RAGSettings(BaseSettings):
    """RAG model, document-processing and retrieval configuration."""

    # Intent classification.
    rag_pretrained_model_dir: str = (
        "models/google-bert--bert-base-chinese/snapshots/master"
    )
    rag_output_dir: str = "models/intent_classifier_bert"
    rag_warm_up_on_startup: bool = True
    rag_num_labels: int = Field(default=4, ge=2)
    rag_max_length: int = Field(default=128, ge=8, le=512)
    rag_num_train_epochs: float = Field(default=3.0, gt=0)
    rag_learning_rate: float = Field(default=2e-5, gt=0)
    rag_train_batch_size: int = Field(default=8, ge=1)
    rag_eval_batch_size: int = Field(default=8, ge=1)
    rag_weight_decay: float = Field(default=0.01, ge=0)
    rag_warmup_ratio: float = Field(default=0.1, ge=0, le=1)
    rag_logging_steps: int = Field(default=10, ge=1)
    rag_save_total_limit: int = Field(default=1, ge=1)
    rag_seed: int = Field(default=42, ge=0)
    rag_eval_ratio: float = Field(default=0.2, gt=0, lt=1)
    rag_eval_dataset_path: str = "database/eval.csv"

    # Knowledge-base parsing and dense vector store.
    knowledge_base_input_dir: str = "database/knowledge_base"
    knowledge_base_output_path: str = "knowledge_base_in/output/chunks.jsonl"
    knowledge_base_chunk_size_tokens: int = Field(default=512, ge=1)
    knowledge_base_chunk_overlap_tokens: int = Field(default=64, ge=0)
    knowledge_base_tokenizer_dir: str = (
        "models/google-bert--bert-base-chinese/snapshots/master"
    )
    knowledge_base_include_heading_context: bool = True
    knowledge_base_drop_heading_only_chunks: bool = True
    knowledge_base_preserve_markdown_tables: bool = True
    knowledge_base_ocr_enabled: bool = True
    knowledge_base_ocr_text_score: float = Field(default=0.5, ge=0.0, le=1.0)
    knowledge_base_embedding_model_dir: str = "models/bge-m3"
    knowledge_base_vector_db_dir: str = "knowledge_base_in/vector_store/chroma"
    knowledge_base_vector_collection: str = "knowledge_base"
    knowledge_base_embedding_batch_size: int = Field(default=8, ge=1)
    knowledge_base_embedding_max_length: int = Field(default=8192, ge=8)
    knowledge_base_chroma_upsert_batch_size: int = Field(default=128, ge=1)
    knowledge_base_retrieval_top_k: int = Field(default=10, ge=1)

    # Cross-encoder reranker.
    rag_reranker_model_dir: str = "models/bge-reranker-v2-m3"
    rag_reranker_input_top_k: int = Field(default=20, ge=1)
    rag_reranker_output_top_k: int = Field(default=3, ge=1)
    rag_reranker_batch_size: int = Field(default=8, ge=1)
    rag_reranker_max_length: int = Field(default=512, ge=8, le=8192)
    rag_reranker_use_fp16: bool = True
    rag_reranker_max_concurrency: int = Field(default=1, ge=1)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_rag_settings() -> RAGSettings:
    """Return the process-level RAG settings singleton."""
    return RAGSettings()
