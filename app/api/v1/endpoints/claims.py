import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import verify_backend_api_key
from app.models.alert import ClaimAlert
from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.enums import ClaimStatus, ClaimType, Stance
from app.models.policy import ClaimPolicy, Policy
from app.models.topic import Topic
from app.schemas.claim import (
    ClaimListEnvelope,
    ClaimListItemRead,
    ClaimStatusUpdateRequest,
    ClusterNowResponse,
    ExistingClaimDetailRead,
    HarmConfirmRequest,
    NonExistingClaimDetailRead,
    NonExistingClaimPredictRequest,
    NonExistingClaimPredictResponse,
    PolicyBrief,
    RescoreResponse,
    TopAccountEntry,
    TopicBrief,
)
from app.schemas.content import ContentItemRead
from app.schemas.matchmaking import (
    GenerateGenericClaimWebhookRequest,
    GenerateGenericClaimWebhookResponse,
)
from app.services import admin_service, scoring_engine
from app.services.claim_prediction_service import predict_non_existing_claim
from app.services.clustering_service import (
    cluster_unclustered_content,
    rescore_all_existing_claims,
    rescore_claim,
)
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, LLMNotConfiguredError, get_llm_client

router = APIRouter(prefix="/claims", tags=["claims"])

TOP_ACCOUNTS_LIMIT = 5

# docs/AI-INTEGRATION.md's claim_type vocabulary table - aliases accepted for the
# EXISTING/Generic claim type on the Flow 3 generate-generic webhook. Non-existing/
# Synthetic claims are never generated here (they require a Policy, see
# claim_prediction_service.predict_non_existing_claim); an unrecognized value is
# rejected outright rather than silently coerced.
GENERIC_CLAIM_TYPE_ALIASES = frozenset({"existing", "generic", "existing_claim", "generic_claim"})


async def _statement_counts(db: AsyncSession, claim_id: uuid.UUID) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(ContentItem.stance, func.count())
            .where(ContentItem.claim_id == claim_id)
            .group_by(ContentItem.stance)
        )
    ).all()
    counts = dict(rows)
    return counts.get(Stance.SUPPORTING, 0), counts.get(Stance.OPPOSING, 0)


