"""管理后台页面、认证、文件、日志和后台更新路由。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from ics_agent.channels import channel_thread_id, list_channels
from ics_agent.channels.http import channel_query

from .files import (
    AdminFileOperationError,
    delete_file,
    faq_file_info,
    faq_file_path,
    faq_workbook_preview,
    file_media_type,
    knowledge_base_root,
    knowledge_file_preview,
    list_files,
    safe_child_path,
    save_faq_upload,
    save_upload,
)
from ics_agent.app.core.context import settings, webhook_repository
from ics_agent.app.core.auth import (
    AdminAuthConfigurationError,
    AdminPrincipal,
    InvalidAdminCredentials,
    authenticate_admin,
    require_admin,
)
from ics_agent.app.services.knowledge_base_update import (
    KnowledgeBaseUpdateInProgress,
    get_knowledge_base_update_manager,
)
from ics_agent.app.core.logging_config import get_log_snapshot, log_event
from ics_agent.app.core.models import AdminFileDeleteRequest, AdminLoginRequest
from ics_agent.faq.config import get_faq_settings
from ics_agent.faq.service import FAQUpdateInProgress, get_faq_service
from ics_agent.rag.memory import get_conversation_memory_manager

logger = logging.getLogger(__name__)
router = APIRouter()
ADMIN_PAGE_PATH = Path(__file__).resolve().parents[1] / "admin.html"


@router.get("/admin/webhook-jobs/{event_key}")
async def webhook_job_status(
    event_key: str,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """查看后台 Webhook 任务的幂等状态和最近一次结果。"""
    status = await asyncio.to_thread(webhook_repository.get, event_key)
    if status is None:
        raise HTTPException(status_code=404, detail="Webhook job not found.")
    return JSONResponse(status_code=200, content=status)


@router.delete("/admin/memory")
async def delete_memory(
    thread_id: str = Query(..., min_length=1, max_length=256),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """删除一个会话的全部 PostgreSQL 对话记忆。"""
    try:
        deleted = await get_conversation_memory_manager().delete_thread(
            channel_thread_id(thread_id, channel_key)
        )
    except Exception as exc:
        logger.exception(
            "事件=管理后台.对话记忆.删除失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Conversation memory storage is temporarily unavailable.",
        ) from exc
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Conversation memory is not initialized.",
        )
    return JSONResponse(
        status_code=200,
        content={"deleted": True, "thread_id": thread_id},
    )


@router.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """提供基于浏览器的管理后台页面。"""
    return FileResponse(ADMIN_PAGE_PATH, media_type="text/html")


@router.post("/auth/login")
async def admin_login(payload: AdminLoginRequest) -> JSONResponse:
    """认证单例系统管理员并签发 JWT。"""
    try:
        access_token = await asyncio.to_thread(
            authenticate_admin,
            payload.username,
            payload.password,
            settings=settings,
        )
    except InvalidAdminCredentials as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid administrator credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AdminAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=应用.管理员登录.失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Administrator authentication is temporarily unavailable.",
        ) from exc

    return JSONResponse(
        status_code=200,
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.admin_access_token_expire_seconds,
            "username": payload.username.strip(),
        },
    )


@router.get("/admin/api/channels")
async def admin_channels(
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回可管理的渠道及对应文件位置。"""
    return JSONResponse(content={"items": [
        {"key": channel.key, "name": channel.name,
         "faq_workbook": channel.faq_workbook, "knowledge_dir": channel.knowledge_dir}
        for channel in list_channels()
    ]})


