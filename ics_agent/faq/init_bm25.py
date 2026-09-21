# 模块职责：从 Redis FAQ 快照读取分词问题并初始化 BM25 的命令行工具。

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.logging_config import configure_logging, log_event
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.logging_config import configure_logging, log_event

logger = logging.getLogger("ics_agent.faq.init_bm25")

try:
    from ics_agent.app.config import get_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings

try:
    from .index import BM25Matcher
    from .models import FAQSnapshot
    from .repositories import RedisFAQSnapshotRepository
except ImportError:
    from ics_agent.faq.index import BM25Matcher
    from ics_agent.faq.models import FAQSnapshot
    from ics_agent.faq.repositories import RedisFAQSnapshotRepository


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout,
        socket_timeout=settings.redis_socket_timeout,
        retry=Retry(NoBackoff(), retries=0),
    )


def tokenized_questions_key() -> str:
    settings = get_settings()
    return f"{settings.redis_key_prefix}{settings.faq_question_tokens_key}"


def _validate_tokenized_questions(value: Any) -> list[list[str]]:
    if not isinstance(value, list) or not all(
        isinstance(tokens, list) for tokens in value
    ):
        raise ValueError("Redis question tokens must be a JSON list of token lists.")
    normalized = [
        [str(token).strip() for token in tokens if str(token).strip()]
        for tokens in value
    ]
    if not normalized or not any(normalized):
        raise ValueError("Redis question tokens are empty.")
    return normalized


def load_tokenized_questions(
    client: redis.Redis | None = None,
    key: str | None = None,
) -> list[list[str]]:
    client = client or redis_client()
    redis_key = key or tokenized_questions_key()
    raw_value = client.get(redis_key)
    if raw_value is None:
        raise KeyError(f"Redis key does not exist: {redis_key}")
    try:
        return _validate_tokenized_questions(json.loads(raw_value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Redis key does not contain valid JSON: {redis_key}") from exc


def load_bm25_from_redis(
    client: redis.Redis | None = None,
    key: str | None = None,
) -> BM25Matcher:
    client = client or redis_client()
    repository = RedisFAQSnapshotRepository(client)
    snapshot = repository.load()
    if key is not None and key != tokenized_questions_key():
        snapshot = FAQSnapshot.from_lists(
            snapshot.question_ids,
            snapshot.questions,
            load_tokenized_questions(client=client, key=key),
        )
    return BM25Matcher(snapshot)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize a BM25 index from the FAQ snapshot stored in Redis.",
    )
    parser.add_argument(
        "--redis-key",
        help="Override the Redis key containing the JSON tokenized question list.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    configure_logging()
    log_event(
        logger,
        logging.INFO,
        "faq.cli.bm25_init.start",
        redis_key=args.redis_key or tokenized_questions_key(),
    )
    matcher = load_bm25_from_redis(key=args.redis_key)
    log_event(
        logger,
        logging.INFO,
        "faq.cli.bm25_init.success",
        document_count=len(matcher.snapshot.documents),
        vocabulary_size=matcher.vocabulary_size,
    )
    print(f"Initialized BM25 with {len(matcher.snapshot.documents)} questions.")
    print(f"Redis key: {args.redis_key or tokenized_questions_key()}")
    print(f"Vocabulary size: {matcher.vocabulary_size}")


if __name__ == "__main__":
    main()
