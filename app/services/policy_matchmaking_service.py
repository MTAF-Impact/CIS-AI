"""F2 AI matchmaking (US42): links a Policy to matching Existing claims, then predicts
one new Non-Existing claim. Two triggers, both BackgroundTasks jobs:
match_and_predict_claims_for_policy (our own POST /policies) and run_matchmaking_webhook
(Go backend's Flow 1, see docs/GO_INTEGRATION.md)."""

import logging
import uuid
from datetime import date

import httpx
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import get_session_factory
from app.models.claim import Claim
from app.models.enums import ClaimType
from app.models.policy import ClaimPolicy, Policy
from app.services import backend_callback_service
from app.services.claim_prediction_service import predict_non_existing_claim
from app.services.document_extraction import UnsupportedDocumentTypeError, extract_text
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, get_llm_client

logger = logging.getLogger(__name__)

DOCUMENT_FETCH_TIMEOUT_SECONDS = 30.0

# Cosine prefilter before the LLM confirmation call, to bound candidate count.
CLAIM_MATCH_PREFILTER_THRESHOLD = 0.35
MAX_MATCH_CANDIDATES = 20
POLICY_TEXT_EXCERPT_CHARS = 4000


async def _run(db, policy: Policy, llm: LLMClient, embedder: EmbeddingService) -> tuple[int, int]:
    """Returns (matched_claim_count, generated_claim_count) for the Flow 2 callback."""
    policy_text = (
        f"{policy.title}\n{policy.description or ''}\n"
        f"{(policy.extracted_text or '')[:POLICY_TEXT_EXCERPT_CHARS]}"
    ).strip()
    policy_embedding = embedder.embed(policy_text)
    policy.embedding = policy_embedding

    existing_claims = list(
        (
            await db.execute(
                select(Claim).where(
                    Claim.claim_type == ClaimType.EXISTING, Claim.embedding.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )

    policy_vec = np.asarray(policy_embedding)
    scored = sorted(
        (
            (float(np.dot(policy_vec, np.asarray(claim.embedding))), claim)
            for claim in existing_claims
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    candidates = [
        claim for score, claim in scored[:MAX_MATCH_CANDIDATES] if score >= CLAIM_MATCH_PREFILTER_THRESHOLD
    ]

    matched_claims: list[Claim] = []
    if candidates:
        try:
            confirmations = await llm.confirm_policy_claim_matches(
                policy.title, policy.description or "", [c.claim_statement for c in candidates]
            )
        except Exception:
            logger.exception(
                "Policy-claim match confirmation failed for policy %s; skipping "
                "existing-claim linking this run",
                policy.id,
            )
        else:
            for claim, is_match in zip(candidates, confirmations, strict=True):
                if is_match:
                    matched_claims.append(claim)
                    db.add(ClaimPolicy(claim_id=claim.id, policy_id=policy.id))

    generated_count = 0
    try:
        await predict_non_existing_claim(
            db,
            policy,
            llm,
            embedder,
            already_covered_claim_statements=[c.claim_statement for c in matched_claims],
        )
        generated_count = 1
    except Exception:
        logger.exception("Non-existing claim prediction failed for policy %s", policy.id)

    return len(matched_claims), generated_count


async def match_and_predict_claims_for_policy(
    policy_id: uuid.UUID,
    llm: LLMClient | None = None,
    embedder: EmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
) -> None:
    """BackgroundTasks job for POST /policies - see app.api.v1.endpoints.policies."""
    llm = llm or get_llm_client()
    embedder = embedder or get_embedding_service()
    session_factory = session_factory or get_session_factory()

    async with session_factory() as db:
        policy = await db.get(Policy, policy_id)
        if policy is None:
            logger.warning("Policy %s no longer exists; skipping matchmaking", policy_id)
            return
        try:
            await _run(db, policy, llm, embedder)
        except Exception:
            logger.exception("Matchmaking pipeline failed for policy %s", policy_id)
        finally:
            policy.processing = False
            await db.commit()


async def _fetch_and_extract(
    file_name: str | None, file_mime_type: str | None, document_url: str | None
) -> tuple[str | None, bytes | None]:
    """Best-effort: any fetch/extract failure falls back to (None, None)."""
    if not document_url:
        return None, None
    try:
        async with httpx.AsyncClient(
            timeout=DOCUMENT_FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.get(document_url)
            response.raise_for_status()
            data = response.content
        return extract_text(file_name or "", file_mime_type, data), data
    except (httpx.HTTPError, UnsupportedDocumentTypeError):
        logger.warning(
            "Could not fetch/extract policy document (file_name=%r) - proceeding with "
            "the name alone",
            file_name,
            exc_info=True,
        )
        return None, None


async def run_matchmaking_webhook(
    backend_policy_id: uuid.UUID,
    name: str,
    description: str | None,
    rolled_out_date: date,
    file_name: str | None,
    file_mime_type: str | None,
    document_url: str | None,
    llm: LLMClient | None = None,
    embedder: EmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
) -> None:
    """Flow 1 handler - creates a Policy for a backend-uploaded policy, runs matchmaking,
    always reports back via Flow 2. Idempotent per backend_policy_id (see GO_INTEGRATION.md)."""
    llm = llm or get_llm_client()
    embedder = embedder or get_embedding_service()
    session_factory = session_factory or get_session_factory()

    async with session_factory() as db:
        existing = (
            await db.execute(select(Policy).where(Policy.backend_policy_id == backend_policy_id))
        ).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "Matchmaking webhook retry for backend policy %s - re-reporting existing "
                "result (ai_policy_id=%s) instead of re-running the pipeline",
                backend_policy_id,
                existing.id,
            )
            matched_count = (
                await db.execute(
                    select(func.count())
                    .select_from(ClaimPolicy)
                    .where(ClaimPolicy.policy_id == existing.id)
                )
            ).scalar_one()
            generated_count = (
                await db.execute(
                    select(func.count())
                    .select_from(Claim)
                    .where(Claim.policy_id == existing.id, Claim.claim_type == ClaimType.NON_EXISTING)
                )
            ).scalar_one()
            await backend_callback_service.report_matchmaking_result(
                backend_policy_id=backend_policy_id,
                ai_policy_id=existing.id,
                status="completed",
                matched_claim_count=matched_count,
                generated_claim_count=generated_count,
            )
            return

        extracted_text, file_data = await _fetch_and_extract(file_name, file_mime_type, document_url)

        policy = Policy(
            title=name,
            description=description,
            rolled_out_date=rolled_out_date,
            extracted_text=extracted_text,
            file_name=file_name,
            file_content_type=file_mime_type,
            file_data=file_data,
            backend_policy_id=backend_policy_id,
            processing=True,
        )
        db.add(policy)
        await db.commit()
        await db.refresh(policy)

        matched_count, generated_count = 0, 0
        error: str | None = None
        try:
            matched_count, generated_count = await _run(db, policy, llm, embedder)
        except Exception as exc:
            logger.exception(
                "Matchmaking webhook pipeline failed for backend policy %s", backend_policy_id
            )
            error = str(exc)
        finally:
            policy.processing = False
            await db.commit()

        await backend_callback_service.report_matchmaking_result(
            backend_policy_id=backend_policy_id,
            ai_policy_id=policy.id,
            status="failed" if error else "completed",
            matched_claim_count=matched_count,
            generated_claim_count=generated_count,
            error=error,
        )
