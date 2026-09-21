# 模块职责：编排 FAQ 初始化、缓存查询、BM25 匹配、MySQL 取答和 RAG 降级。

import logging
import threading
import time
from pathlib import Path

import redis
from mysql.connector.errors import Error as MySQLError

from ics_agent.app.logging_config import log_event

try:
    from .index import BM25Matcher
    from .models import FAQResult, FAQSnapshot
    from .repositories import (
        FAQAnswerCache,
        FAQRepository,
        RedisCircuitOpen,
        RedisFAQSnapshotRepository,
    )
    from .tokenizer import JiebaTokenizer
except ImportError:
    from index import BM25Matcher
    from models import FAQResult, FAQSnapshot
    from repositories import (
        FAQAnswerCache,
        FAQRepository,
        RedisCircuitOpen,
        RedisFAQSnapshotRepository,
    )
    from tokenizer import JiebaTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.config import get_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings


logger = logging.getLogger(__name__)


class FAQSnapshotSynchronizer:
    def __init__(
        self,
        faq_repository: FAQRepository | None = None,
        snapshot_repository: RedisFAQSnapshotRepository | None = None,
        tokenizer: JiebaTokenizer | None = None,
    ) -> None:
        self.faq_repository = faq_repository or FAQRepository()
        self.snapshot_repository = (
            snapshot_repository or RedisFAQSnapshotRepository()
        )
        self.tokenizer = tokenizer or JiebaTokenizer()
        self.last_redis_save_succeeded = False

    def sync(self, include_disabled: bool = False) -> FAQSnapshot:
        log_event(
            logger,
            logging.INFO,
            "faq.snapshot.sync.start",
            include_disabled=include_disabled,
        )
        rows = self.faq_repository.fetch_questions(include_disabled)
        question_ids = [row[0] for row in rows]
        questions = [row[1] for row in rows]
        tokens = self.tokenizer.tokenize_many(questions)
        snapshot = FAQSnapshot.from_lists(question_ids, questions, tokens)
        self.last_redis_save_succeeded = False
        try:
            self.snapshot_repository.save(snapshot)
            self.last_redis_save_succeeded = True
        except redis.RedisError as exc:
            log_event(
                logger,
                logging.WARNING,
                "faq.snapshot.sync.redis_write_failed",
                error_type=type(exc).__name__,
            )
        log_event(
            logger,
            logging.INFO,
            "faq.snapshot.sync.success",
            count=len(snapshot.documents),
        )
        return snapshot


