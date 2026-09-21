# 模块职责：从 MySQL 同步 FAQ 问题、分词结果和快照到 Redis。

import argparse
import logging
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.logging_config import configure_logging, log_event
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.logging_config import configure_logging, log_event

try:
    from ics_agent.app.config import get_settings
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.config import get_settings

try:
    from .service import FAQSnapshotSynchronizer
    from .tokenizer import JiebaTokenizer
    from .repositories import FAQRepository, RedisFAQSnapshotRepository
except ImportError:
    from ics_agent.faq.service import FAQSnapshotSynchronizer
    from ics_agent.faq.tokenizer import JiebaTokenizer
    from ics_agent.faq.repositories import (
        FAQRepository,
        RedisFAQSnapshotRepository,
    )


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def sync_faq(include_disabled: bool = False) -> dict[str, str | int]:
    synchronizer = FAQSnapshotSynchronizer(
        faq_repository=FAQRepository(),
        snapshot_repository=RedisFAQSnapshotRepository(),
        tokenizer=JiebaTokenizer(),
    )
    snapshot = synchronizer.sync(include_disabled=include_disabled)
    settings = get_settings()
    prefix = settings.redis_key_prefix
    return {
        "count": len(snapshot.documents),
        "ids": f"{prefix}{settings.faq_question_ids_key}",
        "questions": f"{prefix}{settings.faq_questions_key}",
        "tokens": f"{prefix}{settings.faq_question_tokens_key}",
        "meta": f"{prefix}{settings.faq_questions_meta_key}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load FAQ questions from MySQL and cache their tokenized snapshot in Redis.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include rows whose enabled field is 0.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    configure_logging()
    log_event(
        logging.getLogger("ics_agent.faq.sync_questions_to_redis"),
        logging.INFO,
        "faq.cli.sync.start",
        include_disabled=args.include_disabled,
    )
    result = sync_faq(include_disabled=args.include_disabled)
    log_event(
        logging.getLogger("ics_agent.faq.sync_questions_to_redis"),
        logging.INFO,
        "faq.cli.sync.success",
        count=result["count"],
    )
    print(f"Synced {result['count']} questions to Redis.")
    print(f"question ids key: {result['ids']}")
    print(f"questions key: {result['questions']}")
    print(f"tokens key: {result['tokens']}")
    print(f"meta key: {result['meta']}")


if __name__ == "__main__":
    main()
