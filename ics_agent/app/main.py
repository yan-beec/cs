# 模块职责：创建 FastAPI 应用，并承载健康检查和 Webhook 接收入口。

import asyncio
import logging
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse

from .auth import (
    AdminAuthConfigurationError,
    AdminPrincipal,
    InvalidAdminCredentials,
    authenticate_admin,
    ensure_admin_user,
    require_admin,
)
from .admin_files import (
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
    save_upload,
)
from .config import get_settings
from .logging_config import configure_logging, get_log_snapshot, log_event
from .models import (
    AdminFileDeleteRequest,
    AdminLoginRequest,
    ChatwootWebhookEvent,
    RAGQueryRequest,
)
from .knowledge_base_update import (
    KnowledgeBaseUpdateInProgress,
    get_knowledge_base_update_manager,
)
from .webhook import parse_event, verify_signature
from ics_agent.faq.service import (
    FAQUpdateInProgress,
    get_faq_service,
)
from ics_agent.faq.config import get_faq_settings
from ics_agent.faq.repositories import ensure_faq_table
from ics_agent.rag.config import get_rag_settings
from ics_agent.rag.graph import get_rag_graph
from ics_agent.rag.main import run_query

settings = get_settings()
rag_settings = get_rag_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
log_event(
    logger,
    logging.INFO,
    "app.startup",
    environment=settings.app_env,
    log_level=settings.log_level,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """在应用启动时预热本地 RAG 模型，并在关闭时记录生命周期事件。"""
    try:
        await asyncio.to_thread(ensure_admin_user, settings)
        log_event(logger, logging.INFO, "app.admin_auth.ready")
    except Exception as exc:
        logger.exception(
            "event=app.admin_auth.init_failed error_type=%s",
            type(exc).__name__,
        )

    try:
        faq_table_created = await asyncio.to_thread(ensure_faq_table)
        log_event(
            logger,
            logging.INFO,
            "app.faq_table.ready",
            created=faq_table_created,
        )
    except Exception as exc:
        # PostgreSQL 不可用时，FAQ 查询仍会降级到 RAG。
        logger.exception(
            "event=app.faq_table.init_failed error_type=%s",
            type(exc).__name__,
        )

    if rag_settings.rag_warm_up_on_startup:
        started_at = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "app.rag_warmup.started",
        )
        try:
            # 模型加载是同步 CPU/GPU 工作，放入线程避免阻塞事件循环。
            await asyncio.to_thread(get_rag_graph, warm_up=True)
        except Exception as exc:
            logger.exception(
                "event=app.rag_warmup.failed error_type=%s",
                type(exc).__name__,
            )
            raise
        log_event(
            logger,
            logging.INFO,
            "app.rag_warmup.completed",
            elapsed_seconds=round(
                time.perf_counter() - started_at,
                3,
            ),
        )
    else:
        log_event(
            logger,
            logging.WARNING,
            "app.rag_warmup.skipped",
            reason="disabled_by_config",
        )

    log_event(logger, logging.INFO, "app.lifecycle.started")
    try:
        yield
    finally:
        log_event(logger, logging.INFO, "app.lifecycle.stopped")


app = FastAPI(
    title="ics_agent",
    version="0.1.0",
    description="Chatwoot webhook receiver and intelligent customer-service agent.",
    lifespan=lifespan,
)

ADMIN_PAGE_PATH = Path(__file__).with_name("admin.html")


