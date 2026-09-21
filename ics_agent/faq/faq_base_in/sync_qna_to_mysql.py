# 模块职责：读取 FAQ Excel 问答对，事务化同步到 MySQL，并刷新 Redis FAQ 快照。

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[3]

if __package__ in {None, ""} and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ics_agent.app.config import get_settings
from ics_agent.app.logging_config import configure_logging, log_event

try:
    from ..repositories import MySQLConnectionPool
    from ..repositories import FAQAnswerCache, RedisFAQSnapshotRepository
    from ..service import FAQService, FAQSnapshotSynchronizer, get_faq_service
except ImportError:
    from ics_agent.faq.repositories import MySQLConnectionPool
    from ics_agent.faq.repositories import (
        FAQAnswerCache,
        RedisFAQSnapshotRepository,
    )
    from ics_agent.faq.service import (
        FAQService,
        FAQSnapshotSynchronizer,
        get_faq_service,
    )


logger = logging.getLogger("ics_agent.faq.faq_base_in.sync_qna_to_mysql")

EXPECTED_COLUMNS = (
    "id",
    "question",
    "answer",
    "category",
    "tags",
    "similar_questions",
    "need_human",
    "enabled",
    "priority",
    "answer_type",
    "updated_at",
)


@dataclass(frozen=True)
class FAQExcelRow:
    """One validated FAQ record read from the source workbook."""

    question_id: int
    question: str
    answer: str
    category: str | None
    tags: str | None
    similar_questions: str | None
    need_human: int
    enabled: int
    priority: int
    answer_type: str
    updated_at: datetime

    def to_mysql_params(self) -> tuple[Any, ...]:
        return (
            self.question_id,
            self.question,
            self.answer,
            self.category,
            self.tags,
            self.similar_questions,
            self.need_human,
            self.enabled,
            self.priority,
            self.answer_type,
            self.updated_at,
        )


@dataclass(frozen=True)
class FAQSyncResult:
    """Summary of one Excel-to-MySQL synchronization."""

    source_path: str
    source_row_count: int
    upserted_row_count: int
    deleted_row_count: int
    redis_snapshot_count: int | None = None
    redis_snapshot_written: bool = False
    redis_cache_keys_removed: int | None = None
    bm25_document_count: int | None = None
    bm25_reloaded: bool = False


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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _parse_json_array(
    value: Any,
    *,
    field_name: str,
    row_number: int,
) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Excel row {row_number} field {field_name} is not valid JSON."
        ) from exc
    if not isinstance(parsed, list):
        raise ValueError(
            f"Excel row {row_number} field {field_name} must be a JSON array."
        )
    return json.dumps(parsed, ensure_ascii=False)


def _parse_timestamp(value: Any, *, row_number: int) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = _required_text(value, field_name="updated_at", row_number=row_number)
    for timestamp_format in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text, timestamp_format)
        except ValueError:
            continue
    raise ValueError(
        f"Excel row {row_number} has an unsupported updated_at value: {text}."
    )


class FAQExcelReader:
    """Load and validate the canonical FAQ workbook."""

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
            seen_ids: set[int] = set()
            for row_number, raw_row in enumerate(rows, start=2):
                if not any(value not in (None, "") for value in raw_row):
                    continue
                record = self._parse_row(raw_row, positions, row_number)
                if record.question_id in seen_ids:
                    raise ValueError(
                        f"Excel contains duplicate FAQ id "
                        f"{record.question_id} at row {row_number}."
                    )
                seen_ids.add(record.question_id)
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
            question_id=_parse_integer(
                value("id"),
                field_name="id",
                row_number=row_number,
                minimum=1,
            ),
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
            category=_optional_text(value("category")),
            tags=_parse_json_array(
                value("tags"),
                field_name="tags",
                row_number=row_number,
            ),
            similar_questions=_parse_json_array(
                value("similar_questions"),
                field_name="similar_questions",
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
            priority=_parse_integer(
                value("priority"),
                field_name="priority",
                row_number=row_number,
            ),
            answer_type=_required_text(
                value("answer_type"),
                field_name="answer_type",
                row_number=row_number,
            ),
            updated_at=_parse_timestamp(value("updated_at"), row_number=row_number),
        )


class FAQQnaSynchronizer:
    """Synchronize validated FAQ records to the configured MySQL table."""

    def __init__(self, connections: MySQLConnectionPool | None = None) -> None:
        self._connections = connections or MySQLConnectionPool()

    def sync(
        self,
        records: Iterable[FAQExcelRow],
        *,
        delete_missing: bool = True,
    ) -> tuple[int, int]:
        rows = list(records)
        if not rows:
            raise ValueError("Cannot synchronize an empty FAQ record set.")

        settings = get_settings()
        table_name = settings.faq_table_name
        columns = ", ".join(EXPECTED_COLUMNS)
        placeholders = ", ".join(["%s"] * len(EXPECTED_COLUMNS))
        update_clause = ", ".join(
            f"{column}=VALUES({column})"
            for column in EXPECTED_COLUMNS
            if column != "id"
        )
        upsert_sql = f"""
            INSERT INTO {table_name} ({columns})
            VALUES ({placeholders})
            ON DUPLICATE KEY UPDATE {update_clause}
        """.strip()

        connection = self._connections.acquire()
        deleted_count = 0
        try:
            cursor = connection.cursor()
            try:
                connection.start_transaction()
                cursor.executemany(
                    upsert_sql,
                    [record.to_mysql_params() for record in rows],
                )
                if delete_missing:
                    ids = [record.question_id for record in rows]
                    id_placeholders = ", ".join(["%s"] * len(ids))
                    cursor.execute(
                        f"DELETE FROM {table_name} "
                        f"WHERE id NOT IN ({id_placeholders})",
                        ids,
                    )
                    deleted_count = max(cursor.rowcount, 0)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
        finally:
            connection.close()

        log_event(
            logger,
            logging.INFO,
            "faq.mysql.sync.success",
            table=table_name,
            source_row_count=len(rows),
            deleted_row_count=deleted_count,
        )
        return len(rows), deleted_count


