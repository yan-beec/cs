"""FAQ-first + RAG 完整查询链路入口。

在 PyCharm 中直接运行本文件，输入用户问题即可执行：

    query -> FAQ 命中直接回复
          -> FAQ 未命中 -> 意图识别
               -> 闲聊 -> LLM 回复
               -> 其他意图 -> 检索 -> 精排 -> LLM 回复

依赖的 PostgreSQL 和 Redis 需要提前启动。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ics_agent.app.logging_config import configure_logging
from ics_agent.rag.main import run_query
from ics_agent.rag.memory import get_conversation_memory_manager


def build_parser() -> argparse.ArgumentParser:
    """构建单次查询和交互式查询参数。"""
    parser = argparse.ArgumentParser(
        description="执行 FAQ-first + RAG 完整查询链路。",
    )
    parser.add_argument(
        "--query",
        "-q",
        help="执行一次指定问题；不传时进入交互式输入模式。",
    )
    parser.add_argument(
        "--thread-id",
        help="会话 ID；相同 ID 会复用 PostgreSQL 对话记忆。",
    )
    return parser


def _print_result(result: Mapping[str, Any]) -> None:
    """打印路由、链路诊断和最终回复。"""
    route = result.get("route")
    faq_route = result.get("faq_route")

    print("\n" + "=" * 72)
    print(f"最终路由: {route}")
    print(f"FAQ 路由: {faq_route}")

    if route == "faq":
        print("FAQ 命中: 是")
        print(f"匹配问题: {result.get('matched_question') or '缓存命中'}")
    else:
        is_chitchat = route == "chitchat"
        if is_chitchat:
            print("FAQ 命中: 否，意图为闲聊，已跳过检索和精排")
        else:
            print("FAQ 命中: 否，已进入 RAG")
        print(f"意图: {result.get('intent_label')}")
        print(f"意图置信度: {result.get('intent_confidence')}")
        if is_chitchat:
            print("召回数量: 0（闲聊分支已跳过）")
            print("精排数量: 0（闲聊分支已跳过）")
        else:
            print(f"召回数量: {result.get('retrieved_count')}")
            print(f"精排数量: {result.get('reranked_count')}")

    print("\n最终回复:")
    print(result.get("answer") or "未生成有效回复。")
    print("=" * 72)


async def execute_query(
    query: str,
    *,
    thread_id: str | None,
) -> None:
    """执行一次 FAQ-first + RAG 完整链路。"""
    normalized_query = query.strip()
    if not normalized_query:
        print("query 不能为空，请重新输入。")
        return

    try:
        result = await run_query(
            normalized_query,
            thread_id=thread_id,
        )
    except Exception as exc:
        print(f"\n查询失败: {type(exc).__name__}: {exc}")
        return

    _print_result(result)


async def interactive_loop(thread_id: str) -> None:
    """持续读取 query，直到用户输入 exit 或 quit。"""
    print("ics_agent FAQ + RAG 完整查询链路已启动。")
    print(f"当前会话 ID: {thread_id}")
    print("输入 exit 或 quit 退出。")

    while True:
        try:
            query = input("\n请输入 query：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return

        if query.lower() in {"exit", "quit"}:
            print("已退出。")
            return
        if not query:
            print("query 不能为空，请重新输入。")
            continue

        await execute_query(query, thread_id=thread_id)


def main() -> None:
    """启动交互式查询，或执行一次命令行 query。"""
    configure_logging()
    args = build_parser().parse_args()
    memory_manager = get_conversation_memory_manager()

    async def run() -> None:
        try:
            if args.query:
                thread_id = args.thread_id or f"root-main:{uuid4()}"
                print(f"当前会话 ID: {thread_id}")
                if not args.thread_id:
                    print("如需后续命令继续本次记忆，请复用这个会话 ID。")
                await execute_query(
                    args.query,
                    thread_id=thread_id,
                )
                return

            thread_id = args.thread_id or f"root-main:{uuid4()}"
            await interactive_loop(thread_id)
        finally:
            await memory_manager.shutdown()

    asyncio.run(run())


if __name__ == "__main__":
    main()
