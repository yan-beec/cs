# 模块职责：提供 FAQ 常驻服务的初始化、查询调用和命令行入口。

import argparse
import json
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from ics_agent.app.logging_config import configure_logging, log_event
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from ics_agent.app.logging_config import configure_logging, log_event

try:
    from .models import FAQResult
    from .service import FAQService, get_faq_service
except ImportError:
    from ics_agent.faq.models import FAQResult
    from ics_agent.faq.service import FAQService, get_faq_service

FAQRuntime = FAQService
FAQRuntimeResult = FAQResult
logger = logging.getLogger("ics_agent.faq.faq_runtime")


def get_runtime() -> FAQService:
    return get_faq_service()


def initialize_faq_runtime(force: bool = False) -> FAQService:
    return get_runtime().initialize(force=force)


def execute_faq_query(query: str) -> FAQResult:
    return get_runtime().query(query)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Initialize the FAQ service and execute one query.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="User question. If omitted, read one question from stdin.",
    )
    parser.add_argument(
        "--force-init",
        action="store_true",
        help="Reload the FAQ snapshot and rebuild BM25 before querying.",
    )
    args = parser.parse_args()

    log_event(
        logger,
        logging.INFO,
        "faq.cli.runtime.start",
        force_init=args.force_init,
    )
    service = initialize_faq_runtime(force=args.force_init)
    query = args.query or input("请输入问题：")
    result = service.query(query)
    log_event(
        logger,
        logging.INFO,
        "faq.cli.runtime.success",
        route=result.route,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
