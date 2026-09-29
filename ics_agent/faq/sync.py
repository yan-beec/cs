# 模块职责：读取 FAQ Excel 问答对，事务化同步 PostgreSQL/Redis，并刷新运行时。

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from ics_agent.app.core.logging_config import log_event
from ics_agent.faq.config import get_faq_settings

from .repositories import (
    FAQAnswerCache,
    PostgreSQLConnectionPool,
    RedisFAQSnapshotRepository,
)
from .service import FAQService, FAQSnapshotSynchronizer, get_faq_service

PROJECT_ROOT = Path(__file__).resolve().parents[2]


logger = logging.getLogger("ics_agent.faq.sync")

EXPECTED_COLUMNS = (
    "question",
    "answer",
    "need_human",
    "enabled",
)


@dataclass(frozen=True)
class FAQExcelRow:
    """从源工作簿读取并校验通过的一条 FAQ 记录。"""

    question: str
    answer: str
    need_human: int
    enabled: int

    def to_postgres_params(self) -> tuple[Any, ...]:
        return (
            self.question,
            self.answer,
            self.need_human,
            self.enabled,
        )


@dataclass(frozen=True)
class FAQSyncResult:
    """一次从 Excel 同步到 PostgreSQL 的摘要。"""

    source_path: str
    source_row_count: int
    upserted_row_count: int
    deleted_row_count: int
    redis_snapshot_count: int | None = None
    redis_snapshot_written: bool = False
    redis_cache_keys_removed: int | None = None
    bm25_document_count: int | None = None
    bm25_reloaded: bool = False
    changed: bool = True
    source_hash: str | None = None


