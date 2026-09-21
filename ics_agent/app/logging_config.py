# 模块职责：配置全局日志格式，并提供统一的事件日志辅助函数。

from __future__ import annotations

import logging
from typing import Any


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str | int | None = None) -> None:
    """Configure the process-wide logger once and update its root level."""
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
