"""Raw Chatwoot intake; no bot replies, model calls, or in-memory-only jobs."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolClosed, PoolTimeout
from starlette.concurrency import run_in_threadpool

from ics_agent.app.chatwoot.payload import verify_signature

from .config import CaptureSettings
from .envelope import CaptureEnvelope
from .repository import CaptureRepository

logger = logging.getLogger(__name__)
DATABASE_UNAVAILABLE = (DatabaseError, PoolTimeout, PoolClosed)


def create_app(settings: CaptureSettings | None = None, repository=None) -> FastAPI:
    settings = settings or CaptureSettings()
    repository = repository if repository is not None else CaptureRepository(settings)

    @asynccontextmanager
    async def lifespan(_):
        await run_in_threadpool(repository.open)
        try:
            yield
        finally:
            await run_in_threadpool(repository.close)

    app = FastAPI(title="ICS conversation intake", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        try:
            await run_in_threadpool(repository.ping)
        except DATABASE_UNAVAILABLE:
            raise HTTPException(status_code=503, detail="Archive database unavailable") from None
        return {"status": "ready"}

    @app.post("/quality/webhooks/chatwoot")
    async def receive(request: Request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not (content_type.startswith("application/") and content_type.endswith("+json")):
            raise HTTPException(status_code=415, detail="Expected a JSON webhook")
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise HTTPException(status_code=400, detail="Invalid content length")
            if len(length.lstrip("0")) > 10 or int(length) > settings.receiver_max_body_bytes:
                raise HTTPException(status_code=413, detail="Webhook body too large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.receiver_max_body_bytes:
                raise HTTPException(status_code=413, detail="Webhook body too large")
            body.extend(chunk)
        raw_body = bytes(body)
        signature = request.headers.get("X-Chatwoot-Signature", "")
        timestamp = request.headers.get("X-Chatwoot-Timestamp", "")
        if not signature.isascii() or not timestamp.isascii() or not verify_signature(
            raw_body=raw_body, signature=signature, timestamp=timestamp,
            secret=settings.webhook_secret.get_secret_value(), tolerance_seconds=settings.webhook_tolerance_seconds,
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        try:
            envelope = CaptureEnvelope.parse(raw_body)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(status_code=400, detail="Invalid Chatwoot event envelope") from None
        if envelope.account_id not in settings.account_ids:
            raise HTTPException(status_code=403, detail="Account is not configured for this receiver")
        try:
            receipt = await run_in_threadpool(repository.receive, envelope)
        except DATABASE_UNAVAILABLE as error:
            # PostgreSQL error details may contain the original row. Log only its class.
            logger.error("Archive event could not be committed: %s", type(error).__name__)
            return JSONResponse(status_code=503, content={"detail": "Archive event was not acknowledged"}, headers={"Retry-After": "3"})
        return JSONResponse(
            status_code=200 if receipt.duplicate else 202,
            content={"status": "accepted", "event_id": receipt.event_id, "duplicate": receipt.duplicate},
        )

    return app
