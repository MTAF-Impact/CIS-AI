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
    """Deterministic heuristic check for Coordinated Inauthentic Behavior across a
    list of posts: burst timing (<10 min), text similarity (>0.80 cosine), and
    account-creation clustering. D3 (Coordinated-Network Detector) dashboard is
    explicitly deferred in the PRD - this endpoint is the groundwork for it."""
    return detect_coordinated_behavior(payload.posts, embedder=embedder)
