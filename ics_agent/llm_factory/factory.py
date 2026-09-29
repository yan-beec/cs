# 模块职责：基于 LangChain init_chat_model 创建可切换的云端聊天模型。

from __future__ import annotations

import logging
import asyncio
import time
import weakref
from functools import lru_cache
from typing import Any, Sequence

from ics_agent.app.core.config import Settings, get_settings
from ics_agent.app.core.logging_config import log_event


logger = logging.getLogger(__name__)
PromptInput = str | Sequence[Any]
ConcurrencyKey = tuple[str, str, str, int]
_CONCURRENCY_GATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[ConcurrencyKey, asyncio.Semaphore],
] = weakref.WeakKeyDictionary()


class LLMConcurrencyLimitError(TimeoutError):
    """在截止时间前没有可用 LLM 并发槽位时抛出。"""


def _load_init_chat_model() -> Any:
    """延迟加载 LangChain，使应用其余部分仍可正常导入。"""
    try:
        from langchain.chat_models import init_chat_model
    except ImportError as exc:
        raise RuntimeError(
            "LangChain dependencies are missing. Install requirements.txt and "
            "the integration package for the configured LLM provider."
        ) from exc
    return init_chat_model


def _get_concurrency_gate(settings: Settings) -> asyncio.Semaphore:
    """获取当前事件循环中各调用共享的并发闸门。"""
    loop = asyncio.get_running_loop()
    key = (
        settings.llm_model,
        settings.llm_model_provider,
        settings.llm_base_url,
        settings.llm_max_concurrency,
    )
    loop_gates = _CONCURRENCY_GATES.setdefault(loop, {})
    gate = loop_gates.get(key)
    if gate is None:
        gate = asyncio.Semaphore(settings.llm_max_concurrency)
        loop_gates[key] = gate
    return gate


async def _acquire_concurrency_slot(settings: Settings) -> asyncio.Semaphore:
    """获取共享异步槽位，同时不阻塞事件循环。"""
    gate = _get_concurrency_gate(settings)
    started_at = time.perf_counter()
    try:
        await asyncio.wait_for(
            gate.acquire(),
            timeout=settings.llm_concurrency_wait_timeout,
        )
    except asyncio.TimeoutError as exc:
        wait_seconds = time.perf_counter() - started_at
        log_event(
            logger,
            logging.WARNING,
            "llm_factory.concurrency.rejected",
            model=settings.llm_model,
            max_concurrency=settings.llm_max_concurrency,
            wait_seconds=round(wait_seconds, 3),
        )
        raise LLMConcurrencyLimitError(
            "LLM concurrency limit reached; no request slot became available "
            f"within {settings.llm_concurrency_wait_timeout} seconds."
        ) from exc

    wait_seconds = time.perf_counter() - started_at
    log_event(
        logger,
        logging.DEBUG,
        "llm_factory.concurrency.acquired",
        model=settings.llm_model,
        max_concurrency=settings.llm_max_concurrency,
        wait_seconds=round(wait_seconds, 3),
    )
    return gate


def _release_concurrency_slot(
    settings: Settings,
    gate: asyncio.Semaphore,
) -> None:
    """提供商调用完成后释放异步槽位。"""
    gate.release()
    log_event(
        logger,
        logging.DEBUG,
        "llm_factory.concurrency.released",
        model=settings.llm_model,
        max_concurrency=settings.llm_max_concurrency,
    )


@lru_cache(maxsize=8)
def _create_chat_model(
    model: str,
    model_provider: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    max_retries: int,
    api_key: str,
    base_url: str,
) -> Any:
    """为每种不同配置创建并缓存一个模型实例。"""
    init_chat_model = _load_init_chat_model()
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    model_instance = init_chat_model(
        model=model,
        model_provider=model_provider or None,
        **kwargs,
    )
    log_event(
        logger,
        logging.INFO,
        "llm_factory.model.created",
        model=model,
        provider=model_provider or "auto",
        timeout=timeout,
        max_retries=max_retries,
    )
    return model_instance


def _get_chat_model(settings: Settings | None = None) -> Any:
    """返回按环境配置并缓存的 LangChain 聊天模型。"""
    runtime_settings = settings or get_settings()
    return _create_chat_model(
        model=runtime_settings.llm_model,
        model_provider=runtime_settings.llm_model_provider,
        temperature=runtime_settings.llm_temperature,
        max_tokens=runtime_settings.llm_max_tokens,
        timeout=runtime_settings.llm_timeout,
        max_retries=runtime_settings.llm_max_retries,
        api_key=runtime_settings.llm_api_key,
        base_url=runtime_settings.llm_base_url,
    )


def _build_model_input(
    prompt: PromptInput,
    system_prompt: str | None,
) -> PromptInput:
    """添加可选系统消息，同时保留 LangChain 输入类型。"""
    if not system_prompt:
        return prompt
    if isinstance(prompt, str):
        return [
            ("system", system_prompt),
            ("human", prompt),
        ]
    return [
        ("system", system_prompt),
        *prompt,
    ]


async def call_cloud_llm(
    prompt: PromptInput,
    *,
    system_prompt: str | None = None,
    settings: Settings | None = None,
) -> Any:
    """异步调用云端 LLM，并返回其原生响应。"""
    runtime_settings = settings or get_settings()
    model = _get_chat_model(runtime_settings)
    gate = await _acquire_concurrency_slot(runtime_settings)
    try:
        response = await model.ainvoke(
            _build_model_input(prompt, system_prompt)
        )
    finally:
        _release_concurrency_slot(runtime_settings, gate)
    log_event(
        logger,
        logging.INFO,
        "llm_factory.call.completed",
        model=runtime_settings.llm_model,
        structured=False,
    )
    return response


async def call_structured_llm(
    prompt: PromptInput,
    schema: type[Any] | dict[str, Any],
    *,
    system_prompt: str | None = None,
    settings: Settings | None = None,
    include_raw: bool = False,
    method: str | None = None,
) -> Any:
    """通过 ``with_structured_output`` 异步调用 LLM。"""
    runtime_settings = settings or get_settings()
    structured_model = _get_chat_model(runtime_settings).with_structured_output(
        schema,
        include_raw=include_raw,
        **(
            {"method": method or runtime_settings.llm_structured_method}
            if method or runtime_settings.llm_structured_method
            else {}
        ),
    )
    gate = await _acquire_concurrency_slot(runtime_settings)
    try:
        response = await structured_model.ainvoke(
            _build_model_input(prompt, system_prompt)
        )
    finally:
        _release_concurrency_slot(runtime_settings, gate)
    log_event(
        logger,
        logging.INFO,
        "llm_factory.call.completed",
        model=runtime_settings.llm_model,
        structured=True,
        include_raw=include_raw,
    )
    return response
