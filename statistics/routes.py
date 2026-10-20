"""Statistics endpoints and dependency-free assets, integrated with ICS admin."""

from datetime import date, datetime
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ics_agent.app.core.auth import require_admin
from ics_agent.channels import list_channels
from quality.query.runtime import ArchiveUnavailable, get_query_runtime
from .periods import BEIJING, ReportPeriod
from .repository import StatisticsRepository

logger = logging.getLogger(__name__)
router = APIRouter()
api = APIRouter(prefix="/admin/api/statistics", dependencies=[Depends(require_admin)])
WEB = Path(__file__).resolve().parent / "web"


@router.get("/admin/statistics/assets/{filename}", include_in_schema=False)
async def asset(filename: Literal["reports.js", "reports.css"]):
    return FileResponse(WEB / filename, media_type="text/javascript" if filename.endswith(".js") else "text/css",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


def report_period(mode: Literal["day", "month", "year"] = "day", date: date | None = None):
    try:
        return ReportPeriod.build(mode, date or datetime.now(BEIJING).date())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def report_scope(
    source_key: str | None = Query(None, min_length=1, max_length=128),
    account_id: int | None = Query(None, ge=1, le=2**63 - 1),
):
    if (source_key is None) != (account_id is None):
        raise HTTPException(status_code=422, detail="来源和账号需一起选择。")
    if source_key and (source_key != source_key.strip() or "\x00" in source_key):
        raise HTTPException(status_code=422, detail="来源格式无效。")
    return {"source_key": source_key, "account_id": account_id}


async def run_report(method, period, **kwargs):
    runtime = get_query_runtime()
    if not runtime.ready or runtime.repository is None:
        raise HTTPException(status_code=503, detail="统计数据暂不可用，请确认对话归档查询已启用且连接正常。", headers={"Retry-After": "10"})
    try:
        repository = StatisticsRepository(runtime.repository.pool)
        # Reuse the existing bounded query executor, semaphore and reader pool.
        # A failed report must not mark unrelated archive pages unavailable.
        return await runtime._execute(getattr(repository, method), period, **kwargs)
    except ArchiveUnavailable:
        raise HTTPException(status_code=503, detail="统计查询繁忙，请稍后重试。", headers={"Retry-After": "10"}) from None
    except Exception as error:
        logger.warning("Statistics query unavailable: %s", type(error).__name__)
        raise HTTPException(status_code=503, detail="统计查询暂不可用，请稍后重试。", headers={"Retry-After": "10"}) from None


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
    period: ReportPeriod = Depends(report_period), scope: dict = Depends(report_scope),
    channel_key: str | None = Query(None, min_length=1, max_length=128),
):
    return await run_report("time_report", period, **scope, channel_key=channel_key)


@api.get("/agents")
async def agent_report(
    period: ReportPeriod = Depends(report_period), scope: dict = Depends(report_scope),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(20, ge=1, le=100),
):
    return await run_report("agent_report", period, **scope, page=page, page_size=page_size)


@api.get("/quality")
async def quality_report(
    period: ReportPeriod = Depends(report_period), scope: dict = Depends(report_scope),
    page: int = Query(1, ge=1, le=100000), page_size: int = Query(20, ge=1, le=100),
):
    return await run_report("quality_report", period, **scope, page=page, page_size=page_size)


router.include_router(api)
