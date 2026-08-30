from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.response import (
    CIBCheckRequest,
    CIBCheckResponse,
    PrebunkPredictRequest,
    PrebunkPredictResponse,
)
from app.services import rag_service
from app.services.cib_detector import detect_coordinated_behavior
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import (
    LLMClient,
    LLMNotConfiguredError,
    get_llm_client,
)

router = APIRouter(prefix="/prebunk", tags=["prebunk"])


@router.post("/predict", response_model=PrebunkPredictResponse)
async def predict_prebunk(
    payload: PrebunkPredictRequest,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> PrebunkPredictResponse:
    """Given a policy description, predict the likely misinformation attack angle and
    draft an inoculation explainer, grounded in matching community fault lines (RAG)."""
    query_text = f"{payload.policy_title or ''} {payload.policy_description}".strip()
    fault_lines = await rag_service.retrieve_relevant_fault_lines(db, query_text, embedder)
    grounding_context = rag_service.build_grounding_context(fault_lines)

    try:
        prediction = await llm.predict_prebunk(payload.policy_description, grounding_context)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return PrebunkPredictResponse(
        predicted_attack_angle=prediction.predicted_attack_angle,
        likely_framing=prediction.likely_framing,
        inoculation_explainer=prediction.inoculation_explainer,
        grounding_sources=[fl.community_name for fl in fault_lines],
    )


@router.post("/check-cib", response_model=CIBCheckResponse)
async def check_cib(
    payload: CIBCheckRequest,
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> CIBCheckResponse:
    """Deterministic heuristic check for Coordinated Inauthentic Behavior across a
    list of posts: burst timing (<10 min), text similarity (>0.80 cosine), and
    account-creation clustering."""
    return detect_coordinated_behavior(payload.posts, embedder=embedder)