@dataclass(frozen=True)
class FAQDatabaseSyncResult:
    """一次 FAQ 表原子全量替换的结果。"""

    source_row_count: int
    deleted_row_count: int
    question_ids: tuple[int, ...]
    changed: bool
    source_hash: str


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("[", "{")):
            try:
                return _canonical_value(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return stripped
    return str(value)


def _canonical_values(values: Iterable[Any]) -> list[Any]:
    return [_canonical_value(value) for value in values]


def _records_hash(records: Iterable[FAQExcelRow]) -> str:
    ordered_records = sorted(records, key=lambda record: record.question)
    payload = [
        _canonical_values(record.to_postgres_params())
        for record in ordered_records
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _rows_hash(rows: Iterable[Iterable[Any]]) -> str:
    payload = sorted(
        (_canonical_values(row) for row in rows),
        key=lambda row: tuple(str(value) for value in row),
    )
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _required_text(
    value: Any,
    *,
    field_name: str,
    row_number: int,
) -> str:
    if value is None:
        raise ValueError(
            f"Excel row {row_number} has an empty required field: {field_name}."
        )
    text = str(value).strip()
    if not text:
        raise ValueError(
            f"Excel row {row_number} has an empty required field: {field_name}."
        )
    return text


def _parse_integer(
    value: Any,
    *,
    field_name: str,
    row_number: int,
    minimum: int | None = None,
) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(
            f"Excel row {row_number} has an invalid integer field: {field_name}."
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Excel row {row_number} has an invalid integer field: {field_name}."
        ) from exc
    if isinstance(value, float) and value != parsed:
        raise ValueError(
            f"Excel row {row_number} has a non-integer value: {field_name}."
        )
    if minimum is not None and parsed < minimum:
        raise ValueError(
            f"Excel row {row_number} has {field_name} below {minimum}."
        )
    return parsed


def _parse_flag(value: Any, *, field_name: str, row_number: int) -> int:
    parsed = _parse_integer(value, field_name=field_name, row_number=row_number)
    if parsed not in {0, 1}:
        raise ValueError(
            f"Excel row {row_number} field {field_name} must be 0 or 1."
        )
    return parsed


class FAQExcelReader:
    """加载并校验规范 FAQ 工作簿。"""

    def read(
        self,
        workbook_path: str | Path,
        sheet_name: str | None = None,
    ) -> list[FAQExcelRow]:
        path = _resolve_project_path(workbook_path)
        if not path.is_file():
            raise FileNotFoundError(f"FAQ workbook does not exist: {path}")
        if path.suffix.lower() != ".xlsx":
            raise ValueError(f"FAQ workbook must be an .xlsx file: {path}")

        log_event(
            logger,
            logging.INFO,
            "faq.excel.read.start",
            path=path,
            sheet_name=sheet_name,
        )
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            selected_sheet = sheet_name or workbook.sheetnames[0]
            if selected_sheet not in workbook.sheetnames:
                raise ValueError(
                    f"FAQ workbook sheet does not exist: {selected_sheet}"
                )
            worksheet = workbook[selected_sheet]
            rows = worksheet.iter_rows(values_only=True)
            raw_header = next(rows, None)
            if raw_header is None:
                raise ValueError(f"FAQ workbook is empty: {path}")

            header = tuple(
                str(value).strip() if value is not None else ""
                for value in raw_header
            )
            self._validate_header(header, path)
            positions = {name: header.index(name) for name in EXPECTED_COLUMNS}

            records: list[FAQExcelRow] = []
            seen_questions: set[str] = set()
            for row_number, raw_row in enumerate(rows, start=2):
                if not any(value not in (None, "") for value in raw_row):
                    continue
                record = self._parse_row(raw_row, positions, row_number)
                if record.question in seen_questions:
                    raise ValueError(
                        f"Excel contains duplicate FAQ question at row "
                        f"{row_number}: {record.question}."
                    )
                seen_questions.add(record.question)
                records.append(record)

            if not records:
                raise ValueError(f"FAQ workbook contains no data rows: {path}")
            log_event(
                logger,
                logging.INFO,
                "faq.excel.read.success",
                path=path,
                sheet_name=selected_sheet,
                row_count=len(records),
            )
            records.sort(key=lambda record: record.question)
            return records
        finally:
            workbook.close()

    @staticmethod
    def _validate_header(header: tuple[str, ...], path: Path) -> None:
        missing = [name for name in EXPECTED_COLUMNS if name not in header]
        duplicates = {
            name for name in header if name and header.count(name) > 1
        }
        if missing or duplicates:
            details = []
            if missing:
                details.append(f"missing={missing}")
            if duplicates:
                details.append(f"duplicates={sorted(duplicates)}")
            raise ValueError(
                f"FAQ workbook header is invalid for {path}: "
                + ", ".join(details)
            )

    @staticmethod
    def _parse_row(
        raw_row: tuple[Any, ...],
        positions: dict[str, int],
        row_number: int,
    ) -> FAQExcelRow:
        def value(field_name: str) -> Any:
            position = positions[field_name]
            return raw_row[position] if position < len(raw_row) else None

        return FAQExcelRow(
            question=_required_text(
                value("question"),
                field_name="question",
                row_number=row_number,
            ),
            answer=_required_text(
                value("answer"),
                field_name="answer",
                row_number=row_number,
            ),
            need_human=_parse_flag(
                value("need_human"),
                field_name="need_human",
                row_number=row_number,
            ),
            enabled=_parse_flag(
                value("enabled"),
                field_name="enabled",
                row_number=row_number,
            ),
        )


class FAQQnaSynchronizer:
    """将校验通过的 FAQ 记录同步到配置的 PostgreSQL 表。"""

    def __init__(
        self,
        connections: PostgreSQLConnectionPool | None = None,
    ) -> None:
        self._connections = connections or PostgreSQLConnectionPool()

    def sync(
        self,
        records: Iterable[FAQExcelRow],
    ) -> FAQDatabaseSyncResult:
        rows = sorted(records, key=lambda record: record.question)
        if not rows:
            raise ValueError("Cannot synchronize an empty FAQ record set.")

        settings = get_faq_settings()
        table_name = settings.faq_table_name
        columns = ", ".join(EXPECTED_COLUMNS)
        placeholders = ", ".join(["%s"] * len(EXPECTED_COLUMNS))
        source_hash = _records_hash(rows)
        insert_sql = f"""
            INSERT INTO {table_name} ({columns})
            VALUES ({placeholders})
            RETURNING id
        """.strip()

        connection = self._connections.acquire()
        deleted_count = 0
        changed = True
        question_ids: list[int] = []
        try:
            cursor = connection.cursor()
            try:
                with connection.transaction():
                    # 跨进程串行执行全量替换。该锁仅限于事务范围，
                    # 不会修改表结构。
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"ics_agent.faq.replace:{table_name}",),
                    )
                    cursor.execute(
                        f"""
                        SELECT id, {columns}
                        FROM {table_name}
                        ORDER BY question, id
                        """.strip()
                    )
                    current_rows = cursor.fetchall()
                    if _rows_hash(row[1:] for row in current_rows) == source_hash:
                        changed = False
                        question_ids = [int(row[0]) for row in current_rows]
                    else:
                        cursor.execute(f"DELETE FROM {table_name}")
                        deleted_count = max(cursor.rowcount, 0)
                        for record in rows:
                            cursor.execute(insert_sql, record.to_postgres_params())
                            inserted_row = cursor.fetchone()
                            if inserted_row is None:
                                raise RuntimeError(
                                    "FAQ insert did not return a generated id."
                                )
                            question_ids.append(int(inserted_row[0]))
            finally:
                cursor.close()
        finally:
            connection.close()

        log_event(
            logger,
            logging.INFO,
            "faq.postgres.sync.success",
            table=table_name,
            source_row_count=len(rows),
            deleted_row_count=deleted_count,
            replace_mode="full",
            changed=changed,
            source_hash=source_hash,
        )
        return FAQDatabaseSyncResult(
            source_row_count=len(rows),
            deleted_row_count=deleted_count,
            question_ids=tuple(question_ids),
            changed=changed,
            source_hash=source_hash,
        )