@router.get("/admin/api/files/knowledge-base")
async def admin_list_knowledge_files(
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """列出配置的知识库目录下的所有文件。"""
    try:
        items = await asyncio.to_thread(list_files, knowledge_base_root(channel_key=channel_key))
    except Exception as exc:
        logger.exception(
            "事件=管理后台.知识库.文件.列表读取失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="知识库文件列表暂时无法读取。",
        ) from exc
    return JSONResponse(
        status_code=200,
        content={"items": items, "count": len(items)},
    )


@router.get("/admin/api/files/knowledge-base/content")
async def admin_preview_knowledge_file(
    path: str = Query(..., min_length=1, max_length=1024),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回一个知识库文件的文本内容或预览元数据。"""
    try:
        result = await asyncio.to_thread(
            knowledge_file_preview,
            knowledge_base_root(channel_key=channel_key),
            path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=200, content=result)


@router.get("/admin/api/files/knowledge-base/preview")
async def admin_stream_knowledge_preview(
    path: str = Query(..., min_length=1, max_length=1024),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """以流式方式返回 PDF/图片知识库文件，供已认证用户预览。"""
    try:
        file_path = await asyncio.to_thread(
            safe_child_path,
            knowledge_base_root(channel_key=channel_key),
            path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if file_path.suffix.lower() in {".md", ".txt", ".json", ".jsonl", ".csv"}:
        raise HTTPException(status_code=400, detail="文本文件请使用文本预览接口。")
    return FileResponse(
        file_path,
        media_type=file_media_type(file_path),
        filename=file_path.name,
        content_disposition_type="inline",
    )


@router.get("/admin/api/files/knowledge-base/download")
async def admin_download_knowledge_file(
    path: str = Query(..., min_length=1, max_length=1024),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """下载一个知识库文件。"""
    try:
        file_path = await asyncio.to_thread(
            safe_child_path,
            knowledge_base_root(channel_key=channel_key),
            path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="只能下载文件。")
    return FileResponse(
        file_path,
        media_type=file_media_type(file_path),
        filename=file_path.name,
    )


@router.post("/admin/api/files/knowledge-base/upload")
async def admin_upload_knowledge_files(
    files: list[UploadFile] = File(...),
    relative_dir: str = Form(default=""),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """将一个或多个文件上传到安全的知识库子目录。"""
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件。")

    root = knowledge_base_root(channel_key=channel_key)
    uploaded: list[dict[str, object]] = []
    try:
        for upload in files:
            filename = Path(upload.filename or "").name
            if not filename or filename in {".", ".."}:
                raise AdminFileOperationError("上传文件名无效。")
            clean_relative_dir = relative_dir.strip().strip("/\\")
            relative_path = (
                f"{clean_relative_dir}/{filename}"
                if clean_relative_dir
                else filename
            )
            destination = safe_child_path(
                root,
                relative_path,
                allow_missing=True,
            )
            uploaded.append(
                await asyncio.to_thread(save_upload, upload, destination)
            )
    except (AdminFileOperationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=管理后台.知识库.文件.上传失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="知识库文件上传失败。",
        ) from exc
    finally:
        for upload in files:
            await upload.close()

    log_event(
        logger,
        logging.INFO,
        "admin.knowledge_base.files.uploaded",
        count=len(uploaded),
    )
    return JSONResponse(status_code=201, content={"items": uploaded})


@router.post("/admin/api/files/knowledge-base/delete")
async def admin_delete_knowledge_file(
    payload: AdminFileDeleteRequest,
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """删除配置的知识库目录下的一个文件。"""
    try:
        result = await asyncio.to_thread(
            delete_file,
            knowledge_base_root(channel_key=channel_key),
            payload.relative_path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        logger,
        logging.INFO,
        "admin.knowledge_base.files.deleted",
        file=result["name"],
    )
    return JSONResponse(status_code=200, content=result)


@router.get("/admin/api/files/faq")
async def admin_faq_file(
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回配置的 FAQ 工作簿状态。"""
    try:
        result = await asyncio.to_thread(faq_file_info, channel_key=channel_key)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="FAQ 文件状态暂时无法读取。",
        ) from exc
    return JSONResponse(status_code=200, content=result)


@router.get("/admin/api/files/faq/content")
async def admin_preview_faq_file(
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回 FAQ 工作簿的工作表和行，供在线浏览。"""
    try:
        result = await asyncio.to_thread(faq_workbook_preview, channel_key=channel_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="FAQ 文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=管理后台.FAQ.文件.预览失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="FAQ 文件无法预览。") from exc
    return JSONResponse(status_code=200, content=result)


@router.get("/admin/api/files/faq/download")
async def admin_download_faq_file(
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """下载配置的 FAQ 工作簿。"""
    try:
        file_path = faq_file_path(channel_key=channel_key)
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="FAQ 文件不存在。")
    return FileResponse(
        file_path,
        media_type=file_media_type(file_path),
        filename=file_path.name,
    )


@router.post("/admin/api/files/faq/upload")
async def admin_upload_faq_file(
    file: UploadFile = File(...),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """校验上传的 Excel，并以渠道固定文件名原子替换 FAQ 工作簿。"""
    try:
        result = await asyncio.to_thread(save_faq_upload, file, channel_key=channel_key)
    except (AdminFileOperationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=管理后台.FAQ.文件.上传失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="FAQ 文件上传失败。") from exc
    finally:
        await file.close()
    log_event(logger, logging.INFO, "admin.faq.file.replaced")
    return JSONResponse(status_code=200, content=result)


@router.get("/admin/api/logs")
async def admin_logs(
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回游标之后的缓冲日志，供管理后台实时日志视图使用。"""
    return JSONResponse(
        status_code=200,
        content=get_log_snapshot(after_id=after_id, limit=limit),
    )


@router.post("/admin/api/update/knowledge-base")
async def admin_update_knowledge_base(
    confirm: bool = Query(default=False),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """提交知识库全量解析和向量库重建任务。"""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="知识库更新会全量覆盖当前向量库，请确认文件已备份后再执行。",
        )
    try:
        status = await get_knowledge_base_update_manager(channel_key).submit_full_update()
    except KnowledgeBaseUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=管理后台.知识库.更新.提交失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="知识库更新任务提交失败。",
        ) from exc
    response_status = 200 if status.status == "already_applied" else 202
    return JSONResponse(
        status_code=response_status,
        content={"accepted": status.status != "already_applied", **status.to_dict()},
    )


@router.post("/admin/api/update/faq")
async def admin_update_faq(
    confirm: bool = Query(default=False),
    channel_key: str = Depends(channel_query),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """提交基于配置 FAQ 工作簿的后台刷新任务。"""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="FAQ 更新会全量覆盖当前 FAQ 数据，请确认 Excel 文件已备份后再执行。",
        )
    try:
        status = await get_faq_service(channel_key).submit_excel_refresh(
            get_faq_settings(channel_key).faq_source_xlsx_path
        )
    except FAQUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=管理后台.FAQ.更新.提交失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="FAQ 更新任务提交失败。",
        ) from exc
    response_status = 200 if status.status == "already_applied" else 202
    return JSONResponse(
        status_code=response_status,
        content={
            "accepted": status.status != "already_applied",
            "job_id": status.job_id,
            "source_hash": status.source_hash,
            "status": status.status,
        },
    )
