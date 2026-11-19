"""Statistics endpoints and dependency-free assets, integrated with ICS admin."""

from datetime import date, datetime, time
import asyncio
import logging
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from ics_agent.app.core.auth import require_admin
from ics_agent.channels import list_channels
from quality.query.export import ExportCancelled, ExportTooLarge
from quality.query.routes import ExportResponse, _generate_export
from quality.query.runtime import ArchiveUnavailable, ExportBusy, QUERY_TIMEOUT_DETAIL, get_query_runtime
from .export import create_export
from .periods import BEIJING, ReportPeriod
from .repository import StatisticsRepository, classify, report_budget

logger = logging.getLogger(__name__)
router = APIRouter()
api = APIRouter(prefix="/admin/api/statistics", dependencies=[Depends(require_admin)])
WEB = Path(__file__).resolve().parent / "web"


@router.get("/admin/statistics/assets/{filename}", include_in_schema=False)
async def asset(filename: Literal["reports.js", "reports.css"]):
    return FileResponse(WEB / filename, media_type="text/javascript" if filename.endswith(".js") else "text/css",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


def report_period(
    mode: Literal["day", "month", "year", "custom"] = "day", date: date | None = None,
    start_date: date | None = None, end_date: date | None = None,
):
    try:
        if mode == "custom":
            if start_date is None or end_date is None:
                raise ValueError("自定义时间段需同时选择起始日期和结束日期。")
            return ReportPeriod.build(mode, start_date, end_date)
        if start_date is not None or end_date is not None:
            raise ValueError("起止日期仅用于自定义统计周期。")
        return ReportPeriod.build(mode, date or datetime.now(BEIJING).date())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def report_window(
    period: ReportPeriod = Depends(report_period),
    start_time: str | None = Query(None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$"),
    end_time: str | None = Query(None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$"),
):
    """R10 二级时刻：开始日期 start_time 起，到结束日期 end_time 那一分钟末（含）；不传即全天。"""
    try:
        return period.refine(time.fromisoformat(start_time) if start_time else None,
                             time.fromisoformat(end_time) if end_time else None)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def hourly_window(
    period: ReportPeriod = Depends(report_window),
    start_time: str | None = Query(None), end_time: str | None = Query(None),
):
    """时间统计/坐席细化/质检统计的二级时刻只到整点：回复类指标按小时预聚合（ops.round_reply_hours），
    分钟级起止会让同一小时的回复被整段丢弃或误计，所以开始须为 HH:00、结束须为 HH:59（含）。"""
    if (start_time and not start_time.endswith(":00")) or (end_time and not end_time.endswith(":59")):
        raise HTTPException(status_code=422, detail="该报表按整点统计：开始时刻须为整点（分=00），结束时刻须为该小时末（分=59）。")
    return period


def report_scope(
    source_key: str | None = Query(None, min_length=1, max_length=128),
    account_id: int | None = Query(None, ge=1, le=2**63 - 1),
):
    if (source_key is None) != (account_id is None):
        raise HTTPException(status_code=422, detail="来源和账号需一起选择。")
    if source_key and (source_key != source_key.strip() or "\x00" in source_key):
        raise HTTPException(status_code=422, detail="来源格式无效。")
    return {"source_key": source_key, "account_id": account_id}


async def run_report(request, method, period, **kwargs):
    runtime = get_query_runtime()
    if not runtime.ready or runtime.repository is None:
        raise HTTPException(status_code=503, detail="统计数据暂不可用，请确认对话归档查询已启用且连接正常。", headers={"Retry-After": "10"})
    abort = threading.Event()
    budget = report_budget(abort_event=abort)
    task = None
    try:
        repository = StatisticsRepository(runtime.repository.pool)
        # 复用交互名额，断开或外部取消时通知仍在运行的数据库线程中止。
        task = asyncio.create_task(runtime._execute(getattr(repository, method), period, budget=budget, **kwargs))
        while True:
            completed, _ = await asyncio.wait({task}, timeout=.25)
            if await request.is_disconnected():
                abort.set()
                return Response(status_code=499)
            if completed:
                return task.result()
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="统计查询繁忙，请稍后重试。", headers={"Retry-After": "10"}) from None
    except Exception as error:
        reason = classify(budget, error)
        if reason == 'aborted':
            return Response(status_code=499)
        if reason in ('expired', 'statement'):
            logger.warning("Statistics query timed out: %s (%s)", method, reason)
            raise HTTPException(status_code=503, detail=QUERY_TIMEOUT_DETAIL, headers={"Retry-After": "10"}) from None
        logger.warning("Statistics query unavailable: %s", type(error).__name__)
        raise HTTPException(status_code=503, detail="统计查询暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None
    finally:
        if task is not None:
            if not task.done():
                abort.set()
                task.cancel()
            # 断开恰逢任务失败时，也必须取走已完成任务的异常。
            await asyncio.gather(task, return_exceptions=True)


@api.get("/filters")
async def filters():
    runtime = get_query_runtime()
    if not runtime.ready:
        raise HTTPException(status_code=503, detail="统计数据暂不可用，请确认对话归档查询已启用。")
    try:
        options = await runtime.call("filter_options")
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="统计筛选项暂不可用，请稍后重试。") from None
    registered = {c.key: c.name for c in list_channels()}
    for row in options.get("channels", []):
        key = row.get("key") or "unmapped"
        registered.setdefault(key, row.get("name") or key)
    registered["unmapped"] = "未映射渠道"
    return {"sources": options.get("sources", []), "channels": [{"key": k, "name": v} for k, v in registered.items()]}


@api.get("/time")
async def time_report(
    request: Request,
    period: ReportPeriod = Depends(hourly_window), scope: dict = Depends(report_scope),
    channel_key: str | None = Query(None, min_length=1, max_length=128),
):
    return await run_report(request, "time_report", period, **scope, channel_key=channel_key)


@api.get("/agents")
async def agent_report(
    request: Request,
    period: ReportPeriod = Depends(hourly_window), scope: dict = Depends(report_scope),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(20, ge=1, le=100),
):
    return await run_report(request, "agent_report", period, **scope, page=page, page_size=page_size)


@api.get("/quality")
async def quality_report(
    request: Request,
    period: ReportPeriod = Depends(hourly_window), scope: dict = Depends(report_scope),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(20, ge=1, le=100),
):
    return await run_report(request, "quality_report", period, **scope, page=page, page_size=page_size)


@api.get("/users")
async def user_report(
    request: Request,
    period: ReportPeriod = Depends(report_window), scope: dict = Depends(report_scope),
    channel_key: str | None = Query(None, min_length=1, max_length=128),
    page: int = Query(1, ge=1, le=5), page_size: int = Query(20, ge=1, le=100),
):
    """R13 用户统计（成功转人工排行）：前 100 名，按 ICS 成功分配时间归入区间。"""
    return await run_report(request, "user_report", period, **scope, channel_key=channel_key, page=page, page_size=page_size)


@api.get("/users/detail")
async def user_detail(
    request: Request,
    period: ReportPeriod = Depends(report_window),
    source_key: str = Query(..., min_length=1, max_length=128),
    account_id: int = Query(..., ge=1, le=2**63 - 1),
    contact_id: str = Query(..., pattern=r"^[0-9A-Za-z_-]{1,64}$"),
    channel_key: str | None = Query(None, min_length=1, max_length=128),
):
    """R13 溯源：某用户区间内每一次成功转人工（分配时间、会话/轮次、分配坐席）。"""
    return await run_report(request, "user_detail", period, source_key=source_key, account_id=account_id,
                            contact_id=contact_id, channel_key=channel_key)


@api.get("/users/export.xlsx")
async def export_users(
    request: Request, period: ReportPeriod = Depends(report_window), scope: dict = Depends(report_scope),
    channel_key: str | None = Query(None, min_length=1, max_length=128),
):
    return await _export_kind(request, "users", period, scope, channel_key)


async def _export_kind(request, kind, period, scope, channel_key):
    try:
        artifact = await _generate_export(request, kind, period, runtime=get_query_runtime(),
            builder=create_export, **scope, channel_key=channel_key,
            channels=list({**{c.key: c.name for c in list_channels()}, "unmapped": "未映射渠道"}.items()))
    except ExportCancelled:
        raise HTTPException(status_code=503, detail="服务正在重启，请稍后重试。", headers={"Retry-After": "10"}) from None
    except ExportBusy:
        raise HTTPException(status_code=429, detail="已有报表正在导出，请稍后再试。", headers={"Retry-After": "3"}) from None
    except ExportTooLarge as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="统计导出暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None
    return ExportResponse(artifact) if artifact is not None else Response(status_code=499)


@api.get("/{kind}/export.xlsx")
async def export_report(
    request: Request, kind: Literal["time", "agents", "quality"],
    period: ReportPeriod = Depends(hourly_window), scope: dict = Depends(report_scope),
    channel_key: str | None = Query(None, min_length=1, max_length=128),
):
    if kind != "time" and channel_key is not None:
        raise HTTPException(status_code=422, detail="渠道筛选仅用于时间统计和用户统计。")
    return await _export_kind(request, kind, period, scope, channel_key)


router.include_router(api)
