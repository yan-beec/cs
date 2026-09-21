# 模块职责：提供 FAQ 检索服务的兼容函数和命令行测试入口。

import argparse
import json
import logging
import os
from pathlib import Path

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

logger = logging.getLogger("ics_agent.faq.search_faq")

try:
    from ics_agent.app.config import get_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings

try:
    from .models import FAQResult
    from .service import get_faq_service
    from .tokenizer import JiebaTokenizer
except ImportError:
    from ics_agent.faq.models import FAQResult
    from ics_agent.faq.service import get_faq_service
    from ics_agent.faq.tokenizer import JiebaTokenizer


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


def search_faq(
    query: str,
    threshold: float | None = None,
    client: redis.Redis | None = None,
):
    if client is None:
        return get_faq_service().query(query, threshold=threshold)

    try:
        from .repositories import (
            FAQAnswerCache,
            FAQRepository,
            RedisFAQSnapshotRepository,
        )
        from .service import FAQService
    except ImportError:
        from ics_agent.faq.repositories import (
            FAQAnswerCache,
            FAQRepository,
            RedisFAQSnapshotRepository,
        )
        from ics_agent.faq.service import FAQService

    snapshot_repository = RedisFAQSnapshotRepository(client)
    service = FAQService(
        faq_repository=FAQRepository(),
        snapshot_repository=snapshot_repository,
        answer_cache=FAQAnswerCache(client),
    )
    return service.query(query, threshold=threshold)


FAQSearchResult = FAQResult


def tokenize_query(query: str) -> list[str]:
    return JiebaTokenizer().tokenize(query)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search FAQ with the shared BM25 service and return the route result.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="User question. If omitted, read one question from stdin.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Override FAQ_MATCH_THRESHOLD for this query.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    configure_logging()
    query = args.query or input("请输入问题：")
    result = search_faq(query, threshold=args.threshold)
    log_event(
        logger,
        logging.INFO,
        "faq.cli.search.success",
        route=result.route,
        threshold=args.threshold,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
