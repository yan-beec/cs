"""Administrator-only archive browsing, isolated from customer service routes."""

import asyncio
import threading
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ics_agent.app.core.auth import AdminPrincipal, require_admin
from quality.assignment.runtime import get_assignment_sync_runtime
from quality.processing.stat_metrics import DRILL_ALIASES, METRICS
from .repository import BLACKLIST_STATE, ArchiveFilters, ReviewConflict, ReviewValidationError, customer_round_scoped
from .export import ExportCancelled, ExportFile, ExportNotFound, ExportTooLarge
from .runtime import ArchiveQueryAborted, ArchiveBusy, ArchiveQueryTimeout, ArchiveUnavailable, ExportBusy, QUERY_TIMEOUT_DETAIL, get_query_runtime

router = APIRouter(prefix="/admin/api/quality", dependencies=[Depends(require_admin)])


def archive_filters(
    source_key: str | None = Query(None, min_length=1, max_length=128),
    account_id: int | None = Query(None, ge=1, le=2**63 - 1),
    channel_key: str | None = Query(None, max_length=128),
    inbox_id: int | None = Query(None, ge=1, le=2**63 - 1),
    customer_query: str | None = Query(None, max_length=200),
    conversation_id: int | None = Query(None, ge=1, le=2**63 - 1),
    round_no: int | None = Query(None, ge=1, le=2**31 - 1),
    reception_type: Literal["ai_only", "human", "unknown"] | None = None,
    agent_id: str | None = Query(None, min_length=1, max_length=128),
    agent_scope: Literal["responsible", "replied", "assigned", "current"] = "responsible",
    time_field: Literal["started_at", "message_at", "resolved_at", "rating_at", "handoff_at", "reply_at",
                        "assigned_at"] = "started_at",
    start: datetime | None = None, end: datetime | None = None,
    rating_status: Literal["not_invited", "pending", "rated", "unlinked"] | None = None,
    rating: int | None = Query(None, ge=1, le=5),
    # 评价分数比较方式：eq=等于、gt=大于、lt=小于；只在指定 rating 时生效。
    rating_op: Literal["eq", "gt", "lt"] = "eq",
    # 流单：转人工后截至查询时仍无公开人工回复的轮次（与统计报表“流单数”同一口径）。
    lost: bool | None = None,
    keyword: str | None = Query(None, max_length=200),
    message_scope: Literal["all", "contact", "agent", "bot", "private"] = "all",
    has_attachment: bool | None = None,
    status: Literal["active", "resolved", "idle_boundary", "unknown"] | None = None,
    ai_review_status: Literal["passed", "failed", "pending"] | None = None,
    human_review_status: Literal["passed", "failed", "pending"] | None = None,
    blacklist: Literal["ever", "active"] | None = None,
    # 锁定时刻：进入归档模块（或报表生成）时的时刻；本次浏览的判定和聊天内容都截至该时刻。
    as_of: datetime | None = None,
    # 统计报表下钻：指标 + 统计窗口（start/end）+ 记入坐席（metric_seat，空字符串=无该坐席的轮次）。
    metric: str | None = Query(None, max_length=64),
    metric_seat: str | None = Query(None, max_length=128),
    outcome: Literal["ai_only", "handoff", "refused", "effective_human", "lost"] | None = None,
    assisted: bool | None = None,
    unassigned: bool | None = None,
    dialogue: Literal["dialogue", "high_quality"] | None = None,
) -> ArchiveFilters:
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if start and end and start >= end:
        raise HTTPException(status_code=422, detail="结束时间必须晚于开始时间。")
    if as_of is not None and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if metric is not None:
        if metric not in METRICS and metric not in DRILL_ALIASES:
            raise HTTPException(status_code=422, detail="统计指标无效。")
        if start is None or end is None:
            raise HTTPException(status_code=422, detail="统计指标下钻需要统计周期的开始和结束时间。")
        if metric_seat not in (None, "none", "any") and (not source_key or account_id is None):
            raise HTTPException(status_code=422, detail="按坐席下钻时需要同时指定来源和账号。")
        # "none": rounds the metric credits to no seat (e.g. lost transfers without an ICS assignment).
        # "any": rounds credited to some seat (the report's seat totals).
        if metric_seat == "none":
            metric_seat = ""
        elif metric_seat == "any":
            metric_seat = "*"
    elif metric_seat is not None:
        raise HTTPException(status_code=422, detail="坐席下钻需要同时指定统计指标。")
    if agent_id and (not source_key or account_id is None):
        raise HTTPException(status_code=422, detail="筛选坐席时需要同时指定来源和账号。")
    if inbox_id is not None and (not source_key or account_id is None):
        raise HTTPException(status_code=422, detail="筛选收件箱时需要同时指定来源和账号。")
    if blacklist and not BLACKLIST_STATE["ready"]:
        raise HTTPException(status_code=422, detail="黑名单筛选暂不可用：请先在归档库执行迁移 012_contact_blacklist。")
    return ArchiveFilters(
        source_key=source_key, account_id=account_id, channel_key=channel_key, inbox_id=inbox_id,
        customer_query=customer_query.strip() if customer_query else None,
        conversation_id=conversation_id, round_no=round_no, reception_type=reception_type,
        agent_id=agent_id, agent_scope=agent_scope, time_field=time_field, start=start, end=end,
        rating_status=rating_status, rating=rating, rating_op=rating_op if rating is not None else "eq",
        lost=True if lost else None, keyword=keyword.strip() if keyword else None,
        message_scope=message_scope, has_attachment=has_attachment, status=status,
        ai_review_status=ai_review_status, human_review_status=human_review_status, blacklist=blacklist,
        as_of=as_of, metric=metric, metric_seat=metric_seat, outcome=outcome, assisted=assisted,
        unassigned=unassigned, dialogue=dialogue,
    )


