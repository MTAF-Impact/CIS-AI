import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging_config import Timer, configure_logging

configure_logging(level=settings.LOG_LEVEL, json_format=settings.log_json)
logger = logging.getLogger("app.request")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(
        "%s starting up (env=%s)",
        settings.PROJECT_NAME,
        settings.ENV,
        extra={"env": settings.ENV, "openai_model": settings.OPENAI_MODEL},
    )
    yield
    logger.info("%s shutting down", settings.PROJECT_NAME)


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Decision-support AI service for climate misinformation detection and inoculation.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())
    with Timer() as timer:
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        timer.elapsed_ms,
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": timer.elapsed_ms,
        },
    )
    return response


app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root() -> dict:
    return {"service": settings.PROJECT_NAME, "status": "running"}


@app.get("/health")
async def health() -> dict:
    """Root-level liveness check (in addition to /api/v1/health)."""
    return {"status": "ok", "service": settings.PROJECT_NAME, "env": settings.ENV}
