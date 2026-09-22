# 模块职责：配置全局日志格式，并提供统一的事件日志辅助函数。

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class LogBufferHandler(logging.Handler):
    """Keep a bounded, process-local log stream for the administrator console."""

    def __init__(self, max_records: int) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=max_records)
        self._next_id = 1
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "id": 0,
                "timestamp": time.strftime(
                    DEFAULT_DATE_FORMAT,
                    time.localtime(record.created),
                ),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            with self._lock:
                payload["id"] = self._next_id
                self._next_id += 1
                self._records.append(payload)
        except Exception:
            self.handleError(record)

    def snapshot(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        with self._lock:
            records = [
                record
                for record in self._records
                if int(record["id"]) > after_id
            ]
            records = records[-max(1, min(limit, 1000)) :]
            next_id = self._next_id - 1
        return {"items": records, "next_id": next_id}


_log_buffer_handler: LogBufferHandler | None = None


def get_log_snapshot(
    *,
    after_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Return buffered application logs for the administrator console."""
    if _log_buffer_handler is None:
        return {"items": [], "next_id": after_id}
    return _log_buffer_handler.snapshot(after_id=after_id, limit=limit)


def configure_logging(level: str | int | None = None) -> None:
    """Configure the process-wide logger once and update its root level."""
    global _log_buffer_handler
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                DEFAULT_LOG_FORMAT,
                datefmt=DEFAULT_DATE_FORMAT,
            )
        )
        root_logger.addHandler(handler)

    if _log_buffer_handler is None:
        from .config import get_settings

        _log_buffer_handler = LogBufferHandler(
            get_settings().admin_log_buffer_size
        )
        _log_buffer_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(_log_buffer_handler)

    if level is None:
        from .config import get_settings

        level = get_settings().log_level

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(level)
    logging.getLogger("jieba").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    level: int,
    event_name: str,
    **fields: Any,
) -> None:
    """Write a searchable event marker with compact key-value fields."""
    parts = [f"event={event_name}"]
    for key, value in fields.items():
        if value is None:
            continue
        normalized = str(value).replace("\r", " ").replace("\n", " ")
        parts.append(f"{key}={normalized.replace(' ', '_')}")
    logger.log(level, " ".join(parts))