def locked_only(filters: ArchiveFilters) -> ArchiveFilters:
    """Only the locked time of the filters (details opened without round-level filters)."""
    return ArchiveFilters(as_of=filters.as_of)


async def _call(method, *args, abort=None, **kwargs):
    try:
        return await get_query_runtime().call(method, *args, **({"abort": abort} if abort is not None else {}), **kwargs)
    except ArchiveQueryAborted:
        return Response(status_code=499)
    except ArchiveQueryTimeout:
        raise HTTPException(status_code=503, detail=QUERY_TIMEOUT_DETAIL, headers={"Retry-After": "10"}) from None
    except ArchiveBusy:
        raise HTTPException(status_code=503, detail="查询繁忙，请稍后重试。", headers={"Retry-After": "2"}) from None
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="对话归档查询暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None


async def _query(request: Request, method, *args, **kwargs):
    """断开时取消排队；已经执行的查询仍由运行时等待结束并释放名额。"""
    abort = threading.Event()
    task = asyncio.create_task(_call(method, *args, abort=abort, **kwargs))
    try:
        while True:
            completed, _ = await asyncio.wait({task}, timeout=0.25)
            if await request.is_disconnected():
                abort.set()
                return Response(status_code=499)
            if completed:
                return task.result()
    finally:
        if not task.done():
            abort.set()
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@router.get("/status")
async def quality_status():
    runtime = get_query_runtime()
    assignment = get_assignment_sync_runtime()
    result = {"enabled": runtime.enabled, "ready": runtime.ready, "status": runtime.status,
              "assignment_sync_status": assignment.status,
              "assignment_sync_pending_count": assignment.pending_count,
              "assignment_sync_blocked_count": assignment.blocked_count,
              "assignment_sync_counts_sampled_at": assignment.counts_sampled_at,
              "queue_decision_sync_pending_count": assignment.queue_pending_count,
              "queue_decision_sync_blocked_count": assignment.queue_blocked_count}
    # Server clock for the archive's locked time ("as_of"), independent of the browser clock.
    result["server_time"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    if runtime.ready:
        try:
            result.update(await runtime.call("processing_status"))
        except ArchiveUnavailable:
            result.update(ready=False, status="unavailable")
    return result


@router.get("/filters")
async def filter_options():
    return await _call("filter_options")


@router.get("/overview")
async def overview(request: Request, filters: ArchiveFilters = Depends(archive_filters)):
    return await _query(request, "overview", filters)


@router.get("/presence")
async def presence():
    return await get_query_runtime().presence()


@router.get("/presence/daily")
async def presence_daily(day: date | None = None, include_hidden: bool = False):
    return await get_query_runtime().presence_daily(day, **({"include_hidden": True} if include_hidden else {}))


@router.get("/presence/agents")
async def presence_agents(
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=100),
    include_hidden: bool = False,
):
    return await get_query_runtime().presence_agents(page, page_size, include_hidden)


