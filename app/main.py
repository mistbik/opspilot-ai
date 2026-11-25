import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import create_tables, engine
from app.ingestion import insert_event, parse_csv, parse_json, process_records
from app.investigation_api import router as investigation_router
from app.schemas import WebhookEvent
from app.security import verify_signature

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(create_tables)
    yield


app = FastAPI(title="Data Ingestion Service", lifespan=lifespan)
app.include_router(investigation_router)


@app.middleware("http")
async def correlate_request(request: Request, call_next):
    correlation_id = str(uuid4())
    with structlog.contextvars.bound_contextvars(correlation_id=correlation_id):
        try:
            response = await call_next(request)
        except Exception:
            log.error("request_failed")
            response = JSONResponse({"detail": "Internal server error"}, status_code=500)
        response.headers["X-Correlation-ID"] = correlation_id
        log.info("request_completed", status_code=response.status_code)
        return response


@app.get("/")
def home():
    return {"message": "Data Ingestion Service is running"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/db-health")
def database_health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        log.error("database_health_check_failed")
        raise HTTPException(status_code=503, detail="Database connection failed") from None
    return {"database": "connected"}


def persist_webhook(event: WebhookEvent) -> bool:
    with Session(engine) as session:
        try:
            inserted = insert_event(session, event)
            session.commit()
        except Exception:
            session.rollback()
            raise
    return inserted


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw_body = await request.body()
    if not verify_signature(raw_body, request.headers.get("X-Signature")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        event = WebhookEvent.model_validate_json(raw_body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid event payload") from None

    try:
        inserted = await run_in_threadpool(persist_webhook, event)
    except Exception:
        log.error("webhook_persistence_failed")
        raise HTTPException(status_code=500, detail="Event processing failed") from None

    status = "processed" if inserted else "already_processed"
    log.info("webhook_processed", inserted=inserted)
    return {"status": status, "event_id": event.event_id}


@app.post("/upload")
def upload_file(file: UploadFile = File(...)):
    filename = file.filename or ""
    extension = filename.lower()
    if not extension.endswith((".csv", ".json")):
        raise HTTPException(status_code=400, detail="Only CSV and JSON files are supported")

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        records = parse_csv(content) if extension.endswith(".csv") else parse_json(content)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Could not parse uploaded file") from None

    try:
        with Session(engine) as session:
            result = process_records(records, session)
    except Exception:
        log.error("upload_persistence_failed")
        raise HTTPException(status_code=500, detail="File ingestion failed") from None

    log.info("upload_processed", **result)
    return {"filename": filename, **result}
