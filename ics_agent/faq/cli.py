"""Command-line tools for the FAQ subsystem.

Use ``python -m ics_agent.faq <command>`` from the project root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from ics_agent.app.config import get_settings as get_app_settings
from ics_agent.app.logging_config import configure_logging, log_event
from ics_agent.faq.config import get_faq_settings
from ics_agent.faq.index import BM25Matcher
from ics_agent.faq.models import FAQResult, FAQSnapshot
from ics_agent.faq.repositories import (
    FAQAnswerCache,
    FAQRepository,
    RedisFAQSnapshotRepository,
)
from ics_agent.faq.service import (
    FAQService,
    FAQSnapshotSynchronizer,
    get_faq_service,
)
from ics_agent.faq.sync import async_sync_qna_from_excel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("ics_agent.faq.cli")


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load simple KEY=VALUE pairs for direct command-line execution."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def redis_client() -> redis.Redis:
    settings = get_app_settings()
    return redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout,
        socket_timeout=settings.redis_socket_timeout,
        retry=Retry(NoBackoff(), retries=0),
    )


def search_faq(
    query: str,
    threshold: float | None = None,
    client: redis.Redis | None = None,
):
    """Search FAQ through the shared service."""
    if client is None:
        return get_faq_service().query(query, threshold=threshold)

    snapshot_repository = RedisFAQSnapshotRepository(client)
    service = FAQService(
        faq_repository=FAQRepository(),
        snapshot_repository=snapshot_repository,
        answer_cache=FAQAnswerCache(client),
    )
    return service.query(query, threshold=threshold)


FAQSearchResult = FAQResult


def sync_faq_snapshot(include_disabled: bool = False) -> dict[str, str | int]:
    """Rebuild the Redis FAQ snapshot from PostgreSQL."""
    synchronizer = FAQSnapshotSynchronizer(
        faq_repository=FAQRepository(),
        snapshot_repository=RedisFAQSnapshotRepository(),
    )
    snapshot = synchronizer.sync(include_disabled=include_disabled)
    app_settings = get_app_settings()
    faq_settings = get_faq_settings()
    prefix = app_settings.redis_key_prefix
    return {
        "count": len(snapshot.documents),
        "ids": f"{prefix}{faq_settings.faq_question_ids_key}",
        "questions": f"{prefix}{faq_settings.faq_questions_key}",
        "tokens": f"{prefix}{faq_settings.faq_question_tokens_key}",
        "meta": f"{prefix}{faq_settings.faq_questions_meta_key}",
    }


def _tokenized_questions_key() -> str:
    app_settings = get_app_settings()
    faq_settings = get_faq_settings()
    return (
        f"{app_settings.redis_key_prefix}"
        f"{faq_settings.faq_question_tokens_key}"
    )


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


def load_bm25_from_redis(
    client: redis.Redis | None = None,
    key: str | None = None,
) -> BM25Matcher:
    """Build a BM25 matcher from the Redis FAQ snapshot."""
    client = client or redis_client()
    repository = RedisFAQSnapshotRepository(client)
    snapshot = repository.load()
    if key is not None and key != _tokenized_questions_key():
        raw_value = client.get(key)
        if raw_value is None:
            raise KeyError(f"Redis key does not exist: {key}")
        try:
            tokenized_questions = _validate_tokenized_questions(
                json.loads(raw_value)
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Redis key does not contain valid JSON: {key}"
            ) from exc
        snapshot = FAQSnapshot.from_lists(
            snapshot.question_ids,
            snapshot.questions,
            tokenized_questions,
        )
    return BM25Matcher(snapshot)


def _query_command(args: argparse.Namespace) -> None:
    service = get_faq_service()
    service.initialize(force=args.force_init)
    query = args.query or input("请输入问题：")
    result = service.query(query, threshold=args.threshold)
    log_event(
        logger,
        logging.INFO,
        "faq.cli.query.success",
        route=result.route,
        threshold=args.threshold,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def _sync_redis_command(args: argparse.Namespace) -> None:
    result = sync_faq_snapshot(include_disabled=args.include_disabled)
    log_event(
        logger,
        logging.INFO,
        "faq.cli.sync_redis.success",
        count=result["count"],
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _build_index_command(args: argparse.Namespace) -> None:
    matcher = load_bm25_from_redis(key=args.redis_key)
    log_event(
        logger,
        logging.INFO,
        "faq.cli.build_index.success",
        document_count=len(matcher.snapshot.documents),
        vocabulary_size=matcher.vocabulary_size,
    )
    print(
        f"Initialized BM25 with {len(matcher.snapshot.documents)} questions.\n"
        f"Redis key: {args.redis_key or _tokenized_questions_key()}\n"
        f"Vocabulary size: {matcher.vocabulary_size}"
    )


def _sync_excel_command(args: argparse.Namespace) -> None:
    result = asyncio.run(
        async_sync_qna_from_excel(
            args.excel_path,
            sheet_name=args.sheet_name,
            sync_redis=not args.skip_redis_sync,
            clear_answer_cache=False if args.keep_answer_cache else None,
        )
    )
    print(
        "FAQ sync completed: "
        f"source_rows={result.source_row_count}, "
        f"deleted={result.deleted_row_count}, "
        f"redis_snapshot={result.redis_snapshot_count}, "
        f"redis_snapshot_written={result.redis_snapshot_written}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FAQ subsystem command-line tools.")
    commands = parser.add_subparsers(dest="command", required=True)

    query = commands.add_parser("query", aliases=["search", "runtime"])
    query.add_argument("query", nargs="?", help="User question.")
    query.add_argument("--threshold", type=float, default=None)
    query.add_argument("--force-init", action="store_true")
    query.set_defaults(handler=_query_command)

    sync_redis = commands.add_parser("sync-redis")
    sync_redis.add_argument("--include-disabled", action="store_true")
    sync_redis.set_defaults(handler=_sync_redis_command)

    build_index = commands.add_parser("build-index")
    build_index.add_argument("--redis-key", default=None)
    build_index.set_defaults(handler=_build_index_command)

    sync_excel = commands.add_parser("sync-excel")
    sync_excel.add_argument("--excel-path", default=None)
    sync_excel.add_argument("--sheet-name", default=None)
    sync_excel.add_argument("--skip-redis-sync", action="store_true")
    sync_excel.add_argument("--keep-answer-cache", action="store_true")
    sync_excel.set_defaults(handler=_sync_excel_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    configure_logging()
    args = build_parser().parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
