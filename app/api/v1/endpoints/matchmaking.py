"""Flow 1: the Go backend POSTs here after a policy is uploaded through F2."""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import get_session_factory
from app.core.security import verify_backend_api_key
from app.schemas.matchmaking import (
    PolicyMatchmakingAckResponse,
    PolicyMatchmakingWebhookRequest,
)
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, get_llm_client
from app.services.policy_matchmaking_service import run_matchmaking_webhook

router = APIRouter(prefix="/matchmaking", tags=["matchmaking"])


@router.post("/policies", response_model=PolicyMatchmakingAckResponse, status_code=202)
async def receive_policy_for_matchmaking(
    payload: PolicyMatchmakingWebhookRequest,
    background_tasks: BackgroundTasks,
    session_factory: async_sessionmaker = Depends(get_session_factory),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
    _: None = Depends(verify_backend_api_key),
) -> PolicyMatchmakingAckResponse:
    background_tasks.add_task(
        run_matchmaking_webhook,
        backend_policy_id=payload.policy_id,
        name=payload.name,
        description=payload.description,
        rolled_out_date=payload.rolled_out_date,
        file_name=payload.file_name,
        file_mime_type=payload.file_mime_type,
        document_url=payload.document_url,
        force=payload.force,
        callback_url=payload.callback_url,
        llm=llm,
        embedder=embedder,
        session_factory=session_factory,
    )
    return PolicyMatchmakingAckResponse()
