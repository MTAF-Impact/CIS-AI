"""F2 AI matchmaking pipeline (US42) - runs once, automatically, after a Policy is
created: links the policy to any Existing claims already in the databank that are
genuinely about it, then predicts one new Non-Existing claim for whatever aspect of the
policy isn't already covered by a matched claim.

Runs as a FastAPI BackgroundTasks job (see app.api.v1.endpoints.policies) - by the time
it executes, the request that created the Policy has already returned and its
request-scoped DB session is closed, so this opens and owns its own AsyncSession."""

import logging
import uuid

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import get_session_factory
from app.models.claim import Claim
from app.models.enums import ClaimType
from app.models.policy import ClaimPolicy, Policy
from app.services.claim_prediction_service import predict_non_existing_claim
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, get_llm_client

logger = logging.getLogger(__name__)

# Loose cosine prefilter before the (more expensive, more precise) LLM confirmation call
# - bounds how many candidates get sent to the LLM without missing plausible matches.
CLAIM_MATCH_PREFILTER_THRESHOLD = 0.35
MAX_MATCH_CANDIDATES = 20
POLICY_TEXT_EXCERPT_CHARS = 4000


async def _run(db, policy: Policy, llm: LLMClient, embedder: EmbeddingService) -> None:
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

    try:
        await predict_non_existing_claim(
            db,
            policy,
            llm,
            embedder,
            already_covered_claim_statements=[c.claim_statement for c in matched_claims],
        )
    except Exception:
        logger.exception("Non-existing claim prediction failed for policy %s", policy.id)


async def match_and_predict_claims_for_policy(
    policy_id: uuid.UUID,
    llm: LLMClient | None = None,
    embedder: EmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
) -> None:
    """Runs as a FastAPI BackgroundTasks job - see app.api.v1.endpoints.policies. Takes
    its session factory as a parameter (rather than importing AsyncSessionLocal
    directly) so tests can point it at the test database via
    Depends(get_session_factory) - a background task can't use a request-scoped
    Depends(get_db), so this is the only way its DB access stays test-overridable."""
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
