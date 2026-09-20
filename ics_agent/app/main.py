import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .webhook import parse_event, verify_signature

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ics_agent")

app = FastAPI(
    title="ics_agent",
    version="0.1.0",
    description="Chatwoot webhook receiver and intelligent customer-service agent.",
)


@app.get("/health")
def health() -> dict[str, str]:
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
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        event = parse_event(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    logger.info(
        "received Chatwoot event=%s event_id=%s message_type=%s",
        event.event,
        event.id,
        event.message_type,
    )

    # The first version only acknowledges and logs events.
    # BM25 retrieval and automatic replies will be added after event validation.
    return JSONResponse(
        status_code=202,
        content={"accepted": True, "event": event.event, "event_id": event.id},
    )
