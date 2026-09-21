# 模块职责：集中定义应用、数据库、缓存、FAQ、RAG、知识库和 LLM 的环境配置。

from functools import lru_cache
import re
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 3001
    log_level: str = "INFO"

    chatwoot_base_url: str = "http://127.0.0.1:3000"
    chatwoot_api_token: str = ""
    chatwoot_account_id: str = ""
    chatwoot_webhook_secret: str = ""

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "cs"
    mysql_user: str = "cs"
    mysql_password: str = "cs_password"
    mysql_pool_name: str = "ics_agent_mysql"
    mysql_pool_size: int = Field(default=20, ge=1, le=20)
    mysql_connection_timeout: int = Field(default=5, ge=1)

    redis_url: str = "redis://127.0.0.1:6380/0"
    redis_key_prefix: str = "ics_agent:"
    redis_connect_timeout: float = Field(default=0.5, gt=0)
    redis_socket_timeout: float = Field(default=0.5, gt=0)
    redis_failure_cooldown_seconds: float = Field(default=30.0, gt=0)

    faq_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    faq_bm25_k1: float = Field(default=1.5, gt=0.0)
    faq_bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)
    faq_sync_on_startup: bool = True
    faq_source_table: str = "faq_qna_optimized"
    faq_source_xlsx_path: str = "database/最新问答对.xlsx"
    faq_clear_answer_cache_on_sync: bool = True
    faq_terms: str = "兴聊,言聊,星盈通"
    faq_question_ids_key: str = "faq:question_ids"
    faq_questions_key: str = "faq:questions"
    faq_question_tokens_key: str = "faq:question_tokens"
    faq_questions_meta_key: str = "faq:questions_meta"
    faq_query_answers_key: str = "faq:query_answers"
    faq_cache_index_key: str = "faq:query_answers:index"
    faq_cache_ttl_seconds: int = Field(default=86400, ge=1)
    faq_cache_max_entries: int = Field(default=10000, ge=1, le=10000)

    rag_pretrained_model_dir: str = (
        "models/google-bert--bert-base-chinese/snapshots/master"
    )
    rag_output_dir: str = "models/intent_classifier_bert"
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

    knowledge_base_input_dir: str = "database/knowledge_base"
    knowledge_base_output_path: str = "knowledge_base_in/output/chunks.jsonl"
    knowledge_base_chunk_size_tokens: int = Field(default=256, ge=1)
    knowledge_base_chunk_overlap_tokens: int = Field(default=32, ge=0)
    knowledge_base_tokenizer_dir: str = (
        "models/google-bert--bert-base-chinese/snapshots/master"
    )
    knowledge_base_include_heading_context: bool = True
    knowledge_base_drop_heading_only_chunks: bool = True
    knowledge_base_ocr_enabled: bool = True
    knowledge_base_ocr_text_score: float = Field(default=0.5, ge=0.0, le=1.0)

    llm_model: str = "openai:gpt-4o-mini"
    llm_model_provider: str = ""
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1024, ge=1)
    llm_timeout: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_max_concurrency: int = Field(default=5, ge=1)
    llm_concurrency_wait_timeout: float = Field(default=5.0, gt=0)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_structured_method: str = "function_calling"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def faq_terms_list(self) -> tuple[str, ...]:
        return tuple(term.strip() for term in self.faq_terms.split(",") if term.strip())

    @property
    def faq_table_name(self) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.faq_source_table):
            raise ValueError("FAQ_SOURCE_TABLE must be a valid SQL table name.")
        return self.faq_source_table


@lru_cache
def get_settings() -> Settings:
    return Settings()
