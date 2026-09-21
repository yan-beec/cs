# 模块职责：创建 FastAPI 应用，并承载健康检查和 Webhook 接收入口。

import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .logging_config import configure_logging, log_event
from .webhook import parse_event, verify_signature

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
log_event(
    logger,
    logging.INFO,
    "app.startup",
    environment=settings.app_env,
    log_level=settings.log_level,
)

app = FastAPI(
    title="ics_agent",
    version="0.1.0",
    description="Chatwoot webhook receiver and intelligent customer-service agent.",
)


@app.get("/health")
def health() -> dict[str, str]:
    log_event(logger, logging.DEBUG, "app.health")
    return {"status": "ok", "service": "ics_agent"}


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

    # The first version only acknowledges and logs events.
    # BM25 retrieval and automatic replies will be added after event validation.
    return JSONResponse(
        status_code=202,
        content={"accepted": True, "event": event.event, "event_id": event.id},
    )
