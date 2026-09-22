# 模块职责：集中定义 FAQ、BM25、Redis 快照和 FAQ 缓存的模块配置。

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ics_agent.app.config import PROJECT_ROOT


class FAQSettings(BaseSettings):
    """FAQ behavior and data-source configuration."""

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
    faq_cache_ttl_seconds: int = Field(default=864000, ge=1)
    faq_cache_max_entries: int = Field(default=10000, ge=1, le=10000)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def faq_terms_list(self) -> tuple[str, ...]:
        return tuple(
            term.strip()
            for term in self.faq_terms.split(",")
            if term.strip()
        )

    @property
    def faq_table_name(self) -> str:
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            self.faq_source_table,
        ):
            raise ValueError("FAQ_SOURCE_TABLE must be a valid SQL table name.")
        return self.faq_source_table


@lru_cache
def get_faq_settings() -> FAQSettings:
    """Return the process-level FAQ settings singleton."""
    return FAQSettings()