async def _alerted_claim_ids(db: AsyncSession, claim_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not claim_ids:
        return set()
    rows = (
        await db.execute(select(ClaimAlert.claim_id).where(ClaimAlert.claim_id.in_(claim_ids)))
    ).scalars().all()
    return set(rows)


async def _is_alerted(db: AsyncSession, claim_id: uuid.UUID) -> bool:
    return (await db.get(ClaimAlert, claim_id)) is not None


async def _top_accounts(db: AsyncSession, claim_id: uuid.UUID) -> list[TopAccountEntry]:
    """Top 5 accounts by post-volume contribution to this claim's Supporting side - see
    TopAccountEntry's docstring for the interpretation-ambiguity note."""
    rows = (
        await db.execute(
            select(ContentItem.author_id, func.count())
            .where(
                ContentItem.claim_id == claim_id,
                ContentItem.stance == Stance.SUPPORTING,
                ContentItem.author_id.is_not(None),
            )
            .group_by(ContentItem.author_id)
            .order_by(func.count().desc())
            .limit(TOP_ACCOUNTS_LIMIT)
        )
    ).all()
    return [TopAccountEntry(account_handle=handle, contribution_count=count) for handle, count in rows]


async def _to_list_item(db: AsyncSession, claim: Claim, is_alerted: bool) -> ClaimListItemRead:
    positive, negative = await _statement_counts(db, claim.id)
    return ClaimListItemRead(
        id=claim.id,
        claim_type=claim.claim_type,
        claim_statement=claim.claim_statement,
        topic=TopicBrief.model_validate(claim.topic),
        status=claim.status,
        first_caught_at=claim.first_caught_at,
        positive_statement_count=positive,
        negative_statement_count=negative,
        final_claim_score=claim.final_claim_score,
        is_alerted=is_alerted,
    )


async def _fetch_claim_with_relations(db: AsyncSession, claim_id: uuid.UUID) -> Claim | None:
    stmt = (
        select(Claim)
        .where(Claim.id == claim_id)
        .options(
            selectinload(Claim.topic),
            selectinload(Claim.policy),
            selectinload(Claim.policy_links).selectinload(ClaimPolicy.policy),
            selectinload(Claim.content_items),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _to_non_existing_detail(claim: Claim) -> NonExistingClaimDetailRead:
    return NonExistingClaimDetailRead(
        id=claim.id,
        claim_type=claim.claim_type,
        claim_statement=claim.claim_statement,
        topic=TopicBrief.model_validate(claim.topic),
        status=claim.status,
        first_caught_at=claim.first_caught_at,
        created_at=claim.created_at,
        updated_at=claim.updated_at,
        policy=PolicyBrief.model_validate(claim.policy) if claim.policy else None,
        activity_content=claim.activity_content,
        activity_generated_at=claim.activity_generated_at,
    )


async def _to_existing_detail(db: AsyncSession, claim: Claim) -> ExistingClaimDetailRead:
    by_stance: dict[Stance, list[ContentItemRead]] = {
        Stance.SUPPORTING: [],
        Stance.OPPOSING: [],
        Stance.NEUTRAL: [],
    }
    for item in sorted(claim.content_items, key=lambda i: i.created_at):
        if item.stance in by_stance:
            by_stance[item.stance].append(ContentItemRead.model_validate(item))

    policies = [PolicyBrief.model_validate(link.policy) for link in claim.policy_links]

    return ExistingClaimDetailRead(
        id=claim.id,
        claim_type=claim.claim_type,
        claim_statement=claim.claim_statement,
        topic=TopicBrief.model_validate(claim.topic),
        status=claim.status,
        first_caught_at=claim.first_caught_at,
        created_at=claim.created_at,
        updated_at=claim.updated_at,
        reach_score=claim.reach_score,
        velocity_score=claim.velocity_score,
        falseness_score=claim.falseness_score,
        harm_score=claim.harm_score,
        harm_public_safety=claim.harm_public_safety,
        harm_institutional_trust=claim.harm_institutional_trust,
        harm_economic=claim.harm_economic,
        harm_policy_disruption=claim.harm_policy_disruption,
        harm_human_confirmed=claim.harm_human_confirmed,
        emotional_intensity_score=claim.emotional_intensity_score,
        emotional_intensity_opposing=claim.emotional_intensity_opposing,
        claim_score=claim.claim_score,
        npr=claim.npr,
        discount_factor=claim.discount_factor,
        final_claim_score=claim.final_claim_score,
        is_dormant=claim.is_dormant,
        is_alerted=await _is_alerted(db, claim.id),
        activity_content=claim.activity_content,
        activity_generated_at=claim.activity_generated_at,
        top_accounts=await _top_accounts(db, claim.id),
        supporting_statements=by_stance[Stance.SUPPORTING],
        opposing_statements=by_stance[Stance.OPPOSING],
        neutral_statements=by_stance[Stance.NEUTRAL],
        policies=policies,
    )


async def _to_detail(db: AsyncSession, claim: Claim):
    if claim.claim_type == ClaimType.NON_EXISTING:
        return _to_non_existing_detail(claim)
    return await _to_existing_detail(db, claim)


async def _list_claims(
    db: AsyncSession,
    claim_type: ClaimType,
    topic_ids: list[uuid.UUID] | None,
    status: ClaimStatus | None,
    q: str | None,
    limit: int,
    offset: int,
) -> ClaimListEnvelope:
    stmt = (
        select(Claim).options(selectinload(Claim.topic)).where(Claim.claim_type == claim_type)
    )
    if topic_ids:
        stmt = stmt.where(Claim.topic_id.in_(topic_ids))
    if status is not None:
        stmt = stmt.where(Claim.status == status)
    if q:
        stmt = stmt.where(Claim.claim_statement.ilike(f"%{q}%"))

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()

    # Merge-then-rank-then-limit across every selected topic (not top-N-per-topic).
    sort_key = Claim.final_claim_score.desc().nulls_last() if claim_type == ClaimType.EXISTING else Claim.created_at.desc()
    stmt = stmt.order_by(sort_key).limit(limit).offset(offset)
    claims = list((await db.execute(stmt)).scalars().all())

    alerted_ids = await _alerted_claim_ids(db, [claim.id for claim in claims])
    items = [await _to_list_item(db, claim, claim.id in alerted_ids) for claim in claims]
    return ClaimListEnvelope(fetched_at=datetime.now(UTC), total=total, items=items)


@router.get("/existing", response_model=ClaimListEnvelope)
async def list_existing_claims(
    db: AsyncSession = Depends(get_db),
    topic_ids: list[uuid.UUID] | None = Query(default=None),
    status: ClaimStatus | None = None,
    q: str | None = None,
    limit: int = Query(default=10, le=1000),
    offset: int = Query(default=0, ge=0),
) -> ClaimListEnvelope:
    """D1 dashboard: ranked by FinalClaimScore, top-10 by default."""
    return await _list_claims(db, ClaimType.EXISTING, topic_ids, status, q, limit, offset)


@router.get("/non-existing", response_model=ClaimListEnvelope)
async def list_non_existing_claims(
    db: AsyncSession = Depends(get_db),
    topic_ids: list[uuid.UUID] | None = Query(default=None),
    status: ClaimStatus | None = None,
    q: str | None = None,
    limit: int = Query(default=10, le=1000),
    offset: int = Query(default=0, ge=0),
) -> ClaimListEnvelope:
    """D2 dashboard: ranked by most-recently-predicted, top-10 by default."""
    return await _list_claims(db, ClaimType.NON_EXISTING, topic_ids, status, q, limit, offset)


@router.post("/cluster-now", response_model=ClusterNowResponse)
async def cluster_now(
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> ClusterNowResponse:
    """Trigger immediate re-clustering of any not-yet-clustered content items."""
    result = await cluster_unclustered_content(db, llm=llm, embedder=embedder)
    return ClusterNowResponse(
        claims_created=result.claims_created,
        claims_updated=result.claims_updated,
        content_items_clustered=result.content_items_clustered,
    )


@router.post("/rescore", response_model=RescoreResponse)
async def rescore(db: AsyncSession = Depends(get_db)) -> RescoreResponse:
    """Time-based NPR/Velocity/discount/final re-evaluation for every EXISTING claim,
    independent of clustering - NPR can drift purely from wall-clock time (old
    opposing posts aging out of the rolling window) even with zero new content."""
    count = await rescore_all_existing_claims(db)
    return RescoreResponse(claims_rescored=count)


@router.post(
    "/generate-generic",
    response_model=GenerateGenericClaimWebhookResponse,
    status_code=201,
    dependencies=[Depends(verify_backend_api_key)],
)
async def generate_generic_claim_webhook(
    payload: GenerateGenericClaimWebhookRequest,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> GenerateGenericClaimWebhookResponse:
    """Flow 3 of the Go backend integration contract (docs/AI-INTEGRATION.md in the
    CIS-Backend repo) - the F4 "Generate Generic Claim" test button. Distinct from
    admin.generate_generic_claim (our own richer, full-detail admin-panel response);
    this returns exactly the minimal shape the backend's doc specifies."""
    if payload.claim_type is not None and payload.claim_type.lower() not in GENERIC_CLAIM_TYPE_ALIASES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported claim_type {payload.claim_type!r}; this endpoint only "
            "generates Existing/Generic claims",
        )

    target_topic = None
    if payload.topic_id is not None:
        target_topic = await db.get(Topic, payload.topic_id)
        if target_topic is None:
            raise HTTPException(status_code=404, detail="Topic not found")

    try:
        claim = await admin_service.generate_demo_existing_claim(db, llm, embedder)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if target_topic is not None:
        claim.topic_id = target_topic.id
        await db.commit()
        await db.refresh(claim)

    return GenerateGenericClaimWebhookResponse(
        claim_id=claim.id, claim_statement=claim.claim_statement, topic_id=claim.topic_id
    )


@router.post("/non-existing/predict", response_model=NonExistingClaimPredictResponse, status_code=201)
async def predict(
    payload: NonExistingClaimPredictRequest,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> NonExistingClaimPredictResponse:
    """Manual/ad-hoc prediction trigger for an already-registered F2 policy. The
    automatic path is the AI matchmaking pipeline (US42), which runs this same
    underlying logic on policy creation without needing this endpoint."""
    policy = await db.get(Policy, payload.policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    try:
        prediction = await predict_non_existing_claim(db, policy, llm, embedder)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()

    claim = await _fetch_claim_with_relations(db, prediction.claim.id)
    return NonExistingClaimPredictResponse(
        claim=_to_non_existing_detail(claim),
        predicted_attack_angle=prediction.predicted_attack_angle,
        likely_framing=prediction.likely_framing,
    )


@router.get("/{claim_id}", response_model=ExistingClaimDetailRead | NonExistingClaimDetailRead)
async def get_claim(claim_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    claim = await _fetch_claim_with_relations(db, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")
    return await _to_detail(db, claim)


@router.patch("/{claim_id}/status", response_model=ExistingClaimDetailRead | NonExistingClaimDetailRead)
async def update_status(
    claim_id: uuid.UUID, payload: ClaimStatusUpdateRequest, db: AsyncSession = Depends(get_db)
):
    claim = await _fetch_claim_with_relations(db, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    claim.status = payload.status
    await db.commit()

    claim = await _fetch_claim_with_relations(db, claim_id)
    return await _to_detail(db, claim)


@router.patch("/{claim_id}/harm/confirm", response_model=ExistingClaimDetailRead)
async def confirm_harm(
    claim_id: uuid.UUID, payload: HarmConfirmRequest, db: AsyncSession = Depends(get_db)
) -> ExistingClaimDetailRead:
    """Human confirms (optionally overriding) the AI-classified Harm sub-scores,
    recomputing harm_score/claim_score/final_claim_score from the result."""
    claim = await db.get(Claim, claim_id)
    if claim is None or claim.claim_type != ClaimType.EXISTING:
        raise HTTPException(status_code=404, detail="Existing claim not found")

    if payload.public_safety is not None:
        claim.harm_public_safety = payload.public_safety
    if payload.institutional_trust is not None:
        claim.harm_institutional_trust = payload.institutional_trust
    if payload.economic is not None:
        claim.harm_economic = payload.economic
    if payload.policy_disruption is not None:
        claim.harm_policy_disruption = payload.policy_disruption

    claim.harm_human_confirmed = True
    claim.harm_score = scoring_engine.harm_score(
        claim.harm_public_safety or 0.0,
        claim.harm_institutional_trust or 0.0,
        claim.harm_economic or 0.0,
        claim.harm_policy_disruption or 0.0,
    )
    await rescore_claim(db, claim)
    await db.commit()

    claim = await _fetch_claim_with_relations(db, claim_id)
    return await _to_existing_detail(db, claim)


@router.post("/{claim_id}/alert", response_model=ClaimListItemRead, status_code=201)
async def add_alert(claim_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ClaimListItemRead:
    """Bell icon "Add" confirmation (US14) - F3 watchlist only ever accepts EXISTING
    claims (US26)."""
    claim = await db.get(Claim, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")
    if claim.claim_type != ClaimType.EXISTING:
        raise HTTPException(status_code=422, detail="Only Existing claims can be alerted")

    if await db.get(ClaimAlert, claim_id) is None:
        db.add(ClaimAlert(claim_id=claim_id))
        await db.commit()

    claim = await _fetch_claim_with_relations(db, claim_id)
    return await _to_list_item(db, claim, is_alerted=True)


@router.delete("/{claim_id}/alert", response_model=ClaimListItemRead)
async def remove_alert(claim_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ClaimListItemRead:
    """Bell icon "Remove" confirmation (US14)."""
    claim = await _fetch_claim_with_relations(db, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    alert = await db.get(ClaimAlert, claim_id)
    if alert is not None:
        await db.delete(alert)
        await db.commit()

    return await _to_list_item(db, claim, is_alerted=False)
