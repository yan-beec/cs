"""Administrator-only archive browsing, isolated from customer service routes."""

import asyncio
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from ics_agent.app.core.auth import require_admin
from quality.assignment.runtime import get_assignment_sync_runtime
from .repository import ArchiveFilters
from .export import ExportFile, ExportNotFound, ExportTooLarge
from .runtime import ArchiveUnavailable, ExportBusy, get_query_runtime

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
    time_field: Literal["started_at", "message_at", "resolved_at", "rating_at"] = "started_at",
    start: datetime | None = None, end: datetime | None = None,
    rating_status: Literal["not_invited", "pending", "rated", "unlinked"] | None = None,
    rating: int | None = Query(None, ge=1, le=5),
    keyword: str | None = Query(None, max_length=200),
    message_scope: Literal["all", "contact", "agent", "bot", "private"] = "all",
    has_attachment: bool | None = None,
    status: Literal["active", "resolved", "idle_boundary", "unknown"] | None = None,
) -> ArchiveFilters:
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if start and end and start >= end:
        raise HTTPException(status_code=422, detail="结束时间必须晚于开始时间。")
    if agent_id and (not source_key or account_id is None):
        raise HTTPException(status_code=422, detail="筛选坐席时需要同时指定来源和账号。")
    if inbox_id is not None and (not source_key or account_id is None):
        raise HTTPException(status_code=422, detail="筛选收件箱时需要同时指定来源和账号。")
    return ArchiveFilters(
        source_key=source_key, account_id=account_id, channel_key=channel_key, inbox_id=inbox_id,
        customer_query=customer_query.strip() if customer_query else None,
        conversation_id=conversation_id, round_no=round_no, reception_type=reception_type,
        agent_id=agent_id, agent_scope=agent_scope, time_field=time_field, start=start, end=end,
        rating_status=rating_status, rating=rating, keyword=keyword.strip() if keyword else None,
        message_scope=message_scope, has_attachment=has_attachment, status=status,
    )


async def _call(method, *args, **kwargs):
    try:
        return await get_query_runtime().call(method, *args, **kwargs)
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="对话归档查询暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None


@router.get("/status")
async def quality_status():
    runtime = get_query_runtime()
    assignment = get_assignment_sync_runtime()
    result = {"enabled": runtime.enabled, "ready": runtime.ready, "status": runtime.status,
              "assignment_sync_status": assignment.status,
              "assignment_sync_pending_count": assignment.pending_count,
              "assignment_sync_blocked_count": assignment.blocked_count,
              "assignment_sync_counts_sampled_at": assignment.counts_sampled_at}
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
async def overview(filters: ArchiveFilters = Depends(archive_filters)):
    return await _call("overview", filters)


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
    filters: ArchiveFilters = Depends(archive_filters),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(30, ge=1, le=100),
):
    return await _call("list_rounds", filters, page=page, page_size=page_size)


@router.get("/customers")
async def customers(
    filters: ArchiveFilters = Depends(archive_filters),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(30, ge=1, le=100),
):
    return await _call("list_customers", filters, page=page, page_size=page_size)


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


async def _generate_export(request, mode, filters, **scope):
    task = asyncio.create_task(get_query_runtime().export(mode, filters, **scope))
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
async def export_round(request: Request, round_id: str):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    return await _export(request, "rounds", ArchiveFilters(), round_id=round_id)


@router.get("/customers/{customer_id}/export.xlsx")
async def export_customer(request: Request, customer_id: str):
    if len(customer_id) > 200:
        raise HTTPException(status_code=422, detail="用户标识无效。")
    return await _export(request, "customers", ArchiveFilters(), customer_id=customer_id)


@router.get("/rounds/{round_id}")
async def round_detail(
    round_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(100, ge=1, le=200),
):
    if len(round_id) > 200:
        raise HTTPException(status_code=422, detail="轮次标识无效。")
    result = await _call("round_detail", round_id, page=page, page_size=page_size)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该轮对话。")
    return result


@router.get("/customers/{customer_id}/history")
async def customer_history(
    customer_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(100, ge=1, le=200),
):
    if len(customer_id) > 200:
        raise HTTPException(status_code=422, detail="用户标识无效。")
    result = await _call("customer_history", customer_id, page=page, page_size=page_size)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该用户。")
    return result


@router.get("/customers/{customer_id}/rounds")
async def customer_rounds(
    customer_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=100),
):
    if len(customer_id) > 200:
        raise HTTPException(status_code=422, detail="用户标识无效。")
    result = await _call("customer_rounds", customer_id, page=page, page_size=page_size)
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
