"""Complete, consistent Excel exports using a dedicated read-only connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
import uuid

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import QuerySettings
from .repository import (ArchiveFilters, QueryRepository, _flatten, customer_conditions, customer_round_scoped,
                         customer_round_seq_cte, customer_scope_round_conditions, load_facts, round_conditions)


class ExportTooLarge(ValueError):
    pass


class ExportNotFound(LookupError):
    pass


class ExportCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportFile:
    path: str
    filename: str

    def cleanup(self):
        Path(self.path).unlink(missing_ok=True)


class ExportRepository(QueryRepository):
    """Share SQL semantics and role checks, never the interactive query pool."""

    def __init__(self, settings: QuerySettings):
        self.settings = settings
        kwargs = {**settings.connection_kwargs, "application_name": "ics-quality-export",
                  "options": "-c statement_timeout=15000 -c lock_timeout=500 -c default_transaction_read_only=on",
                  "row_factory": dict_row}
        self.pool = ConnectionPool(kwargs=kwargs, min_size=0, max_size=1, timeout=1, open=False)

    @staticmethod
    def _selection(mode, filters, customer_id=None, round_id=None, facts=None):
        if mode == "unlinked-ratings":
            return "SELECT rating_key FROM ops.rating_index WHERE status='unlinked'", []
        if mode == "rounds":
            if round_id is not None:
                return "SELECT r.round_id FROM ops.round_index r WHERE r.round_id=%s", [round_id]
            where, values = round_conditions(filters, facts)
            return f"SELECT r.round_id FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id WHERE {where}", values
        if mode == "customers":
            if customer_round_scoped(filters):
                # 按客户 + 轮次级筛选：只导出命中的轮次（与列表、详情一致），不展开用户完整历史。
                if customer_id is not None:
                    where, values = customer_scope_round_conditions(filters, facts)
                    return (f"SELECT r.round_id FROM ops.round_index r WHERE r.customer_id=%s AND {where}",
                            [customer_id, *values])
                where, values = round_conditions(filters, facts)
                return f"SELECT r.round_id FROM ops.round_index r LEFT JOIN ops.customer_index c ON c.customer_id=r.customer_id WHERE {where}", values
            if customer_id is not None:
                return "SELECT c.customer_id FROM ops.customer_index c WHERE c.customer_id=%s", [customer_id]
            where, values = customer_conditions(filters, facts)
            return f"SELECT c.customer_id FROM ops.customer_index c WHERE {where}", values
        raise ValueError("Unsupported archive export mode")

    def _check_limits(self, entity_count, message_count, byte_count):
        exceeded = []
        if entity_count > self.settings.export_max_entities:
            exceeded.append(f"轮次/用户 {entity_count:,} 个（ARCHIVE_EXPORT_MAX_ENTITIES 上限 {self.settings.export_max_entities:,}）")
        if message_count > self.settings.export_max_messages:
            exceeded.append(f"消息 {message_count:,} 条（ARCHIVE_EXPORT_MAX_MESSAGES 上限 {self.settings.export_max_messages:,}）")
        if byte_count > self.settings.export_max_bytes:
            exceeded.append(f"数据估算 {byte_count / 1024**2:.1f} MiB（ARCHIVE_EXPORT_MAX_BYTES 上限 {self.settings.export_max_bytes / 1024**2:.1f} MiB）")
        if exceeded:
            raise ExportTooLarge("Excel 导出超过限制：" + "；".join(exceeded) +
                                 "。请缩小筛选范围，或使用离线 HTML 留档；本次未生成部分文件。")

    def create_export(self, mode: str, filters: ArchiveFilters, *, customer_id=None, round_id=None,
                      cancelled: threading.Event | None = None) -> ExportFile:
        # Import only when exporting; optional workbook code cannot break startup.
        from .excel import ExcelLimitExceeded, build_workbook

        deadline = time.monotonic() + self.settings.export_timeout_seconds
        def check():
            if cancelled is not None and cancelled.is_set():
                raise ExportCancelled()
            if time.monotonic() >= deadline:
                raise ExportTooLarge("导出用时过长，请缩小筛选范围后重试。")

        # A single repeatable-read snapshot keeps selection, counts, and messages
        # consistent while the archive worker updates its disposable projections.
        with self.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            facts = load_facts(connection, filters, customer_id=customer_id) if mode != "unlinked-ratings" else None
            selection, values = self._selection(mode, filters, customer_id, round_id, facts)
            if mode == "unlinked-ratings":
                round_where = customer_where = message_where = None
                rating_budget = connection.execute("""
                    SELECT count(*) AS n,COALESCE(sum(octet_length(to_jsonb(r)::text)),0) AS bytes
                    FROM ops.rating_index r WHERE r.status='unlinked'
                """).fetchone()
                entity_count, byte_count = rating_budget["n"], rating_budget["bytes"]
                message_count = 0
            else:
                locked = ("(m.sent_at IS NULL OR m.sent_at < '" + filters.as_of.isoformat() + "'::timestamptz)"
                          if filters.as_of is not None else "TRUE")
                if mode == "rounds" or customer_round_scoped(filters):
                    round_where = f"r.round_id IN ({selection})"
                    message_where = f"m.round_id IN ({selection}) AND {locked}"
                    customer_where = f"c.customer_id IN (SELECT r.customer_id FROM ops.round_index r WHERE {round_where})"
                else:
                    customer_where = f"c.customer_id IN ({selection})"
                    round_where = f"r.customer_id IN ({selection})"
                    message_where = f"m.customer_id IN ({selection}) AND {locked}"
                # Counts first (index scans only): a range over the row limits is refused
                # before any row is serialized to estimate its size.
                counts = []
                for table, alias, where in (("round_index", "r", round_where), ("customer_index", "c", customer_where),
                                             ("message_index", "m", message_where)):
                    check()
                    counts.append(connection.execute(
                        f"SELECT count(*) AS n FROM ops.{table} {alias} WHERE {where}", values).fetchone()["n"])
                entity_count, message_count = max(counts[0], counts[1]), counts[2]
                if ((round_id is not None and not counts[0]) or
                        (customer_id is not None and not counts[1])):
                    raise ExportNotFound()
                self._check_limits(entity_count, message_count, 0)
                byte_count = 0
                for table, alias, where in (("round_index", "r", round_where), ("customer_index", "c", customer_where),
                                             ("message_index", "m", message_where)):
                    check()
                    byte_count += connection.execute(
                        f"SELECT COALESCE(sum(octet_length(to_jsonb({alias})::text)),0) AS bytes FROM ops.{table} {alias}"
                        f" WHERE {where}", values).fetchone()["bytes"]
                # A review is repeated on its round's message rows; include it
                # in the byte budget before streaming the single worksheet.
                review_budget = connection.execute(f"""
                    SELECT COALESCE(sum(octet_length(COALESCE(review.remark,'')) +
                        octet_length(COALESCE(review.ai_status,'')) +
                        octet_length(COALESCE(review.human_status,''))),0) AS bytes
                    FROM ops.message_index m LEFT JOIN quality.round_review_current review
                        ON review.round_id=m.round_id WHERE {message_where}
                """, values).fetchone()
                byte_count += review_budget["bytes"]
            self._check_limits(entity_count, message_count, byte_count)

            def rows(statement, parameters=values):
                check()
                with connection.cursor(name="quality_export_" + uuid.uuid4().hex) as cursor:
                    cursor.execute(statement, parameters)
                    while True:
                        check()
                        batch = cursor.fetchmany(200)
                        if not batch:
                            break
                        batch = [_flatten(row) for row in batch]
                        for row in batch:
                            check()
                            yield row

            dataset = {}
            if mode == "unlinked-ratings":
                dataset["ratings"] = rows("""
                    SELECT r.*,s.event_id,NULLIF(r.data ->> 'source_time', '')::timestamptz AS rating_at FROM ops.rating_index r
                    JOIN archive.rating_snapshots s ON s.id=r.snapshot_id WHERE r.status='unlinked'
                    ORDER BY rating_at DESC NULLS LAST,r.source_key,r.account_id,r.conversation_id,r.rating_key
                """, [])
            else:
                # 用户累计咨询次序按客户全部轮次计算，不受本次选中的轮次范围影响。
                dataset["messages"] = rows(f"""
                    WITH {customer_round_seq_cte(f"SELECT m.customer_id FROM ops.message_index m WHERE {message_where}")}
                    SELECT m.*,c.display_name AS customer_name,c.contact_id,r.round_no,r.reception_type,seq.customer_round_seq,
                        review.ai_status AS ai_review_status,review.human_status AS human_review_status,
                        review.remark AS review_remark
                    FROM ops.message_index m
                    LEFT JOIN ops.customer_index c ON c.customer_id=m.customer_id
                    LEFT JOIN ops.round_index r ON r.round_id=m.round_id
                    LEFT JOIN customer_round_seq seq ON seq.round_id=m.round_id
                    LEFT JOIN quality.round_review_current review ON review.round_id=m.round_id
                    WHERE {message_where} ORDER BY m.customer_id,m.conversation_id,m.sent_at NULLS LAST,m.message_id,m.source_key,m.account_id
                """, [*values, *values])
            check()
            exported_at = datetime.now(timezone.utc)
            descriptor, path = tempfile.mkstemp(prefix="ics-quality-export-", suffix=".xlsx")
            import os
            os.close(descriptor)
            result = ExportFile(path, "ics-archive-" + mode + "-" + exported_at.strftime("%Y%m%dT%H%M%SZ") + ".xlsx")
            try:
                with open(path, "wb") as destination:
                    build_workbook(destination, dataset, filters={}, exported_at=exported_at, mode=mode)
                check()
                connection.commit()
                return result
            except ExcelLimitExceeded as error:
                result.cleanup()
                raise ExportTooLarge(str(error)) from None
            except BaseException:
                result.cleanup()
                raise
