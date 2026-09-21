# 模块职责：提供 FAQ 答案缓存的兼容函数入口。

try:
    from .repositories import FAQAnswerCache, normalize_query
except ImportError:
    from repositories import FAQAnswerCache, normalize_query


def get_cached_answer(client, query: str, ttl_seconds: int | None = None):
    return FAQAnswerCache(client).get(query, ttl_seconds=ttl_seconds)


def set_cached_answer(
    client,
    query: str,
    answer: str,
    ttl_seconds: int | None = None,
    max_entries: int | None = None,
) -> bool:
    FAQAnswerCache(client).set(
        query,
        answer,
        ttl_seconds=ttl_seconds,
        max_entries=max_entries,
    )
    return True


def cache_key() -> str:
    from ics_agent.app.config import get_settings

    settings = get_settings()
    return f"{settings.redis_key_prefix}{settings.faq_query_answers_key}"


def cache_index_key() -> str:
    from ics_agent.app.config import get_settings

    settings = get_settings()
    return f"{settings.redis_key_prefix}{settings.faq_cache_index_key}"
