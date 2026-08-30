from fastapi import APIRouter

from app.api.v1.endpoints import (
    claims,
    coordination,
    health,
    ingestion,
    policies,
    topics,
)

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(ingestion.router)
api_router.include_router(claims.router)
api_router.include_router(topics.router)
api_router.include_router(policies.router)
api_router.include_router(coordination.router)
