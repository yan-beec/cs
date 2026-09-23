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
from .chatwoot_client import (
    ChatwootAPIError,
    get_chatwoot_client,
)
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
from .webhook import is_emoji_only_text, parse_event, verify_signature
from .webhook_idempotency import (
    get_webhook_idempotency_repository,
    payload_hash,
)
from .webhook_queue import (
    WebhookJob,
    WebhookJobQueue,
    WebhookQueueFull,
)
from ics_agent.faq.service import (
    FAQUpdateInProgress,
    get_faq_service,
)
from ics_agent.faq.config import get_faq_settings
from ics_agent.faq.repositories import ensure_faq_table
from ics_agent.rag.config import get_rag_settings
from ics_agent.rag.graph import get_rag_graph
from ics_agent.rag.main import run_query
from ics_agent.rag.memory import get_conversation_memory_manager

settings = get_settings()
rag_settings = get_rag_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
webhook_repository = get_webhook_idempotency_repository()
webhook_queue: WebhookJobQueue | None = None
EMOJI_ACK_MESSAGE = "如果有什么需要帮助，可以直接问我哦，我会尽力帮您解答的~"
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
    global webhook_queue
    memory_manager = get_conversation_memory_manager()
    try:
        await memory_manager.startup()
        await memory_manager.start_cleanup_task()
    except Exception as exc:
        # PostgreSQL 不可用时保留无记忆的 RAG 降级能力，服务仍可启动。
        logger.exception(
            "事件=应用.RAG对话记忆.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    try:
        await asyncio.to_thread(webhook_repository.ensure_table)
        log_event(logger, logging.INFO, "app.webhook_idempotency.ready")
    except Exception as exc:
        logger.exception(
            "事件=应用.Webhook幂等.初始化失败 错误类型=%s",
            type(exc).__name__,
        )

    webhook_queue = WebhookJobQueue(
        processor=_process_webhook_job,
        repository=webhook_repository,
    )
    await webhook_queue.start()

    try:
        await asyncio.to_thread(ensure_admin_user, settings)
        log_event(logger, logging.INFO, "app.admin_auth.ready")
    except Exception as exc:
        logger.exception(
            "事件=应用.管理员认证.初始化失败 错误类型=%s",
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
            "事件=应用.FAQ数据表.初始化失败 错误类型=%s",
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
            await asyncio.to_thread(
                get_rag_graph,
                warm_up=True,
                checkpointer=memory_manager.checkpointer,
            )
        except Exception as exc:
            logger.exception(
                "事件=应用.RAG.预热.失败 错误类型=%s",
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
        if webhook_queue is not None:
            await webhook_queue.stop()
            webhook_queue = None
        await memory_manager.shutdown()
        log_event(logger, logging.INFO, "app.lifecycle.stopped")


app = FastAPI(
    title="ics_agent",
    version="0.1.0",
    description="Chatwoot webhook receiver and intelligent customer-service agent.",
    lifespan=lifespan,
)

ADMIN_PAGE_PATH = Path(__file__).with_name("admin.html")


async def _run_rag_query(
    query: str,
    *,
    thread_id: str | None = None,
) -> dict[str, object]:
    """执行 FAQ-first 查询编排，并统一记录请求耗时和异常。"""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")

    started_at = time.perf_counter()
    try:
        result = await run_query(normalized_query, thread_id=thread_id)
    except Exception as exc:
        logger.exception(
            "事件=应用.查询.失败 错误类型=%s",
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


def _webhook_thread_id(event: ChatwootWebhookEvent) -> str | None:
    """根据 Chatwoot 会话生成稳定的 LangGraph thread_id。"""
    conversation = event.conversation or {}
    account = event.account or {}
    conversation_id = conversation.get("id")
    account_id = account.get("id") or conversation.get("account_id")
    if conversation_id is not None:
        return f"chatwoot:{account_id or 'default'}:{conversation_id}"

    # 极少数事件没有 conversation 对象时，尽量使用 sender 维持短期连续性。
    sender_id = (event.sender or {}).get("id")
    if sender_id is not None:
        return f"chatwoot:sender:{sender_id}"
    if event.id is not None:
        return f"chatwoot:event:{event.id}"
    return None


def _webhook_event_key(
    event: ChatwootWebhookEvent,
    raw_body: bytes,
) -> str:
    """优先使用 Chatwoot 事件 ID，没有 ID 时使用请求体哈希。"""
    account = event.account or {}
    account_id = account.get("id") or "default"
    if event.id is not None:
        return f"chatwoot:{account_id}:{event.id}"
    return f"chatwoot:payload:{payload_hash(raw_body)}"


async def _process_webhook_job(job: WebhookJob) -> dict[str, object]:
    """后台执行一条入站消息；结果由队列写入幂等记录。"""
    event = job.event
    content = event.content or ""
    if is_emoji_only_text(content):
        log_event(
            logger,
            logging.INFO,
            "webhook.emoji_only",
            event_key=job.event_key,
            content_length=len(content),
        )
        result: dict[str, object] = {
            "route": "emoji_ack",
            "faq_route": "emoji_ack",
            "answer": EMOJI_ACK_MESSAGE,
            "intent_label": None,
            "intent_confidence": None,
        }
    else:
        result = await _run_rag_query(
            content,
            thread_id=_webhook_thread_id(event),
        )
    reply_result: dict[str, object] = {
        "enabled": settings.chatwoot_auto_reply_enabled,
        "sent": False,
    }
    if settings.chatwoot_auto_reply_enabled:
        conversation = event.conversation or {}
        account = event.account or {}
        account_id = str(
            account.get("id")
            or conversation.get("account_id")
            or settings.chatwoot_account_id
            or ""
        )
        conversation_id = str(conversation.get("id") or "")
        try:
            sent_message = await asyncio.to_thread(
                get_chatwoot_client().send_message,
                account_id=account_id,
                conversation_id=conversation_id,
                content=str(result.get("answer") or ""),
            )
        except ChatwootAPIError as exc:
            log_event(
                logger,
                logging.WARNING,
                "webhook.chatwoot_reply.failed",
                event_key=job.event_key,
                status_code=exc.status_code,
                error=str(exc),
            )
            raise
        reply_result = {
            "enabled": True,
            "sent": True,
            "message_id": sent_message.get("id"),
        }
    log_event(
        logger,
        logging.INFO,
        "webhook.job.answer_ready",
        event_key=job.event_key,
        route=result["route"],
    )
    return {
        "route": result["route"],
        "faq_route": result["faq_route"],
        "answer": result["answer"],
        "intent_label": result["intent_label"],
        "intent_confidence": result["intent_confidence"],
        "chatwoot_reply": reply_result,
    }


@app.get("/health")
def health() -> dict[str, str]:
    log_event(logger, logging.DEBUG, "app.health")
    return {"status": "ok", "service": "ics_agent"}


@app.get("/admin/webhook-jobs/{event_key}")
async def webhook_job_status(
    event_key: str,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """查看后台 Webhook 任务的幂等状态和最近一次结果。"""
    status = await asyncio.to_thread(webhook_repository.get, event_key)
    if status is None:
        raise HTTPException(status_code=404, detail="Webhook job not found.")
    return JSONResponse(status_code=200, content=status)


@app.delete("/admin/memory")
async def delete_memory(
    thread_id: str = Query(..., min_length=1, max_length=256),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """删除一个会话的全部 PostgreSQL 对话记忆。"""
    try:
        deleted = await get_conversation_memory_manager().delete_thread(
            thread_id
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


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """提供基于浏览器的管理后台页面。"""
    return FileResponse(ADMIN_PAGE_PATH, media_type="text/html")


@app.post("/auth/login")
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


@app.get("/admin/api/files/knowledge-base")
async def admin_list_knowledge_files(
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """列出配置的知识库目录下的所有文件。"""
    try:
        items = await asyncio.to_thread(list_files, knowledge_base_root())
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


@app.get("/admin/api/files/knowledge-base/content")
async def admin_preview_knowledge_file(
    path: str = Query(..., min_length=1, max_length=1024),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """返回一个知识库文件的文本内容或预览元数据。"""
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
    """以流式方式返回 PDF/图片知识库文件，供已认证用户预览。"""
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
    """下载一个知识库文件。"""
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
    """将一个或多个文件上传到安全的知识库子目录。"""
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


@app.post("/admin/api/files/knowledge-base/delete")
async def admin_delete_knowledge_file(
    payload: AdminFileDeleteRequest,
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """删除配置的知识库目录下的一个文件。"""
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
    """返回配置的 FAQ 工作簿状态。"""
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
    """返回 FAQ 工作簿的工作表和行，供在线浏览。"""
    try:
        result = await asyncio.to_thread(faq_workbook_preview)
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


@app.get("/admin/api/files/faq/download")
async def admin_download_faq_file(
    _: AdminPrincipal = Depends(require_admin),
) -> FileResponse:
    """下载配置的 FAQ 工作簿。"""
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
    """原子替换配置的 FAQ 工作簿。"""
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
            "事件=管理后台.FAQ.文件.上传失败 错误类型=%s",
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
    """返回游标之后的缓冲日志，供管理后台实时日志视图使用。"""
    return JSONResponse(
        status_code=200,
        content=get_log_snapshot(after_id=after_id, limit=limit),
    )


@app.post("/admin/api/update/knowledge-base")
async def admin_update_knowledge_base(
    confirm: bool = Query(default=False),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """提交知识库全量解析和向量库重建任务。"""
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


@app.post("/admin/api/update/faq")
async def admin_update_faq(
    confirm: bool = Query(default=False),
    _: AdminPrincipal = Depends(require_admin),
) -> JSONResponse:
    """提交基于配置 FAQ 工作簿的后台刷新任务。"""
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


@app.post("/rag/query")
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


@app.post("/faq/update")
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


@app.get("/faq/update/{job_id}")
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


@app.post("/knowledge-base/update")
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


@app.get("/knowledge-base/update/{job_id}")
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


@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(
    request: Request,
    x_chatwoot_signature: str | None = Header(
        default=None,
        alias="X-Chatwoot-Signature",
    ),
    x_chatwoot_timestamp: str | None = Header(
        default=None,
        alias="X-Chatwoot-Timestamp",
    ),
) -> JSONResponse:
    raw_body = await request.body()

    if not verify_signature(
        raw_body=raw_body,
        signature=x_chatwoot_signature,
        timestamp=x_chatwoot_timestamp,
        secret=settings.chatwoot_webhook_secret,
        tolerance_seconds=settings.chatwoot_webhook_tolerance_seconds,
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

    event_key = _webhook_event_key(event, raw_body)
    try:
        claim = await asyncio.to_thread(
            webhook_repository.claim,
            event_key=event_key,
            event_id=None if event.id is None else str(event.id),
            event_type=event.event,
            body_hash=payload_hash(raw_body),
            stale_after_seconds=rag_settings.webhook_idempotency_stale_after_seconds,
        )
    except Exception as exc:
        logger.exception(
            "事件=Webhook幂等登记失败 错误类型=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook idempotency storage is temporarily unavailable.",
        ) from exc

    if not claim.accepted:
        log_event(
            logger,
            logging.INFO,
            "webhook.duplicate",
            event_key=event_key,
            status=claim.status,
        )
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "duplicate": True,
                "status": claim.status,
                "event": event.event,
                "event_id": event.id,
                "event_key": event_key,
            },
        )

    if webhook_queue is None:
        await asyncio.to_thread(
            webhook_repository.mark_failed,
            event_key,
            "Webhook queue is not started.",
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook queue is temporarily unavailable.",
        )

    try:
        webhook_queue.enqueue_nowait(
            WebhookJob(event_key=event_key, event=event)
        )
    except WebhookQueueFull as exc:
        await asyncio.to_thread(
            webhook_repository.mark_failed,
            event_key,
            str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook queue is temporarily full.",
        ) from exc

    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "duplicate": False,
            "status": "queued",
            "event": event.event,
            "event_id": event.id,
            "event_key": event_key,
        },
    )
