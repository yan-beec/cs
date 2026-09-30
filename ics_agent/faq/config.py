# 模块职责：集中定义 FAQ、BM25 和数据源配置。

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ics_agent.app.core.config import PROJECT_ROOT


class FAQSettings(BaseSettings):
    """FAQ 行为和数据源配置。"""

    faq_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    faq_bm25_k1: float = Field(default=1.5, gt=0.0)
    faq_bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)
    faq_source_table: str = "faq_qna_optimized"
    faq_source_xlsx_path: str = "database/最新问答对.xlsx"
    faq_terms: str = "兴聊,言聊,星盈通"

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
    """返回进程级 FAQ 设置单例。"""
    return FAQSettings()
