"""公开页面、用户身份、RAG 查询和通用更新路由。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ics_agent.app.core.context import settings
from ics_agent.app.core.auth import AdminPrincipal, require_admin
from ics_agent.app.core.config import PROJECT_ROOT
from ics_agent.app.services.knowledge_base_update import (
    KnowledgeBaseUpdateInProgress,
    get_knowledge_base_update_manager,
)
from ics_agent.app.core.logging_config import log_event
from ics_agent.app.core.models import RAGQueryRequest
from ics_agent.app.services.rag import _run_rag_query
from ics_agent.faq.service import FAQUpdateInProgress, get_faq_service

logger = logging.getLogger(__name__)
router = APIRouter()
SUPPORT_CHAT_PAGE_PATH = PROJECT_ROOT / "support-chat.html"


@router.get("/health")
def health() -> dict[str, str]:
    log_event(logger, logging.DEBUG, "app.health")
    return {"status": "ok", "service": "ics_agent"}


@router.get("/chatwoot/profile")
def chatwoot_profile(
    request: Request,
    identifier: str = Query(..., min_length=1, max_length=256),
    name: str | None = Query(default=None, max_length=256),
    email: str | None = Query(default=None, max_length=320),
    phone_number: str | None = Query(default=None, max_length=64),
    avatar_url: str | None = Query(default=None, max_length=2048),
) -> JSONResponse:
    """为 Website Widget 生成稳定的 Chatwoot 用户身份。

    生产环境应由业务后端根据登录态返回 identifier，而不是信任 URL
    参数。当前接口保留查询参数是为了兼容 APP WebView 和 file:// 本地联调。
    """
    if not settings.chatwoot_profile_allow_query_identifiers:
        raise HTTPException(
            status_code=403,
            detail="Query-based Chatwoot profile lookup is disabled.",
        )

    clean_identifier = identifier.strip()
    hmac_token = settings.chatwoot_hmac_token.strip()
    if not hmac_token:
        raise HTTPException(
            status_code=503,
            detail="Chatwoot HMAC token is not configured.",
        )

    identifier_hash = hmac.new(
        hmac_token.encode("utf-8"),
        clean_identifier.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    profile: dict[str, object] = {
        "identifier": clean_identifier,
        "identifier_hash": identifier_hash,
        "name": name.strip() if name and name.strip() else None,
        "email": email.strip() if email and email.strip() else None,
        "phone_number": (
            phone_number.strip()
            if phone_number and phone_number.strip()
            else None
        ),
        "avatar_url": (
            avatar_url.strip()
            if avatar_url and avatar_url.strip()
            else None
        ),
        "custom_attributes": {
            "app_user_id": clean_identifier,
        },
    }
    profile = {
        key: value
        for key, value in profile.items()
        if value is not None
    }
    log_event(
        logger,
        logging.INFO,
        "chatwoot.profile.generated",
        identifier=clean_identifier,
        origin=request.headers.get("origin"),
    )
    return JSONResponse(status_code=200, content=profile)


@router.get("/support-chat", include_in_schema=False)
@router.get("/support-chat.html", include_in_schema=False)
def support_chat_page() -> FileResponse:
    """提供可远端访问的 Website Widget 客服承载页。"""
    if not SUPPORT_CHAT_PAGE_PATH.is_file():
        raise HTTPException(status_code=404, detail="Support chat page is unavailable.")
    return FileResponse(SUPPORT_CHAT_PAGE_PATH, media_type="text/html")


@router.get("/support-chat/config", include_in_schema=False)
def support_chat_config(request: Request) -> JSONResponse:
    """返回浏览器侧客服页所需的公网 Chatwoot 配置。"""
    public_base_url = settings.chatwoot_public_base_url.strip()
    if not public_base_url:
        public_base_url = str(request.base_url).rstrip("/")
    return JSONResponse(
        status_code=200,
        content={
            "chatwoot_base_url": public_base_url,
            "profile_endpoint": "/chatwoot/profile",
        },
    )


@router.post("/rag/query")
async def rag_query(payload: RAGQueryRequest) -> JSONResponse:
    """通过常驻 FastAPI 进程执行一轮 RAG MVP，便于本地验证。"""
    try:
        result = await _run_rag_query(
            payload.query,
            thread_id=payload.thread_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="RAG service is temporarily unavailable.",
        ) from exc

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "query": payload.query.strip(),
            **result,
        },
    )


@router.post("/faq/update")
async def update_faq(
    file: UploadFile = File(...),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """提交 FAQ 工作簿，执行非阻塞后台替换。"""
    filename = file.filename or "faq-upload.xlsx"
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(
            status_code=400,
            detail="FAQ workbook must be an .xlsx file.",
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="faq-upload-",
            suffix=".xlsx",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            while chunk := await file.read(1024 * 1024):
                await asyncio.to_thread(temporary_file.write, chunk)

        status = await get_faq_service().submit_excel_refresh(temporary_path)
    except FAQUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=应用.FAQ更新.提交失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="FAQ update could not be submitted.",
        ) from exc
    finally:
        await file.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "事件=应用.FAQ更新.临时文件清理失败 路径=%s",
                    temporary_path,
                )

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "job_id": status.job_id,
            "source_hash": status.source_hash,
            "status": status.status,
        },
    )


@router.get("/faq/update/{job_id}")
async def faq_update_status(
    job_id: str,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回已提交 FAQ 工作簿更新任务的状态。"""
    status = get_faq_service().get_update_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="FAQ update job not found.")
    return JSONResponse(
        status_code=200,
        content={
            "job_id": job_id,
            "status": status,
        },
    )


@router.post("/knowledge-base/update")
async def update_knowledge_base(
    force: bool = False,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """提交知识库全量解析和 ChromaDB 重建任务。"""
    try:
        status = await get_knowledge_base_update_manager().submit_full_update(
            force=force,
        )
    except KnowledgeBaseUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "事件=应用.知识库更新.提交失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Knowledge-base update could not be submitted.",
        ) from exc

    response_status = 200 if status.status == "already_applied" else 202
    return JSONResponse(
        status_code=response_status,
        content={
            "accepted": status.status != "already_applied",
            **status.to_dict(),
        },
    )


@router.get("/knowledge-base/update/{job_id}")
async def knowledge_base_update_status(
    job_id: str,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回知识库重建任务的状态和结果。"""
    status = get_knowledge_base_update_manager().get_status(job_id)
    if status is None:
        raise HTTPException(
            status_code=404,
            detail="Knowledge-base update job not found.",
        )
    return JSONResponse(
        status_code=200,
        content=status.to_dict(),
    )
