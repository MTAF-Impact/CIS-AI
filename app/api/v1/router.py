from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    alerts,
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
api_router.include_router(alerts.router)
api_router.include_router(admin.router)
api_router.include_router(coordination.router)
