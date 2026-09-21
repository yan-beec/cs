# 模块职责：使用 Jieba 对 FAQ 问题和用户查询进行分词。

import logging
from pathlib import Path

import jieba

from ics_agent.app.logging_config import log_event

PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

try:
    from ics_agent.app.config import get_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings


class JiebaTokenizer:
    def __init__(self, terms: tuple[str, ...] | None = None) -> None:
        jieba.default_logger.setLevel(logging.WARNING)
        self._terms = terms if terms is not None else get_settings().faq_terms_list
        self._register_terms()
        log_event(
            logger,
            logging.DEBUG,
            "faq.tokenizer.initialized",
            custom_term_count=len(self._terms),
        )

    def _register_terms(self) -> None:
        for term in self._terms:
            jieba.add_word(term, freq=200000, tag="nz")

    def tokenize(self, text: str) -> list[str]:
        return [token.strip() for token in jieba.lcut(text) if token.strip()]

    def tokenize_many(self, texts: list[str]) -> list[list[str]]:
        return [self.tokenize(text) for text in texts]