def sync_qna_from_excel(
    workbook_path: str | Path | None = None,
    *,
    sheet_name: str | None = None,
    sync_redis: bool = True,
    clear_answer_cache: bool | None = None,
    refresh_bm25: bool = False,
    faq_service: FAQService | None = None,
) -> FAQSyncResult:
    """同步 PostgreSQL、Redis FAQ 状态，并按需刷新实时 BM25 匹配器。"""
    settings = get_faq_settings()
    source_path = _resolve_project_path(
        workbook_path or settings.faq_source_xlsx_path
    )
    records = FAQExcelReader().read(source_path, sheet_name=sheet_name)
    database_result = FAQQnaSynchronizer().sync(records)
    upserted_count = database_result.source_row_count
    deleted_count = database_result.deleted_row_count

    redis_count: int | None = None
    redis_snapshot_written = False
    redis_cache_keys_removed: int | None = None
    bm25_document_count: int | None = None
    bm25_reloaded = False
    snapshot = None
    if sync_redis:
        snapshot_repository = RedisFAQSnapshotRepository()
        snapshot_synchronizer = FAQSnapshotSynchronizer(
            snapshot_repository=snapshot_repository,
        )
        try:
            snapshot = snapshot_synchronizer.sync()
            redis_count = len(snapshot.documents)
            redis_snapshot_written = (
                snapshot_synchronizer.last_redis_save_succeeded
            )
            log_event(
                logger,
                logging.INFO,
                "faq.redis.snapshot.refresh.success",
                count=redis_count,
                written=redis_snapshot_written,
            )
        except Exception as exc:
            # PostgreSQL 是事实来源，Redis 待恢复可用后由常规 FAQ 初始化流程刷新。
            log_event(
                logger,
                logging.WARNING,
                "faq.redis.snapshot.refresh.failed",
                error_type=type(exc).__name__,
            )

        should_clear_cache = (
            settings.faq_clear_answer_cache_on_sync
            if clear_answer_cache is None
            else clear_answer_cache
        )
        if should_clear_cache and redis_snapshot_written:
            try:
                redis_cache_keys_removed = FAQAnswerCache(
                    snapshot_repository.client,
                    circuit=snapshot_repository.circuit,
                ).clear()
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "faq.redis.answer_cache.clear.failed",
                    error_type=type(exc).__name__,
                )
        elif should_clear_cache and redis_count is not None:
            log_event(
                logger,
                logging.WARNING,
                "faq.redis.answer_cache.clear.skipped",
                reason="snapshot_not_written",
            )
        elif not should_clear_cache:
            log_event(
                logger,
                logging.INFO,
                "faq.redis.answer_cache.clear.skipped",
                reason="disabled_by_configuration",
            )

    if refresh_bm25:
        if snapshot is None:
            raise RuntimeError(
                "refresh_bm25 requires a successful FAQ snapshot build."
            )
        runtime_service = faq_service or get_faq_service()
        runtime_service.reload_snapshot(snapshot)
        bm25_document_count = len(snapshot.documents)
        bm25_reloaded = True

    return FAQSyncResult(
        source_path=str(source_path),
        source_row_count=len(records),
        upserted_row_count=upserted_count,
        deleted_row_count=deleted_count,
        redis_snapshot_count=redis_count,
        redis_snapshot_written=redis_snapshot_written,
        redis_cache_keys_removed=redis_cache_keys_removed,
        bm25_document_count=bm25_document_count,
        bm25_reloaded=bm25_reloaded,
        changed=database_result.changed,
        source_hash=database_result.source_hash,
    )


async def async_sync_qna_from_excel(
    workbook_path: str | Path | None = None,
    *,
    sheet_name: str | None = None,
    sync_redis: bool = True,
    clear_answer_cache: bool | None = None,
    refresh_bm25: bool = False,
    faq_service: FAQService | None = None,
) -> FAQSyncResult:
    """在线程中执行阻塞式 Excel/数据库刷新，避免阻塞 asyncio 事件循环。"""
    return await asyncio.to_thread(
        sync_qna_from_excel,
        workbook_path,
        sheet_name=sheet_name,
        sync_redis=sync_redis,
        clear_answer_cache=clear_answer_cache,
        refresh_bm25=refresh_bm25,
        faq_service=faq_service,
    )


def refresh_faq_runtime_from_excel(
    workbook_path: str | Path | None = None,
    *,
    sheet_name: str | None = None,
    clear_answer_cache: bool | None = None,
    faq_service: FAQService | None = None,
) -> FAQSyncResult:
    """为上传的工作簿提供后端可直接调用的一站式 FAQ 刷新。"""
    return sync_qna_from_excel(
        workbook_path,
        sheet_name=sheet_name,
        sync_redis=True,
        clear_answer_cache=clear_answer_cache,
        refresh_bm25=True,
        faq_service=faq_service,
    )