async def _run_rag_query(query: str) -> dict[str, object]:
    """执行 FAQ-first 查询编排，并统一记录请求耗时和异常。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")

    started_at = time.perf_counter()
    try:
        result = await run_query(normalized_query)
    except Exception as exc:
        logger.exception(
            "event=app.query.failed error_type=%s",
            type(exc).__name__,
        )
        raise

    log_event(
        logger,
        logging.INFO,
        "app.query.completed",
        query_length=len(normalized_query),
        route=result["route"],
        faq_route=result["faq_route"],
        intent_label=result["intent_label"],
        retrieved_count=result["retrieved_count"],
        reranked_count=result["reranked_count"],
        elapsed_seconds=round(time.perf_counter() - started_at, 3),
    )
    return result


def _should_process_webhook_event(event: ChatwootWebhookEvent) -> bool:
    """仅处理带文本的入站消息，避免回复消息触发重复 RAG。"""
    if not event.content or not event.content.strip():
        return False
    if event.message_type is None:
        return True
    return event.message_type.lower() == "incoming"


@app.get("/health")
def health() -> dict[str, str]:
    log_event(logger, logging.DEBUG, "app.health")
    return {"status": "ok", "service": "ics_agent"}


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """Serve the browser-based administrator console shell."""
    return FileResponse(ADMIN_PAGE_PATH, media_type="text/html")


@app.post("/auth/login")
async def admin_login(payload: AdminLoginRequest) -> JSONResponse:
    """Authenticate the singleton system administrator and issue a JWT."""
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
            "event=app.admin_login.failed error_type=%s",
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


@app.get("/admin/api/files/knowledge-base")
async def admin_list_knowledge_files(
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """List all files below the configured knowledge-base directory."""
    try:
        items = await asyncio.to_thread(list_files, knowledge_base_root())
    except Exception as exc:
        logger.exception(
            "event=admin.knowledge_base.files.list_failed error_type=%s",
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


@app.get("/admin/api/files/knowledge-base/content")
async def admin_preview_knowledge_file(
    path: str = Query(..., min_length=1, max_length=1024),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Return text content or preview metadata for one knowledge-base file."""
    try:
        result = await asyncio.to_thread(
            knowledge_file_preview,
            knowledge_base_root(),
            path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=200, content=result)


