from fastapi import APIRouter

from app.api.v1.endpoints import health, ingestion, narratives, prebunk, truth_sandwich

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(ingestion.router)
api_router.include_router(narratives.router)
api_router.include_router(prebunk.router)
api_router.include_router(truth_sandwich.router)
