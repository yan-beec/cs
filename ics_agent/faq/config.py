# 模块职责：集中定义 FAQ、BM25 和数据源配置。

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ics_agent.app.core.config import PROJECT_ROOT
from ics_agent.channels import DEFAULT_CHANNEL, get_channel


class FAQSettings(BaseSettings):
    """FAQ 行为和数据源配置。"""

    faq_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    faq_bm25_k1: float = Field(default=1.5, gt=0.0)
    faq_bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)
    faq_source_table: str = "faq_channel_xingliao"
    faq_source_xlsx_path: str = "database/channel_xingliao/最新问答对.xlsx"
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
def get_faq_settings(channel_key: str | None = None) -> FAQSettings:
    """每个渠道绑定独立资源；兴聊保留显式环境变量覆盖能力。"""
    channel = get_channel(channel_key)
    settings = FAQSettings()
    values = {
        "faq_source_table": channel.faq_table,
        "faq_source_xlsx_path": channel.faq_workbook,
    }
    if channel.key == DEFAULT_CHANNEL:
        legacy_values = {"faq_source_table": "faq_qna_optimized",
                         "faq_source_xlsx_path": "database/最新问答对.xlsx"}
        values = {key: value for key, value in values.items()
                  if key not in settings.model_fields_set
                  or getattr(settings, key) == legacy_values[key]}
    return settings.model_copy(update=values)