@router.delete("/presence/agents/{agent_id}")
async def presence_remove_agent(
    agent_id: int, source_key: str = Query(..., min_length=1, max_length=128),
    account_id: int = Query(..., ge=1, le=2**63-1),
):
    if not 0<agent_id<=2**63-1 or source_key != source_key.strip() or "\x00" in source_key:
        raise HTTPException(status_code=422, detail="坐席标识或来源无效。")
    result = await get_query_runtime().presence_remove_agent(source_key, account_id, agent_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="本地坐席不存在，请刷新名单。")
    if result["status"] == "scope_changed":
        raise HTTPException(status_code=409, detail="Chatwoot 来源或账号已变更，请刷新名单后重试。")
    if result["status"] != "ready":
        raise HTTPException(status_code=503, detail="坐席管理暂不可用，请确认状态采集及数据库迁移后重试。")
    return result


@router.get("/presence/timeline")
async def presence_timeline(
    agent_id: int = Query(..., ge=1, le=2**63 - 1), day: date | None = None,
):
    return await get_query_runtime().presence_timeline(day, agent_id)


@router.get("/rounds")
async def rounds(
    request: Request,
    filters: ArchiveFilters = Depends(archive_filters),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(30, ge=1, le=100),
):
    return await _query(request, "list_rounds", filters, page=page, page_size=page_size)


@router.get("/customers")
async def customers(
    request: Request,
    filters: ArchiveFilters = Depends(archive_filters),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(30, ge=1, le=100),
):
    return await _query(request, "list_customers", filters, page=page, page_size=page_size)


@router.get("/unlinked-ratings")
async def unlinked_ratings(
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(30, ge=1, le=100),
):
    return await _call("unlinked_ratings", page=page, page_size=page_size)


