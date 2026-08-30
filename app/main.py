from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Decision-support AI service for climate misinformation detection and inoculation.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root() -> dict:
    return {"service": settings.PROJECT_NAME, "status": "running"}


@app.get("/health")
async def health() -> dict:
    """Root-level liveness check (in addition to /api/v1/health)."""
    return {"status": "ok", "service": settings.PROJECT_NAME, "env": settings.ENV}
