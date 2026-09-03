from fastapi import APIRouter, Depends

from app.schemas.coordination import CIBCheckRequest, CIBCheckResponse
from app.services.cib_detector import detect_coordinated_behavior
from app.services.embedding_service import EmbeddingService, get_embedding_service

router = APIRouter(prefix="/coordination", tags=["coordination"])


@router.post("/check-cib", response_model=CIBCheckResponse)
async def check_cib(
    payload: CIBCheckRequest,
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> CIBCheckResponse:
    """Deterministic coordinated-behavior heuristic."""
    return detect_coordinated_behavior(payload.posts, embedder=embedder)
