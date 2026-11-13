"""管理员留档任务管理及使用短期签名的大文件下载。"""
import asyncio
from dataclasses import asdict
from datetime import date
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.background import BackgroundTask

from ics_agent.app.core.auth import require_admin
from quality.query.repository import ArchiveFilters
from quality.query.routes import archive_filters
from .jobs import MEDIA_SUFFIX, get_offline_jobs
from .naming import download_name

router = APIRouter(prefix="/admin/api/quality/offline")
logger = logging.getLogger(__name__)


async def storage_call(operation):
    try:
        return await asyncio.to_thread(lambda: operation(get_offline_jobs()))
    except (sqlite3.Error, OSError) as error:
        logger.warning("Offline archive storage unavailable: %s", type(error).__name__)
        raise HTTPException(503, "离线留档目录暂时无法读写，请检查目录权限、磁盘空间及服务挂载后重试。") from None


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "month", "rounds", "customers"]
    month: str | None = None
    summary: str | None = Field(default=None, max_length=2000)
    view: Literal["rounds", "customers"] = "rounds"

    @field_validator("month")
    @classmethod
    def valid_month(cls, value):
        if value is not None:
            try:
                parsed = date.fromisoformat(value + "-01")
                if len(value) != 7 or parsed.year >= 9999:
                    raise ValueError()
            except ValueError:
                raise ValueError("请选择有效月份。") from None
        return value

    @model_validator(mode="after")
    def month_required(self):
        if self.mode == "month" and not self.month:
            raise ValueError("按月留档需要选择月份。")
        return self


def request_body(scope, filters):
    body = scope.model_dump()
    # 旧请求仍按命中轮次导出；新请求通过 view 显式选择客户视图的未归轮消息。
    if body["mode"] == "customers":
        body["mode"] = "rounds"
    if body["mode"] != "month":
        body["month"] = None
    if body["mode"] not in ("rounds", "customers"):
        body.pop("summary", None)
        body.pop("view", None)
    return {**body, "filters": asdict(filters)}


def query_enabled():
    from quality.query.config import QuerySettings
    if not QuerySettings().query_enabled:
        raise HTTPException(503, "归档查询未启用，无法创建留档。")


# 预估同一时间只跑一个：反复点击或多个管理员同时预估时不在归档库上叠加扫描。
estimate_lock = threading.Lock()


@router.post("/estimate", dependencies=[Depends(require_admin)])
async def estimate(scope: Scope, filters: ArchiveFilters = Depends(archive_filters)):
    """导出前预估消息 / 轮次 / 附件数量，返回提醒级别；任何失败都只影响本次请求。"""
    query_enabled()
    if not estimate_lock.acquire(blocking=False):
        raise HTTPException(429, "正在预估导出数量，请稍候再试。")
    try:
        from .repository import estimate as run_estimate
        settings = get_offline_jobs().settings
        return await asyncio.to_thread(run_estimate, request_body(scope, filters), settings)
    except HTTPException:
        raise
    except Exception as error:
        # 归档库不可达、连接池超时等：返回可读提示，不把异常抛给服务。
        logger.warning("Offline archive estimate failed: %s", type(error).__name__)
        raise HTTPException(503, "暂时无法预估导出数量（归档库繁忙或不可达），请稍后重试。") from None
    finally:
        estimate_lock.release()


@router.post("", status_code=202, dependencies=[Depends(require_admin)])
async def create(scope: Scope, filters: ArchiveFilters = Depends(archive_filters)):
    query_enabled()
    try:
        # 数量上限由生成子进程在同一数据库快照内再次检查，超限任务失败并提示原因，不影响服务。
        return await storage_call(lambda store: store.enqueue(request_body(scope, filters)))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("", dependencies=[Depends(require_admin)])
async def jobs():
    return await storage_call(lambda store: {"items": store.list(), "retention_days": store.settings.retention_days,
                                             "unclaimed_hours": store.settings.unclaimed_hours,
                                             "downloaded_hours": store.settings.downloaded_hours})


@router.delete("/{job_id}", dependencies=[Depends(require_admin)])
async def cancel(job_id: str):
    result = await storage_call(lambda store: store.cancel(job_id))
    if not result:
        raise HTTPException(404, "留档任务不存在。")
    return result


@router.delete("/{job_id}/record", dependencies=[Depends(require_admin)])
async def delete_record(job_id: str):
    try:
        deleted = await storage_call(lambda store: store.delete(job_id))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if not deleted:
        raise HTTPException(404, "留档任务不存在或已删除。")
    return {"deleted": True}


@router.post("/{job_id}/ticket", dependencies=[Depends(require_admin)])
async def ticket(job_id: str, part: str = Query("html", pattern="^(html|media)$")):
    try:
        signed = await storage_call(lambda store: store.ticket(job_id, part))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    suffix = "&part=media" if part == "media" else ""
    return {"url": f"/admin/api/quality/offline/{job_id}/download?ticket={signed}{suffix}"}


CHUNK_BYTES = 256 * 1024


def one_time_file(path, sent):
    """逐块读取（在线程池中执行，不阻塞事件循环）；最后一块成功交给连接后才标记发送完成。

    浏览器中途断开时 Starlette 会停止迭代，生成器走不到末尾，任务保持“待下载”。
    即使标记了完成，文件也会再保留 ARCHIVE_OFFLINE_DOWNLOADED_HOURS 供重新下载。
    """
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            yield chunk
    sent["complete"] = True


async def consume_after_download(job_id, sent):
    if not sent.get("complete"):
        return
    try:
        await asyncio.to_thread(lambda: get_offline_jobs().consume(job_id))
    except (sqlite3.Error, OSError) as error:
        # 记录失败不影响本次下载；任务保持“待下载”，按未领取保留时间清理。
        logger.warning("Offline archive download record failed: %s", type(error).__name__)


def attachment_header(filename):
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


@router.get("/{job_id}/download")
async def download(job_id: str, ticket: str = Query(..., max_length=200),
                   part: str = Query("html", pattern="^(html|media)$")):
    def locate(store):
        path = store.download_path(job_id, ticket, part)
        return path, store.get(job_id), path.stat().st_size
    try:
        path, job, size = await storage_call(locate)
    except ValueError as error:
        raise HTTPException(403, str(error)) from None
    html_file = path.suffix == ".html"
    # 附件包下载名与 HTML 引用的文件夹名一致（“<标题>-附件”），解压到同一目录即可查看。
    suffix = ".html" if html_file else MEDIA_SUFFIX if part == "media" else ".zip"
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               # 以附件下载；即使被浏览器直接打开也处于沙箱，不能访问后台页面的登录状态。
               "Content-Security-Policy": "sandbox", "Content-Length": str(size),
               "Content-Disposition": attachment_header(
                   download_name(job["request"], job["created"], suffix))}
    # 完整发送后标记为已下载，服务器端再保留 DOWNLOADED_HOURS 供重新下载，之后由定时清理删除。
    sent = {}
    return StreamingResponse(one_time_file(path, sent),
                             media_type="text/html; charset=utf-8" if html_file else "application/zip",
                             headers=headers, background=BackgroundTask(consume_after_download, job_id, sent))


@router.get("/assets/admin.js", include_in_schema=False)
def admin_script():
    # no-cache：每次按 ETag 回源校验，改版后刷新即生效，不会被浏览器启发式缓存卡在旧脚本。
    return FileResponse(Path(__file__).parent / "web/admin.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