def sync_qna_from_excel(
    workbook_path: str | Path | None = None,
    *,
    sheet_name: str | None = None,
    delete_missing: bool = True,
    sync_redis: bool = True,
    clear_answer_cache: bool | None = None,
    refresh_bm25: bool = False,
    faq_service: FAQService | None = None,
) -> FAQSyncResult:
    """Synchronize MySQL, Redis FAQ state, and optionally the live BM25 matcher."""
    settings = get_settings()
    source_path = _resolve_project_path(
        workbook_path or settings.faq_source_xlsx_path
    )
    records = FAQExcelReader().read(source_path, sheet_name=sheet_name)
    upserted_count, deleted_count = FAQQnaSynchronizer().sync(
        records,
        delete_missing=delete_missing,
    )

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
            # MySQL is the source-of-truth update. Redis can be refreshed by
            # the normal FAQ initialization path when it becomes available.
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
    )


def refresh_faq_runtime_from_excel(
    workbook_path: str | Path | None = None,
    *,
    sheet_name: str | None = None,
    delete_missing: bool = True,
    clear_answer_cache: bool | None = None,
    faq_service: FAQService | None = None,
) -> FAQSyncResult:
    """Backend-ready one-call FAQ refresh for an uploaded workbook."""
    return sync_qna_from_excel(
        workbook_path,
        sheet_name=sheet_name,
        delete_missing=delete_missing,
        sync_redis=True,
        clear_answer_cache=clear_answer_cache,
        refresh_bm25=True,
        faq_service=faq_service,
    )


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs for direct CLI execution."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize FAQ Q&A rows from an Excel workbook to MySQL "
            "and refresh the Redis FAQ snapshot."
        )
    )
    parser.add_argument(
        "--excel-path",
        help="Override FAQ_SOURCE_XLSX_PATH for this execution.",
    )
    parser.add_argument(
        "--sheet-name",
        help="Workbook sheet name. Defaults to the first sheet.",
    )
    parser.add_argument(
        "--keep-extra",
        action="store_true",
        help="Do not delete MySQL rows whose ids are absent from the workbook.",
    )
    parser.add_argument(
        "--skip-redis-sync",
        action="store_true",
        help="Only update MySQL and leave the Redis snapshot unchanged.",
    )
    parser.add_argument(
        "--keep-answer-cache",
        action="store_true",
        help="Refresh the Redis snapshot but keep the existing FAQ answer cache.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    configure_logging()
    log_event(
        logger,
        logging.INFO,
        "faq.cli.qna_sync.start",
        excel_path=args.excel_path or get_settings().faq_source_xlsx_path,
        sheet_name=args.sheet_name,
        delete_missing=not args.keep_extra,
        sync_redis=not args.skip_redis_sync,
        clear_answer_cache=(
            False if args.keep_answer_cache else
            get_settings().faq_clear_answer_cache_on_sync
        ),
    )
    try:
        result = sync_qna_from_excel(
            args.excel_path,
            sheet_name=args.sheet_name,
            delete_missing=not args.keep_extra,
            sync_redis=not args.skip_redis_sync,
            clear_answer_cache=(
                False if args.keep_answer_cache else None
            ),
        )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "faq.cli.qna_sync.failed",
            error_type=type(exc).__name__,
        )
        raise

    log_event(
        logger,
        logging.INFO,
        "faq.cli.qna_sync.success",
        source_row_count=result.source_row_count,
        upserted_row_count=result.upserted_row_count,
        deleted_row_count=result.deleted_row_count,
        redis_snapshot_count=result.redis_snapshot_count,
        redis_snapshot_written=result.redis_snapshot_written,
        redis_cache_keys_removed=result.redis_cache_keys_removed,
        bm25_document_count=result.bm25_document_count,
        bm25_reloaded=result.bm25_reloaded,
    )
    print(
        "FAQ sync completed: "
        f"source_rows={result.source_row_count}, "
        f"upserted={result.upserted_row_count}, "
        f"deleted={result.deleted_row_count}, "
        f"redis_snapshot={result.redis_snapshot_count}, "
        f"redis_snapshot_written={result.redis_snapshot_written}, "
        f"redis_cache_keys_removed={result.redis_cache_keys_removed}, "
        f"bm25_reloaded={result.bm25_reloaded}"
    )


if __name__ == "__main__":
    main()