class FAQService:
    def __init__(
        self,
        faq_repository: FAQRepository | None = None,
        snapshot_repository: RedisFAQSnapshotRepository | None = None,
        answer_cache: FAQAnswerCache | None = None,
        tokenizer: JiebaTokenizer | None = None,
        synchronizer: FAQSnapshotSynchronizer | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._faq_repository = faq_repository or FAQRepository()
        self._snapshot_repository = (
            snapshot_repository or RedisFAQSnapshotRepository()
        )
        redis_circuit = getattr(self._snapshot_repository, "circuit", None)
        self._answer_cache = answer_cache or FAQAnswerCache(
            self._snapshot_repository.client,
            circuit=redis_circuit,
        )
        self._tokenizer = tokenizer or JiebaTokenizer()
        self._synchronizer = synchronizer or FAQSnapshotSynchronizer(
            faq_repository=self._faq_repository,
            snapshot_repository=self._snapshot_repository,
            tokenizer=self._tokenizer,
        )
        self._matcher: BM25Matcher | None = None
        self._initialization_error: Exception | None = None

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def matcher(self) -> BM25Matcher:
        if self._matcher is None:
            raise RuntimeError("FAQ service has not been initialized.")
        return self._matcher

    def reload_snapshot(self, snapshot: FAQSnapshot) -> "FAQService":
        """Replace the in-memory BM25 matcher with an already refreshed snapshot."""
        matcher = BM25Matcher(snapshot)
        with self._lock:
            self._matcher = matcher
            self._initialization_error = None
            self._initialized = True
        log_event(
            logger,
            logging.INFO,
            "faq.service.bm25.reload.success",
            count=len(snapshot.documents),
            vocabulary_size=matcher.vocabulary_size,
        )
        return self

    def initialize(self, force: bool = False) -> "FAQService":
        if self._initialized and not force:
            return self
        with self._lock:
            if self._initialized and not force:
                return self

            settings = get_settings()
            log_event(
                logger,
                logging.INFO,
                "faq.service.initialize.start",
                sync_on_startup=settings.faq_sync_on_startup,
            )
            try:
                if settings.faq_sync_on_startup:
                    try:
                        snapshot = self._synchronizer.sync()
                    except (MySQLError, OSError, TimeoutError) as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "faq.service.initialize.mysql_sync_failed",
                            error_type=type(exc).__name__,
                        )
                        snapshot = self._snapshot_repository.load()
                        log_event(
                            logger,
                            logging.INFO,
                            "faq.service.initialize.redis_snapshot_fallback",
                            count=len(snapshot.documents),
                        )
                else:
                    snapshot = self._snapshot_repository.load()
                self._matcher = BM25Matcher(snapshot)
                self._initialization_error = None
                log_event(
                    logger,
                    logging.INFO,
                    "faq.service.initialize.success",
                    count=len(snapshot.documents),
                    vocabulary_size=self._matcher.vocabulary_size,
                )
            except (
                MySQLError,
                OSError,
                TimeoutError,
                KeyError,
                ValueError,
                redis.RedisError,
            ) as exc:
                self._matcher = None
                self._initialization_error = exc
                log_event(
                    logger,
                    logging.ERROR,
                    "faq.service.initialize.failed",
                    error_type=type(exc).__name__,
                )
            self._initialized = True
            return self

    def query(self, query: str, threshold: float | None = None) -> FAQResult:
        started_at = time.perf_counter()
        self.initialize()
        query = query.strip()
        if not query:
            raise ValueError("User query cannot be empty.")
        log_event(
            logger,
            logging.DEBUG,
            "faq.query.start",
            query_length=len(query),
        )

        if self._matcher is None:
            log_event(
                logger,
                logging.WARNING,
                "faq.query.route",
                route="rag",
                reason="matcher_unavailable",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return FAQResult(route="rag", query=query, rag_query=query)

        try:
            cached_answer = self._answer_cache.get(query)
        except RedisCircuitOpen:
            log_event(
                logger,
                logging.DEBUG,
                "faq.query.redis_cache_read_skipped",
                reason="circuit_open",
            )
            cached_answer = None
        except redis.RedisError as exc:
            log_event(
                logger,
                logging.WARNING,
                "faq.query.redis_cache_read_failed",
                error_type=type(exc).__name__,
            )
            cached_answer = None

        if cached_answer is not None:
            log_event(
                logger,
                logging.INFO,
                "faq.query.route",
                route="cache",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return FAQResult(
                route="cache",
                query=query,
                answer=cached_answer,
                cache_key=self._answer_cache.key,
            )

        tokens = self._tokenizer.tokenize(query)
        if not tokens:
            log_event(
                logger,
                logging.INFO,
                "faq.query.route",
                route="rag",
                reason="empty_tokens",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return FAQResult(route="rag", query=query, rag_query=query)

        match = self.matcher.match(tokens)
        settings = get_settings()
        match_threshold = (
            settings.faq_match_threshold if threshold is None else threshold
        )
        if match is None or match.match_degree < match_threshold:
            log_event(
                logger,
                logging.INFO,
                "faq.query.route",
                route="rag",
                reason="below_threshold" if match else "no_match",
                match_degree=None if match is None else round(match.match_degree, 4),
                threshold=match_threshold,
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return FAQResult(
                route="rag",
                query=query,
                bm25_score=None if match is None else match.bm25_score,
                match_degree=None if match is None else match.match_degree,
                rag_query=query,
            )

        try:
            _, answer = self._faq_repository.fetch_answer(match.document.question_id)
        except (MySQLError, LookupError, OSError, TimeoutError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "faq.query.route",
                route="rag",
                reason="mysql_answer_failed",
                question_id=match.document.question_id,
                error_type=type(exc).__name__,
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return FAQResult(
                route="rag",
                query=query,
                matched_question=match.document.question,
                question_id=match.document.question_id,
                bm25_score=match.bm25_score,
                match_degree=match.match_degree,
                rag_query=query,
            )

        try:
            self._answer_cache.set(query, answer)
        except RedisCircuitOpen:
            log_event(
                logger,
                logging.DEBUG,
                "faq.query.redis_cache_write_skipped",
                reason="circuit_open",
            )
        except redis.RedisError as exc:
            log_event(
                logger,
                logging.WARNING,
                "faq.query.redis_cache_write_failed",
                error_type=type(exc).__name__,
            )

        sql = self._faq_repository.answer_sql()
        log_event(
            logger,
            logging.INFO,
            "faq.query.route",
            route="faq",
            question_id=match.document.question_id,
            match_degree=round(match.match_degree, 4),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )
        return FAQResult(
            route="faq",
            query=query,
            answer=answer,
            matched_question=match.document.question,
            question_id=match.document.question_id,
            bm25_score=match.bm25_score,
            match_degree=match.match_degree,
            mysql_query=f"{sql}  params=({match.document.question_id},)",
            cache_key=self._answer_cache.key,
        )


_service: FAQService | None = None
_service_lock = threading.Lock()


def get_faq_service() -> FAQService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = FAQService()
    return _service