class ExportResponse(FileResponse):
    def __init__(self, artifact: ExportFile):
        self.artifact = artifact
        super().__init__(artifact.path, filename=artifact.filename,
                         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.artifact.cleanup()


async def _generate_export(request, mode, filters, *, runtime=None, **scope):
    task = asyncio.create_task((runtime or get_query_runtime()).export(mode, filters, **scope))
    artifact = None
    try:
        while True:
            completed, _ = await asyncio.wait({task}, timeout=0.25)
            # Uvicorn does not cancel handlers when a browser aborts its fetch.
            # Explicitly cancel and drain the independent export worker instead.
            if await request.is_disconnected():
                return None
            if completed:
                artifact = task.result()
                return artifact
    finally:
        if artifact is None:
            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
            if results and isinstance(results[0], ExportFile):
                results[0].cleanup()


async def _export(request, mode, filters, **scope):
    try:
        artifact = await _generate_export(request, mode, filters, **scope)
    except ExportCancelled:
        raise HTTPException(status_code=503, detail="服务正在重启，请稍后重试。", headers={"Retry-After": "10"}) from None
    except ExportBusy:
        raise HTTPException(status_code=429, detail="已有归档导出正在生成，请稍后再试。", headers={"Retry-After": "3"}) from None
    except ExportTooLarge as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except ExportNotFound:
        raise HTTPException(status_code=404, detail="未找到要导出的归档记录。") from None
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="归档导出暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None
    return ExportResponse(artifact) if artifact is not None else Response(status_code=499)


@router.get("/export.xlsx")
async def export_filtered(
    request: Request,
    mode: Literal["rounds", "customers", "unlinked-ratings"] = "rounds",
    filters: ArchiveFilters = Depends(archive_filters),
):
    return await _export(request, mode, filters)


@router.get("/rounds/{round_id}/export.xlsx")
async def export_round(request: Request, round_id: str, as_of: datetime | None = None):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    return await _export(request, "rounds", ArchiveFilters(as_of=as_of), round_id=round_id)


@router.get("/customers/{customer_id}/export.xlsx")
async def export_customer(request: Request, customer_id: str, filters: ArchiveFilters = Depends(archive_filters)):
    if len(customer_id) > 200:
        raise HTTPException(status_code=422, detail="用户标识无效。")
    # 按客户筛选打开的详情带上筛选条件：只导出命中轮次；不带轮次级条件时为完整历史。
    return await _export(request, "customers", filters if customer_round_scoped(filters) else locked_only(filters),
                         customer_id=customer_id)


@router.get("/rounds/{round_id}")
async def round_detail(
    request: Request,
    round_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(100, ge=1, le=200),
    as_of: datetime | None = None,
):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    scope = {"as_of": as_of} if as_of is not None else {}
    result = await _query(request, "round_detail", round_id, page=page, page_size=page_size, **scope)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该轮对话。")
    return result


class RatingReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["valid", "invalid"]
    revision: int = Field(ge=0, le=2**63 - 1)
    content_revision: str = Field(min_length=1, max_length=128)


@router.get("/rounds/{round_id}/rating-review")
async def rating_review(round_id: str):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    result = await _call("rating_review", round_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该轮对话。")
    return result


@router.put("/rounds/{round_id}/rating-review")
async def save_rating_review(round_id: str, body: RatingReviewInput,
                             principal: AdminPrincipal = Depends(require_admin)):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    try:
        result = await _call("save_rating_review", round_id, body.status, principal.username,
                             body.revision, body.content_revision)
    except ReviewConflict:
        raise HTTPException(status_code=409, detail="已被他人修改或评分内容已变化，请刷新后重试。") from None
    except ReviewValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该轮对话。")
    return result


class RoundReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ai_status: Literal["passed", "failed", "pending"]
    human_status: Literal["passed", "failed", "pending"] | None
    remark: str = Field(default="", max_length=5000)
    revision: int = Field(default=0, ge=0, le=2**63 - 1)

    @field_validator("remark")
    @classmethod
    def valid_remark(cls, value):
        if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("备注包含无效字符。")
        return value


@router.get("/rounds/{round_id}/review")
async def round_review(round_id: str):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    result = await _call("round_review", round_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该轮对话。")
    return result


@router.put("/rounds/{round_id}/review")
async def save_round_review(
    round_id: str, body: RoundReviewInput, principal: AdminPrincipal = Depends(require_admin),
):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    try:
        result = await _call("save_round_review", round_id, body.ai_status, body.human_status, body.remark,
                             principal.username, body.revision)
    except ReviewConflict:
        raise HTTPException(status_code=409, detail="已被他人修改，请刷新（重新读取）后再保存。") from None
    except ReviewValidationError as error:
        raise HTTPException(status_code=422, detail={"message": str(error), "reception_type": error.reception_type}) from None
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该轮对话，备注未保存。")
    return result


@router.get("/customers/{customer_id}/history")
async def customer_history(
    request: Request,
    customer_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(100, ge=1, le=200),
    filters: ArchiveFilters = Depends(archive_filters),
):
    if len(customer_id) > 200:
        raise HTTPException(status_code=422, detail="用户标识无效。")
    # 带轮次级筛选（坐席、时间、关键词、评价、质检等）时只返回命中轮次的消息。
    scope = ({"filters": filters} if customer_round_scoped(filters)
             else {"filters": locked_only(filters)} if filters.as_of is not None else {})
    result = await _query(request, "customer_history", customer_id, page=page, page_size=page_size, **scope)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该用户。")
    return result


@router.get("/customers/{customer_id}/rounds")
async def customer_rounds(
    request: Request,
    customer_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=100),
    filters: ArchiveFilters = Depends(archive_filters),
):
    if len(customer_id) > 200:
        raise HTTPException(status_code=422, detail="用户标识无效。")
    scope = ({"filters": filters} if customer_round_scoped(filters)
             else {"filters": locked_only(filters)} if filters.as_of is not None else {})
    result = await _query(request, "customer_rounds", customer_id, page=page, page_size=page_size, **scope)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该用户。")
    return result


@router.get("/messages/{message_id}/versions")
async def message_versions(
    message_id: int, source_key: str = Query(..., min_length=1, max_length=128),
    account_id: int = Query(..., ge=1, le=2**63 - 1),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(100, ge=1, le=200),
):
    if not 0 < message_id <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="消息编号无效。")
    return await _call("message_versions", source_key, account_id, message_id, page=page, page_size=page_size)


@router.get("/events/{event_id}")
async def raw_event(
    event_id: int, source_key: str = Query(..., min_length=1, max_length=128),
    account_id: int = Query(..., ge=1, le=2**63 - 1),
):
    if not 0 < event_id <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="事件编号无效。")
    body = await _call("raw_event", event_id, source_key, account_id)
    if body is None:
        raise HTTPException(status_code=404, detail="未找到该原始事件。")
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="chatwoot-event-{event_id}.json"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
