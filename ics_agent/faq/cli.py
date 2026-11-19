"""FAQ 子系统命令行工具。

Use ``python -m ics_agent.faq <command>`` from the project root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from ics_agent.app.core.logging_config import configure_logging, log_event
from ics_agent.faq.models import FAQResult
from ics_agent.faq.service import (
    get_faq_service,
)
from ics_agent.faq.sync import async_sync_qna_from_excel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("ics_agent.faq.cli")


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """加载供命令行直接执行的简单 KEY=VALUE 配置。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def search_faq(
    query: str,
    threshold: float | None = None,
):
    """通过共享服务搜索 FAQ。"""
    return get_faq_service().query(query, threshold=threshold)


FAQSearchResult = FAQResult


def _query_command(args: argparse.Namespace) -> None:
    service = get_faq_service(args.channel)
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


def _sync_excel_command(args: argparse.Namespace) -> None:
    result = asyncio.run(
        async_sync_qna_from_excel(
            args.excel_path,
            sheet_name=args.sheet_name,
            refresh_bm25=args.refresh_runtime,
            channel_key=args.channel,
        )
    )
    print(
        "FAQ sync completed: "
        f"source_rows={result.source_row_count}, "
        f"deleted={result.deleted_row_count}, "
        f"bm25_documents={result.bm25_document_count}, "
        f"bm25_reloaded={result.bm25_reloaded}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FAQ subsystem command-line tools.")
    commands = parser.add_subparsers(dest="command", required=True)

    query = commands.add_parser("query", aliases=["search", "runtime"])
    query.add_argument("query", nargs="?", help="User question.")
    query.add_argument("--threshold", type=float, default=None)
    query.add_argument("--force-init", action="store_true")
    query.add_argument("--channel", default="xingliao")
    query.set_defaults(handler=_query_command)

    sync_excel = commands.add_parser("sync-excel")
    sync_excel.add_argument("--excel-path", default=None)
    sync_excel.add_argument("--sheet-name", default=None)
    sync_excel.add_argument("--refresh-runtime", action="store_true")
    sync_excel.add_argument("--channel", default="xingliao")
    sync_excel.set_defaults(handler=_sync_excel_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    configure_logging()
    args = build_parser().parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
