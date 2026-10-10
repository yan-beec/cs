"""对比 BGE-M3 稠密召回排序与 reranker 精排排序。

脚本固定先召回 ``top_k`` 个文档（默认 10），然后让本地
``bge-reranker-v2-m3`` 对同一批文档全部打分。它不会调用 LLM，也不会修改
向量库或运行时配置，适合评估“直接使用双塔召回 Top-5”是否会损失排序质量。

推荐运行方式（使用项目的 cs 环境）：

    C:\\Users\\lenovo\\Anaconda3\\envs\\cs\\python.exe \\
        -m ics_agent.rag.reranker.compare_dense_and_rerank \\
        --query "实名认证一直审核不成功"

也可以直接运行脚本文件：

    C:\\Users\\lenovo\\Anaconda3\\envs\\cs\\python.exe \\
        ics_agent\\rag\\reranker\\compare_dense_and_rerank.py \\
        -q "实名认证一直审核不成功"

``--json`` 可输出机器可读的完整结果；默认输出便于人工查看的对比表。
不传 ``--query`` 时，脚本会在终端提示输入问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    _project_root = Path(__file__).resolve().parents[3]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from ics_agent.app.core.logging_config import configure_logging
from ics_agent.rag.config import get_rag_settings
from ics_agent.rag.reranker.reranker import (
    RerankedDocument,
    RerankerService,
)
from ics_agent.rag.retrieval.retriever import RetrievedDocument
from ics_agent.rag.retrieval import aretrieve_documents


DEFAULT_TOP_K = 10
DEFAULT_DIRECT_TOP_K = 5


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数。"""
    parser = argparse.ArgumentParser(
        description=(
            "召回 Top-K 文档，并对同一批文档执行 reranker，比较两种排序。"
        ),
    )
    parser.add_argument(
        "-q",
        "--query",
        help="要测试的用户问题；不传时交互式输入。",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"双塔召回和完整精排的文档数，默认 {DEFAULT_TOP_K}。",
    )
    parser.add_argument(
        "--direct-top-k",
        type=int,
        default=DEFAULT_DIRECT_TOP_K,
        help=(
            "取消 reranker 时建议直接提供给 LLM 的双塔文档数，"
            f"仅用于输出提示，默认 {DEFAULT_DIRECT_TOP_K}。"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出完整 JSON，不输出人类可读表格。",
    )
    return parser


def _validate_args(query: str, top_k: int, direct_top_k: int) -> None:
    """校验脚本参数，尽早给出清晰错误。"""
    if not query.strip():
        raise ValueError("query 不能为空。")
    if top_k < 1:
        raise ValueError("top-k 必须大于等于 1。")
    if direct_top_k < 1:
        raise ValueError("direct-top-k 必须大于等于 1。")
    if direct_top_k > top_k:
        raise ValueError("direct-top-k 不能大于 top-k。")


def _document_preview(document: RetrievedDocument, *, length: int = 180) -> str:
    """生成单行文档摘要，避免完整正文淹没排序结果。"""
    text = " ".join(document.text.split())
    return text if len(text) <= length else f"{text[:length]}..."


def _source(document: Any) -> str:
    metadata = getattr(document, "metadata", {}) or {}
    return str(metadata.get("source") or metadata.get("file_name") or "")


def _comparison_rows(
    dense_documents: Sequence[RetrievedDocument],
    reranked_documents: Sequence[RerankedDocument],
) -> list[dict[str, Any]]:
    """按 chunk_id 合并两种排序，计算名次变化。"""
    dense_by_id = {document.chunk_id: document for document in dense_documents}
    rerank_by_id = {document.chunk_id: document for document in reranked_documents}

    rows: list[dict[str, Any]] = []
    for document in dense_documents:
        reranked = rerank_by_id.get(document.chunk_id)
        dense_rank = document.rank
        rerank_rank = reranked.rerank_rank if reranked else None
        rows.append(
            {
                "chunk_id": document.chunk_id,
                "source": _source(document),
                "dense_rank": dense_rank,
                "rerank_rank": rerank_rank,
                # 正数表示 reranker 将文档前移，负数表示后移。
                "rank_change": (
                    dense_rank - rerank_rank if rerank_rank is not None else None
                ),
                "dense_distance": document.distance,
                "rerank_score": (
                    reranked.rerank_score if reranked is not None else None
                ),
                "text": _document_preview(document),
            }
        )

    # 防止未来 reranker 返回了召回批次之外的文档时静默丢失信息。
    for document in reranked_documents:
        if document.chunk_id in dense_by_id:
            continue
        rows.append(
            {
                "chunk_id": document.chunk_id,
                "source": _source(document),
                "dense_rank": None,
                "rerank_rank": document.rerank_rank,
                "rank_change": None,
                "dense_distance": document.distance,
                "rerank_score": document.rerank_score,
                "text": _document_preview(document),
            }
        )

    return rows


def _result_payload(
    query: str,
    *,
    top_k: int,
    direct_top_k: int,
    dense_documents: Sequence[RetrievedDocument],
    reranked_documents: Sequence[RerankedDocument],
) -> dict[str, Any]:
    """构建 JSON 安全的完整对比结果。"""
    dense_order = [document.chunk_id for document in dense_documents]
    reranked_order = [document.chunk_id for document in reranked_documents]
    direct_dense_order = dense_order[:direct_top_k]
    direct_reranked_order = reranked_order[:direct_top_k]
    same_position_count = sum(
        dense_id == rerank_id
        for dense_id, rerank_id in zip(dense_order, reranked_order)
    )
    dense_top_ids = set(dense_order[:direct_top_k])
    reranked_top_ids = set(reranked_order[:direct_top_k])
    direct_top_overlap_k = min(
        direct_top_k,
        len(dense_order),
        len(reranked_order),
    )

    dense_rows = [
        {
            "rank": document.rank,
            "chunk_id": document.chunk_id,
            "distance": document.distance,
            "source": _source(document),
            "text": _document_preview(document),
            "metadata": document.metadata,
        }
        for document in dense_documents
    ]
    rerank_rows = [
        {
            "rank": document.rerank_rank,
            "retrieval_rank": document.retrieval_rank,
            "chunk_id": document.chunk_id,
            "rerank_score": document.rerank_score,
            "distance": document.distance,
            "source": _source(document),
            "text": _document_preview(document),
            "metadata": document.metadata,
        }
        for document in reranked_documents
    ]
    return {
        "query": query,
        "top_k": top_k,
        "direct_top_k": direct_top_k,
        "retrieved_count": len(dense_documents),
        "reranked_count": len(reranked_documents),
        "dense_order": dense_order,
        "reranked_order": reranked_order,
        "same_order": dense_order == reranked_order,
        "top1_same": bool(dense_order and reranked_order and dense_order[0] == reranked_order[0]),
        "same_position_count": same_position_count,
        "changed_position_count": max(len(dense_order), len(reranked_order))
        - same_position_count,
        "direct_top_overlap": len(dense_top_ids & reranked_top_ids),
        "direct_top_overlap_k": direct_top_overlap_k,
        "direct_dense_order": direct_dense_order,
        "direct_reranked_order": direct_reranked_order,
        "dense_top_k": dense_rows,
        "reranked_top_k": rerank_rows,
        "comparison": _comparison_rows(dense_documents, reranked_documents),
    }


def _print_table(payload: dict[str, Any]) -> None:
    """打印适合终端阅读的排序对比。"""
    print(f"问题: {payload['query']}")
    print(
        f"双塔召回: Top-{payload['top_k']}；"
        f"建议取消 reranker 时直接取双塔 Top-{payload['direct_top_k']}。"
    )
    print(
        "排序摘要: "
        f"Top1相同={'是' if payload['top1_same'] else '否'}，"
        f"相同位置={payload['same_position_count']}/{payload['retrieved_count']}，"
        f"直接Top{payload['direct_top_k']}重合="
        f"{payload['direct_top_overlap']}/{payload['direct_top_overlap_k']}"
    )
    timing = payload.get("timing_ms", {})
    if timing:
        print(
            f"耗时: 双塔召回={timing.get('retrieval')} ms，"
            f"reranker={timing.get('rerank')} ms（首次运行可能包含模型加载）"
        )

    print("\n双塔召回排序（原始顺序）")
    for row in payload["dense_top_k"]:
        print(
            f"  [{row['rank']:>2}] distance={row['distance']:.6f} "
            f"id={row['chunk_id']} source={row['source'] or '-'}\n"
            f"       {row['text']}"
        )

    print("\nReranker 精排排序（对同一批文档重新打分）")
    for row in payload["reranked_top_k"]:
        print(
            f"  [{row['rank']:>2}] 原召回#{row['retrieval_rank']:>2} "
            f"score={row['rerank_score']:.6f} id={row['chunk_id']} "
            f"source={row['source'] or '-'}\n"
            f"       {row['text']}"
        )

    print("\n名次变化（正数=reranker 前移，负数=后移）")
    header = f"{'原召回':>6} {'精排':>6} {'变化':>6}  chunk_id"
    print(header)
    print("-" * len(header))
    for row in payload["comparison"]:
        dense_rank = str(row["dense_rank"]) if row["dense_rank"] else "-"
        rerank_rank = str(row["rerank_rank"]) if row["rerank_rank"] else "-"
        change = str(row["rank_change"]) if row["rank_change"] is not None else "-"
        print(
            f"{dense_rank:>6} {rerank_rank:>6} {change:>6}  "
            f"{row['chunk_id']}"
        )


async def compare(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    direct_top_k: int = DEFAULT_DIRECT_TOP_K,
) -> dict[str, Any]:
    """执行一次 Top-K 双塔与 reranker 排序对比。"""
    _validate_args(query, top_k, direct_top_k)

    # 显式传 top_k，避免当前 .env 中较小的 RAG_RERANKER_INPUT_TOP_K
    # （例如 6）把本次实验悄悄截断成少于 10 个候选。
    retrieval_started_at = time.perf_counter()
    dense_documents = await aretrieve_documents(query, top_k=top_k)
    retrieval_elapsed_ms = round(
        (time.perf_counter() - retrieval_started_at) * 1000,
        1,
    )
    if not dense_documents:
        raise RuntimeError("双塔召回结果为空，无法进行排序对比。")

    # 本次实验只临时覆盖 reranker 输入/输出数量，不写入 .env，也不改变
    # 应用进程级 reranker 单例。这样即使线上配置为 input_top_k=6，脚本仍
    # 会对本次召回得到的全部 Top-10 文档打分。
    settings = get_rag_settings().model_copy(
        update={
            "rag_reranker_input_top_k": len(dense_documents),
            "rag_reranker_output_top_k": len(dense_documents),
        }
    )
    reranker = RerankerService(settings=settings)
    rerank_started_at = time.perf_counter()
    reranked_documents = await reranker.arerank(
        query,
        dense_documents,
        top_k=len(dense_documents),
    )
    payload = _result_payload(
        query,
        top_k=top_k,
        direct_top_k=direct_top_k,
        dense_documents=dense_documents,
        reranked_documents=reranked_documents,
    )
    # 首次运行的计时可能包含模型加载；重复运行更适合比较纯推理耗时。
    payload["timing_ms"] = {
        "retrieval": retrieval_elapsed_ms,
        "rerank": round((time.perf_counter() - rerank_started_at) * 1000, 1),
    }
    return payload


def main() -> None:
    """命令行入口。"""
    args = build_parser().parse_args()
    query = args.query
    if query is None:
        try:
            query = input("请输入要测试的问题：").strip()
        except EOFError as exc:
            raise SystemExit(
                "未提供问题。请使用 -q/--query，或在交互式终端中运行脚本。"
            ) from exc
    try:
        _validate_args(query, args.top_k, args.direct_top_k)
        configure_logging()
        payload = asyncio.run(
            compare(
                query,
                top_k=args.top_k,
                direct_top_k=args.direct_top_k,
            )
        )
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise SystemExit(f"对比失败: {type(exc).__name__}: {exc}") from exc

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_table(payload)


if __name__ == "__main__":
    main()
