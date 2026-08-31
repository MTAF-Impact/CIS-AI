from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    alerts,
    claims,
    coordination,
    fault_lines,
    health,
    ingestion,
    matchmaking,
    networks,
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
api_router.include_router(networks.coordination_router)
api_router.include_router(matchmaking.router)
api_router.include_router(fault_lines.router)
