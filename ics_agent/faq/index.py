# 模块职责：提供 FAQ 分词、BM25 索引和匹配度计算。

import logging
import math
from collections import Counter
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

from ics_agent.app.logging_config import log_event

try:
    from .models import FAQMatch, FAQSnapshot
except ImportError:
    from models import FAQMatch, FAQSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.faq.config import get_faq_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.faq.config import get_faq_settings


logger = logging.getLogger(__name__)


class JiebaTokenizer:
    """使用配置的产品词对 FAQ 问题和查询进行分词。"""

    def __init__(self, terms: tuple[str, ...] | None = None) -> None:
        jieba.default_logger.setLevel(logging.WARNING)
        self._terms = (
            terms
            if terms is not None
            else get_faq_settings().faq_terms_list
        )
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


def match_degree(
    score: float,
    query_tokens: list[str],
    matched_tokens: list[str],
) -> float:
    if not query_tokens:
        return 0.0

    query_counts = Counter(query_tokens)
    matched_counts = Counter(matched_tokens)
    overlap_count = sum(
        min(count, matched_counts[token])
        for token, count in query_counts.items()
    )
    token_coverage = overlap_count / len(query_tokens)
    bm25_confidence = 1.0 - math.exp(-max(score, 0.0))
    return bm25_confidence * token_coverage


class BM25Matcher:
    def __init__(self, snapshot: FAQSnapshot) -> None:
        if not snapshot.documents:
            raise ValueError("Cannot initialize BM25 with an empty FAQ snapshot.")

        settings = get_faq_settings()
        self._snapshot = snapshot
        self._index = BM25Okapi(
            snapshot.tokenized_questions,
            k1=settings.faq_bm25_k1,
            b=settings.faq_bm25_b,
        )
        log_event(
            logger,
            logging.INFO,
            "faq.bm25.initialized",
            document_count=len(snapshot.documents),
            vocabulary_size=len(self._index.idf),
        )

    @property
    def snapshot(self) -> FAQSnapshot:
        return self._snapshot

    @property
    def vocabulary_size(self) -> int:
        return len(self._index.idf)

    def match(self, query_tokens: list[str]) -> FAQMatch | None:
        if not query_tokens:
            return None

        scores = self._index.get_scores(query_tokens)
        matched_index = max(range(len(scores)), key=lambda index: scores[index])
        document = self._snapshot.documents[matched_index]
        raw_score = float(scores[matched_index])
        return FAQMatch(
            document=document,
            bm25_score=raw_score,
            match_degree=match_degree(
                raw_score,
                query_tokens,
                list(document.tokens),
            ),
        )
