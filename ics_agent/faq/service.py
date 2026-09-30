# 模块职责：编排 FAQ 初始化、异步后台更新、内存 BM25 匹配和 RAG 降级。

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from psycopg import Error as PostgresError
except ImportError:
    class PostgresError(Exception):
        """PostgreSQL 依赖安装前使用的占位类型。"""

from ics_agent.app.core.logging_config import log_event

try:
    from .index import BM25Matcher, JiebaTokenizer
    from .models import FAQResult, FAQSnapshot
    from .repositories import (
        FAQRepository,
        PostgresDependencyError,
        PostgresPoolExhausted,
        ensure_faq_table,
    )
except ImportError:
    from index import BM25Matcher, JiebaTokenizer
    from models import FAQResult, FAQSnapshot
    from repositories import (
        FAQRepository,
        PostgresDependencyError,
        PostgresPoolExhausted,
        ensure_faq_table,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.faq.config import get_faq_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.faq.config import get_faq_settings


logger = logging.getLogger(__name__)


class FAQUpdateInProgress(RuntimeError):
    """已有其他 FAQ 更新任务运行时抛出。"""


@dataclass(frozen=True)
class FAQUpdateStatus:
    """提交后台 FAQ 更新任务后返回的公开状态。"""

    job_id: str
    source_hash: str
    status: str


@dataclass(frozen=True)
class _FAQRuntimeVersion:
    """在下一次原子切换前供查询使用的不可变 FAQ 版本。"""

    matcher: BM25Matcher
    answers: dict[int, str]
    source_hash: str | None


class FAQSnapshotSynchronizer:
    def __init__(
        self,
        faq_repository: FAQRepository | None = None,
        tokenizer: JiebaTokenizer | None = None,
    ) -> None:
        self.faq_repository = faq_repository or FAQRepository()
        self.tokenizer = tokenizer or JiebaTokenizer()

    def build_snapshot(self, rows: list[tuple[int, str]]) -> FAQSnapshot:
        question_ids = [row[0] for row in rows]
        questions = [row[1] for row in rows]
        tokens = self.tokenizer.tokenize_many(questions)
        return FAQSnapshot.from_lists(question_ids, questions, tokens)

    def sync(self, include_disabled: bool = False) -> FAQSnapshot:
        log_event(
            logger,
            logging.INFO,
            "faq.snapshot.sync.start",
            include_disabled=include_disabled,
        )
        rows = self.faq_repository.fetch_questions(include_disabled)
        snapshot = self.build_snapshot(rows)
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
        tokenizer: JiebaTokenizer | None = None,
        synchronizer: FAQSnapshotSynchronizer | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._version_lock = threading.Lock()
        self._update_state_lock = threading.Lock()
        self._active_update_task: asyncio.Task | None = None
        self._active_update_hash: str | None = None
        self._last_completed_hash: str | None = None
        self._update_statuses: dict[str, str] = {}
        self._initialized = False
        self._faq_repository = faq_repository or FAQRepository()
        self._tokenizer = tokenizer or JiebaTokenizer()
        self._synchronizer = synchronizer or FAQSnapshotSynchronizer(
            faq_repository=self._faq_repository,
            tokenizer=self._tokenizer,
        )
        self._matcher: BM25Matcher | None = None
        self._runtime_version: _FAQRuntimeVersion | None = None
        self._initialization_error: Exception | None = None

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def matcher(self) -> BM25Matcher:
        runtime_version = self._get_runtime_version()
        if runtime_version is None:
            raise RuntimeError("FAQ service has not been initialized.")
        return runtime_version.matcher

    def _get_runtime_version(self) -> _FAQRuntimeVersion | None:
        with self._version_lock:
            return self._runtime_version

    def _build_runtime_version(
        self,
        snapshot: FAQSnapshot,
        *,
        answers: dict[int, str],
        source_hash: str | None = None,
    ) -> _FAQRuntimeVersion:
        return _FAQRuntimeVersion(
            matcher=BM25Matcher(snapshot),
            answers=answers,
            source_hash=source_hash,
        )

    def _activate_runtime_version(
        self,
        runtime_version: _FAQRuntimeVersion,
    ) -> None:
        """通过一次指针切换发布一个完整 FAQ 版本。"""
        with self._version_lock:
            self._runtime_version = runtime_version
            self._matcher = runtime_version.matcher
            self._initialization_error = None
            self._initialized = True

    def reload_snapshot(
        self,
        snapshot: FAQSnapshot,
        *,
        answers: dict[int, str],
        source_hash: str | None = None,
    ) -> "FAQService":
        """原子发布已经准备好的 FAQ 运行时版本。"""
        runtime_version = self._build_runtime_version(
            snapshot,
            answers=answers,
            source_hash=source_hash,
        )
        self._activate_runtime_version(runtime_version)
        log_event(
            logger,
            logging.INFO,
            "faq.service.bm25.reload.success",
            count=len(snapshot.documents),
            vocabulary_size=runtime_version.matcher.vocabulary_size,
        )
        return self

    def initialize(self, force: bool = False) -> "FAQService":
        if self._initialized and not force:
            return self
        with self._lock:
            if self._initialized and not force:
                return self

            log_event(
                logger,
                logging.INFO,
                "faq.service.initialize.start",
            )
            try:
                ensure_faq_table()
                snapshot = self._synchronizer.sync()
                runtime_answers = self._faq_repository.fetch_answers(
                    snapshot.question_ids
                )
                if len(runtime_answers) != len(snapshot.documents):
                    missing_ids = [
                        question_id
                        for question_id in snapshot.question_ids
                        if question_id not in runtime_answers
                    ]
                    raise LookupError(
                        "FAQ answers are incomplete for question ids: "
                        + ", ".join(str(question_id) for question_id in missing_ids)
                    )
                if (
                    not force
                    and self._initialized
                    and self._runtime_version is not None
                ):
                    # 启动初始化读取旧版本时，后台刷新可能已经发布了更新版本。
                    return self
                runtime_version = self._build_runtime_version(
                    snapshot,
                    answers=runtime_answers,
                )
                self._activate_runtime_version(runtime_version)
                log_event(
                    logger,
                    logging.INFO,
                    "faq.service.initialize.success",
                    count=len(snapshot.documents),
                    vocabulary_size=runtime_version.matcher.vocabulary_size,
                    answers_in_memory=True,
                )
            except (
                PostgresError,
                PostgresDependencyError,
                PostgresPoolExhausted,
                LookupError,
                OSError,
                TimeoutError,
                KeyError,
                ValueError,
            ) as exc:
                self._matcher = None
                self._runtime_version = None
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
        runtime_version = self._get_runtime_version()
        return self._query_initialized(
            query,
            threshold,
            started_at,
            runtime_version,
        )

    def _query_initialized(
        self,
        query: str,
        threshold: float | None,
        started_at: float,
        runtime_version: _FAQRuntimeVersion | None,
    ) -> FAQResult:
        query = query.strip()
        if not query:
            raise ValueError("User query cannot be empty.")
        log_event(
            logger,
            logging.DEBUG,
            "faq.query.start",
            query_length=len(query),
        )

        if runtime_version is None:
            log_event(
                logger,
                logging.WARNING,
                "faq.query.route",
                route="rag",
                reason="matcher_unavailable",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return FAQResult(route="rag", query=query, rag_query=query)

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

        match = runtime_version.matcher.match(tokens)
        settings = get_faq_settings()
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

        answer = runtime_version.answers.get(match.document.question_id)
        if answer is None:
            log_event(
                logger,
                logging.WARNING,
                "faq.query.route",
                route="rag",
                reason="answer_missing_from_runtime_version",
                question_id=match.document.question_id,
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

        log_event(
            logger,
            logging.INFO,
            "faq.query.route",
            route="faq",
            question_id=match.document.question_id,
            answer_source="memory",
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
        )

    def _apply_excel_refresh_sync(
        self,
        records: list[object],
        source_path: str,
        source_hash: str,
    ) -> object:
        """提交准备好的 Excel 版本，然后原子发布它。"""
        from .sync import (
            FAQQnaSynchronizer,
            FAQSyncResult,
        )

        typed_records = sorted(
            records,
            key=lambda record: str(getattr(record, "question", "")),
        )
        if not any(
            getattr(record, "enabled", 1) == 1
            and getattr(record, "need_human", 0) == 0
            for record in typed_records
        ):
            raise ValueError("FAQ workbook contains no usable FAQ records.")
        database_result = FAQQnaSynchronizer().sync(  # type: ignore[arg-type]
            typed_records
        )
        active_records = [
            (question_id, record)
            for question_id, record in zip(
                database_result.question_ids,
                typed_records,
            )
            if (
                getattr(record, "enabled", 1) == 1
                and getattr(record, "need_human", 0) == 0
            )
        ]

        snapshot = self._synchronizer.build_snapshot(
            [
                (question_id, record.question)  # type: ignore[attr-defined]
                for question_id, record in active_records
            ]
        )
        answers = {
            question_id: record.answer  # type: ignore[attr-defined]
            for question_id, record in active_records
        }
        new_runtime_version = self._build_runtime_version(
            snapshot,
            answers=answers,
            source_hash=source_hash,
        )

        current_version = self._get_runtime_version()
        should_activate = (
            database_result.changed
            or not self._initialized
            or current_version is None
            or current_version.source_hash != source_hash
        )
        if should_activate:
            self._activate_runtime_version(new_runtime_version)

        return FAQSyncResult(
            source_path=source_path,
            source_row_count=database_result.source_row_count,
            upserted_row_count=database_result.source_row_count,
            deleted_row_count=database_result.deleted_row_count,
            bm25_document_count=len(snapshot.documents),
            bm25_reloaded=should_activate,
            changed=database_result.changed,
            source_hash=source_hash,
        )

    async def submit_excel_refresh(
        self,
        workbook_path: str | Path | None = None,
        *,
        sheet_name: str | None = None,
    ) -> FAQUpdateStatus:
        """准备并提交去重后的后台 FAQ 刷新任务。"""
        from .sync import (
            FAQExcelReader,
            _records_hash,
        )

        settings = get_faq_settings()
        source_path = Path(
            workbook_path or settings.faq_source_xlsx_path
        )
        if not source_path.is_absolute():
            source_path = PROJECT_ROOT / source_path
        records = await asyncio.to_thread(
            lambda: FAQExcelReader().read(source_path, sheet_name=sheet_name)
        )
        source_hash = _records_hash(records)

        with self._update_state_lock:
            active_task = self._active_update_task
            if active_task is not None and not active_task.done():
                if self._active_update_hash == source_hash:
                    return FAQUpdateStatus(
                        job_id=self._find_active_job_id(),
                        source_hash=source_hash,
                        status="already_running",
                    )
                raise FAQUpdateInProgress(
                    "A different FAQ workbook update is already running."
                )
            if self._last_completed_hash == source_hash:
                return FAQUpdateStatus(
                    job_id="",
                    source_hash=source_hash,
                    status="already_applied",
                )

            job_id = uuid.uuid4().hex
            self._active_update_hash = source_hash
            self._update_statuses[job_id] = "running"
            self._active_update_task = asyncio.create_task(
                self._run_excel_refresh(
                    job_id,
                    records,
                    str(source_path),
                    source_hash,
                )
            )
            self._active_update_task.add_done_callback(
                lambda task: self._finish_update(job_id, task)
            )
            log_event(
                logger,
                logging.INFO,
                "faq.update.background.submitted",
                job_id=job_id,
                source_hash=source_hash,
                source_path=source_path,
            )

        return FAQUpdateStatus(
            job_id=job_id,
            source_hash=source_hash,
            status="submitted",
        )

    async def _run_excel_refresh(
        self,
        job_id: str,
        records: list[object],
        source_path: str,
        source_hash: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._apply_excel_refresh_sync,
                records,
                source_path,
                source_hash,
            )
            with self._update_state_lock:
                self._update_statuses[job_id] = "completed"
                self._last_completed_hash = source_hash
            log_event(
                logger,
                logging.INFO,
                "faq.update.background.completed",
                job_id=job_id,
                source_hash=source_hash,
            )
        except Exception as exc:
            with self._update_state_lock:
                self._update_statuses[job_id] = "failed"
            log_event(
                logger,
                logging.ERROR,
                "faq.update.background.failed",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            raise

    def _find_active_job_id(self) -> str:
        for job_id, status in self._update_statuses.items():
            if status == "running":
                return job_id
        return "unknown"

    def _finish_update(
        self,
        job_id: str,
        task: asyncio.Task | None = None,
    ) -> None:
        # 消费该异常，因为此任务有意与提交后台刷新的请求分离。
        if task is not None and not task.cancelled():
            try:
                task.exception()
            except asyncio.CancelledError:
                pass
        with self._update_state_lock:
            if self._active_update_task is not None:
                self._active_update_task = None
            self._active_update_hash = None
            self._update_statuses[job_id] = self._update_statuses.get(
                job_id,
                "completed",
            )

    def get_update_status(self, job_id: str) -> str | None:
        with self._update_state_lock:
            return self._update_statuses.get(job_id)


_service: FAQService | None = None
_service_lock = threading.Lock()


def get_faq_service() -> FAQService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = FAQService()
    return _service


# 公开运行时别名让应用代码与单例实现解耦，同时避免额外的包装模块。
FAQRuntime = FAQService
FAQRuntimeResult = FAQResult


def get_runtime() -> FAQService:
    return get_faq_service()


def initialize_faq_runtime(force: bool = False) -> FAQService:
    return get_runtime().initialize(force=force)


def execute_faq_query(query: str) -> FAQResult:
    return get_runtime().query(query)


async def submit_faq_excel_refresh(
    workbook_path: str | Path | None = None,
    *,
    sheet_name: str | None = None,
) -> FAQUpdateStatus:
    """提交去重后的非阻塞 FAQ 工作簿刷新任务。"""
    return await get_runtime().submit_excel_refresh(
        workbook_path,
        sheet_name=sheet_name,
    )


def get_faq_update_status(job_id: str) -> str | None:
    return get_runtime().get_update_status(job_id)
