# 模块职责：集中定义意图识别、知识库、向量检索和重排的 RAG 配置。

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ics_agent.app.core.config import PROJECT_ROOT


class RAGSettings(BaseSettings):
    """RAG 模型、文档处理和检索配置。"""

    # 意图分类。
    rag_pretrained_model_dir: str = (
        "models/google-bert--bert-base-chinese/snapshots/master"
    )
    rag_output_dir: str = "models/intent_classifier_bert"
    rag_intent_dataset_path: str = "database/intent_dataset_4000.csv"
    rag_intent_dataset_verify_fingerprint: bool = True
    rag_warm_up_on_startup: bool = True
    rag_memory_enabled: bool = True
    rag_memory_window_turns: int = Field(default=10, ge=1)
    rag_memory_summary_interval: int = Field(default=10, ge=1)
    # 0 表示永久保留会话记忆，不执行按时间清理。
    rag_memory_retention_days: int = Field(default=0, ge=0)
    rag_memory_cleanup_interval_seconds: int = Field(
        default=86400,
        ge=60,
    )
    rag_memory_cleanup_batch_size: int = Field(default=500, ge=1, le=5000)
    webhook_worker_count: int = Field(default=2, ge=1, le=16)
    webhook_queue_max_size: int = Field(default=1000, ge=1, le=10000)
    # The RAG LLM call may use the provider's initial attempt plus two
    # internal retries, and the reply node itself reconnects twice with 1 s
    # and 3 s delays. With the default 30 s provider timeout, the complete
    # fallback path can therefore approach five minutes; keep the outer
    # deadline above that window so llm_reply_node can return its fallback
    # answer instead of being cancelled first.
    # Keep the per-message RAG deadline below the outer webhook deadline so a
    # stalled retrieval/model call can produce a fallback answer and release
    # the conversation partition instead of blocking later messages.
    webhook_rag_timeout_seconds: float = Field(default=120.0, gt=0)
    webhook_processing_timeout_seconds: float = Field(default=300.0, gt=0)
    webhook_max_retries: int = Field(default=3, ge=0, le=10)
    webhook_retry_initial_delay_seconds: float = Field(default=2.0, gt=0)
    webhook_retry_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    webhook_retry_max_delay_seconds: float = Field(default=30.0, gt=0)
    webhook_queue_shutdown_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
    )
    webhook_idempotency_retention_days: int = Field(default=7, ge=1)
    webhook_idempotency_stale_after_seconds: int = Field(
        default=300,
        ge=60,
    )
    webhook_idempotency_cleanup_interval_seconds: int = Field(
        default=86400,
        ge=60,
    )
    webhook_idempotency_cleanup_batch_size: int = Field(
        default=1000,
        ge=1,
        le=10000,
    )
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

    # 知识库解析和稠密向量库。
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
    knowledge_base_ocr_cpu_threads: int = Field(default=2, ge=1, le=32)
    knowledge_base_ocr_cpu_interop_threads: int = Field(default=1, ge=1, le=32)
    knowledge_base_embedding_model_dir: str = "models/bge-m3"
    knowledge_base_vector_db_dir: str = "knowledge_base_in/vector_store/chroma"
    knowledge_base_vector_collection: str = "knowledge_base"
    knowledge_base_embedding_batch_size: int = Field(default=8, ge=1)
    knowledge_base_embedding_max_length: int = Field(default=8192, ge=8)
    knowledge_base_chroma_upsert_batch_size: int = Field(default=128, ge=1)
    knowledge_base_embedding_cpu_threads: int = Field(default=2, ge=1, le=32)
    knowledge_base_embedding_cpu_interop_threads: int = Field(
        default=1,
        ge=1,
        le=32,
    )
    # 在线查询将短时间内到达的 query 合并为一次 BGE-M3 encode 调用。
    # 这是批处理等待窗口和单批上限，不是整个 RAG 请求的超时。
    knowledge_base_embedding_micro_batch_wait_ms: int = Field(
        default=20,
        ge=0,
        le=1000,
    )
    knowledge_base_embedding_micro_batch_max_size: int = Field(
        default=4,
        ge=1,
        le=64,
    )
    knowledge_base_embedding_batch_pause_seconds: float = Field(
        default=0.1,
        ge=0,
        le=60,
    )
    knowledge_base_retrieval_top_k: int = Field(default=8, ge=1)

    # Cross-encoder 重排器。
    rag_reranker_model_dir: str = "models/bge-reranker-v2-m3"
    rag_reranker_input_top_k: int = Field(default=8, ge=1)
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
    """返回进程级 RAG 设置单例。"""
    return RAGSettings()