@app.get("/admin/api/files/knowledge-base/preview")
async def admin_stream_knowledge_preview(
    path: str = Query(..., min_length=1, max_length=1024),
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """Stream a PDF/image knowledge-base file for authenticated preview."""
    try:
        file_path = await asyncio.to_thread(
            safe_child_path,
            knowledge_base_root(),
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
        headers={
            "Content-Disposition": (
                f'inline; filename="{file_path.name}"'
            )
        },
    )


@app.get("/admin/api/files/knowledge-base/download")
async def admin_download_knowledge_file(
    path: str = Query(..., min_length=1, max_length=1024),
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """Download one knowledge-base file."""
    try:
        file_path = await asyncio.to_thread(
            safe_child_path,
            knowledge_base_root(),
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


@app.post("/admin/api/files/knowledge-base/upload")
async def admin_upload_knowledge_files(
    files: list[UploadFile] = File(...),
    relative_dir: str = Form(default=""),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Upload one or more files into a safe knowledge-base subdirectory."""
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件。")

    root = knowledge_base_root()
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
            "event=admin.knowledge_base.files.upload_failed error_type=%s",
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


@app.post("/admin/api/files/knowledge-base/delete")
async def admin_delete_knowledge_file(
    payload: AdminFileDeleteRequest,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Delete one file below the configured knowledge-base directory."""
    try:
        result = await asyncio.to_thread(
            delete_file,
            knowledge_base_root(),
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


@app.get("/admin/api/files/faq")
async def admin_faq_file(
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Return the state of the configured FAQ workbook."""
    try:
        result = await asyncio.to_thread(faq_file_info)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="FAQ 文件状态暂时无法读取。",
        ) from exc
    return JSONResponse(status_code=200, content=result)


@app.get("/admin/api/files/faq/content")
async def admin_preview_faq_file(
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Return FAQ workbook sheets and rows for online browsing."""
    try:
        result = await asyncio.to_thread(faq_workbook_preview)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="FAQ 文件不存在。") from exc
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "event=admin.faq.file.preview_failed error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="FAQ 文件无法预览。") from exc
    return JSONResponse(status_code=200, content=result)


@app.get("/admin/api/files/faq/download")
async def admin_download_faq_file(
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """Download the configured FAQ workbook."""
    try:
        file_path = faq_file_path()
    except AdminFileOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="FAQ 文件不存在。")
    return FileResponse(
        file_path,
        media_type=file_media_type(file_path),
        filename=file_path.name,
    )


@app.post("/admin/api/files/faq/upload")
async def admin_upload_faq_file(
    file: UploadFile = File(...),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Atomically replace the configured FAQ workbook."""
    destination = faq_file_path()
    filename = Path(file.filename or "").name
    if filename != destination.name:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail=f"上传文件名必须是 {destination.name}。",
        )
    try:
        result = await asyncio.to_thread(save_upload, file, destination)
    except (AdminFileOperationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "event=admin.faq.file.upload_failed error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="FAQ 文件上传失败。") from exc
    finally:
        await file.close()
    log_event(logger, logging.INFO, "admin.faq.file.replaced")
    return JSONResponse(status_code=200, content=result)


@app.get("/admin/api/logs")
async def admin_logs(
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Return buffered logs after a cursor for the live admin log view."""
    return JSONResponse(
        status_code=200,
        content=get_log_snapshot(after_id=after_id, limit=limit),
    )


@app.post("/admin/api/update/knowledge-base")
async def admin_update_knowledge_base(
    confirm: bool = Query(default=False),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Submit a full knowledge-base parse and vector-store rebuild."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="知识库更新会全量覆盖当前向量库，请确认文件已备份后再执行。",
        )
    try:
        status = await get_knowledge_base_update_manager().submit_full_update()
    except KnowledgeBaseUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "event=admin.knowledge_base.update.submit_failed error_type=%s",
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


@app.post("/admin/api/update/faq")
async def admin_update_faq(
    confirm: bool = Query(default=False),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Submit a background refresh from the configured FAQ workbook."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="FAQ 更新会全量覆盖当前 FAQ 数据，请确认 Excel 文件已备份后再执行。",
        )
    try:
        status = await get_faq_service().submit_excel_refresh(
            get_faq_settings().faq_source_xlsx_path
        )
    except FAQUpdateInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "event=admin.faq.update.submit_failed error_type=%s",
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


@app.post("/rag/query")
async def rag_query(payload: RAGQueryRequest) -> JSONResponse:
    """通过常驻 FastAPI 进程执行一轮 RAG MVP，便于本地验证。"""
    try:
        result = await _run_rag_query(payload.query)
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


@app.post("/faq/update")
async def update_faq(
    file: UploadFile = File(...),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Submit an FAQ workbook for a non-blocking background replacement."""
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
            "event=app.faq_update.submit_failed error_type=%s",
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
                    "event=app.faq_update.tempfile_cleanup_failed path=%s",
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


@app.get("/faq/update/{job_id}")
async def faq_update_status(
    job_id: str,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Return the status of a previously submitted FAQ workbook update."""
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


@app.post("/knowledge-base/update")
async def update_knowledge_base(
    force: bool = False,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Submit a full knowledge-base parse and ChromaDB rebuild."""
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
            "event=app.knowledge_base_update.submit_failed error_type=%s",
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


@app.get("/knowledge-base/update/{job_id}")
async def knowledge_base_update_status(
    job_id: str,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """Return the state and result of a knowledge-base rebuild task."""
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


@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(
    request: Request,
    x_chatwoot_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw_body = await request.body()

    if not verify_signature(
        raw_body,
        x_chatwoot_signature,
        settings.chatwoot_webhook_secret,
    ):
        log_event(logger, logging.WARNING, "webhook.signature.invalid")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        event = parse_event(raw_body)
    except ValueError as exc:
        log_event(
            logger,
            logging.WARNING,
            "webhook.payload.invalid",
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    log_event(
        logger,
        logging.INFO,
        "webhook.received",
        webhook_event=event.event,
        event_id=event.id,
        message_type=event.message_type,
    )

    if not _should_process_webhook_event(event):
        log_event(
            logger,
            logging.DEBUG,
            "webhook.rag.skipped",
            reason="not_incoming_text_message",
        )
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "event": event.event,
                "event_id": event.id,
                "rag_processed": False,
            },
        )

    try:
        result = await _run_rag_query(event.content or "")
    except Exception:
        # Webhook 已完成签名校验和事件接收；RAG 失败不泄露内部异常。
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "event": event.event,
                "event_id": event.id,
                "rag_processed": True,
                "rag_success": False,
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "accepted": True,
            "event": event.event,
            "event_id": event.id,
            "query_processed": True,
            "rag_processed": result["route"] == "rag",
            "rag_success": True,
            "route": result["route"],
            "faq_route": result["faq_route"],
            "faq_matched": result["faq_matched"],
            "answer": result["answer"],
            "question_id": result["question_id"],
            "match_degree": result["match_degree"],
            "intent_label": result["intent_label"],
            "intent_confidence": result["intent_confidence"],
            "retrieved_count": result["retrieved_count"],
            "reranked_count": result["reranked_count"],
        },
    )
