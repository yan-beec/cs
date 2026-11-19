"""异步重连装饰器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import ParamSpec, TypeVar

from ics_agent.app.core.logging_config import log_event


P = ParamSpec("P")
T = TypeVar("T")


def async_reconnect(
    *,
    delays: Sequence[float],
    logger: logging.Logger | None = None,
    operation: str = "async_operation",
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """按给定间隔重试异步调用，最终失败时重新抛出最后一次异常。"""

    retry_delays = tuple(delays)

    def decorator(
        function: Callable[P, Awaitable[T]],
    ) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            attempt = 1
            while True:
                try:
                    return await function(*args, **kwargs)
                except exceptions as exc:
                    if attempt > len(retry_delays):
                        if logger is not None:
                            log_event(
                                logger,
                                logging.ERROR,
                                "retry.reconnect.failed",
                                operation=operation,
                                attempts=attempt,
                                error_type=type(exc).__name__,
                            )
                        raise

                    delay = retry_delays[attempt - 1]
                    if logger is not None:
                        log_event(
                            logger,
                            logging.WARNING,
                            "retry.reconnect.attempt_failed",
                            operation=operation,
                            attempt=attempt,
                            next_attempt=attempt + 1,
                            delay_seconds=delay,
                            error_type=type(exc).__name__,
                        )
                    await asyncio.sleep(delay)
                    attempt += 1

        return wrapper

    return decorator
