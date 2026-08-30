import uuid
from datetime import date, datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import Response
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints.claims import _alerted_claim_ids, _to_list_item
from app.core.database import get_db, get_session_factory
from app.models.claim import Claim
from app.models.policy import ClaimPolicy, Policy
from app.schemas.policy import PolicyDetailRead, PolicyListResult, PolicyRead
from app.services.document_extraction import UnsupportedDocumentTypeError, extract_text
from app.services.policy_matchmaking_service import match_and_predict_claims_for_policy

router = APIRouter(prefix="/policies", tags=["policies"])


async def _latest_linked_claim_activity(db: AsyncSession, policy_id: uuid.UUID) -> datetime | None:
    """US35's sort key: the latest created_at among ANY claim linked to this policy
    (many-to-many Existing via ClaimPolicy, one-to-many Non-Existing via
    Claim.policy_id) - not the policy's own creation date."""
    existing_max = (
        await db.execute(
            select(func.max(Claim.created_at))
            .select_from(ClaimPolicy)
            .join(Claim, Claim.id == ClaimPolicy.claim_id)
            .where(ClaimPolicy.policy_id == policy_id)
        )
    ).scalar_one()
    non_existing_max = (
        await db.execute(select(func.max(Claim.created_at)).where(Claim.policy_id == policy_id))
    ).scalar_one()
    candidates = [d for d in (existing_max, non_existing_max) if d is not None]
    return max(candidates) if candidates else None


@router.get("", response_model=PolicyListResult)
async def list_policies(
    db: AsyncSession = Depends(get_db),
    years: list[int] | None = Query(default=None),
    q: str | None = None,
    limit: int = Query(default=10, le=1000),
    offset: int = Query(default=0, ge=0),
) -> PolicyListResult:
    stmt = select(Policy)
    if years:
        stmt = stmt.where(extract("year", Policy.rolled_out_date).in_(years))
    if q:
        stmt = stmt.where(Policy.title.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    policies = list((await db.execute(stmt)).scalars().all())

    # US35: policies with linked-claim activity first (most recent first), then
    # policies with no linked claims yet, by their own creation date (most recent first).
    ranked = []
    for policy in policies:
        latest = await _latest_linked_claim_activity(db, policy.id)
        ranked.append((0, latest) if latest is not None else (1, policy.created_at))
    ranked_policies = sorted(
        zip(ranked, policies, strict=True), key=lambda pair: (pair[0][0], -pair[0][1].timestamp())
    )
    page = [policy for _, policy in ranked_policies[offset : offset + limit]]

    return PolicyListResult(total=total, items=[PolicyRead.model_validate(p) for p in page])


@router.post("", response_model=PolicyRead, status_code=201)
async def create_policy(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=255),
    rolled_out_date: date = Form(...),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> Policy:
    """US40 - the only 3 fields the "Add Public Policy" modal collects. Immediately
    after commit, kicks off the AI matchmaking pipeline (US42) in the background - the
    response returns right away with processing=True; the FE shows the "Processing"
    badge until a later fetch shows processing=False."""
    data = await file.read()
    try:
        extracted_text = extract_text(file.filename or "", file.content_type, data)
    except UnsupportedDocumentTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    policy = Policy(
        title=name,
        rolled_out_date=rolled_out_date,
        extracted_text=extracted_text,
        file_name=file.filename,
        file_content_type=file.content_type,
        file_data=data,
        processing=True,
    )
    db.add(policy)
    await db.commit()
    await db.refresh(policy)

    background_tasks.add_task(
        match_and_predict_claims_for_policy, policy.id, session_factory=session_factory
    )
    return policy


@router.get("/{policy_id}", response_model=PolicyDetailRead)
async def get_policy(policy_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> PolicyDetailRead:
    """US39 - correlated Existing/Non-Existing claims, using the exact same card
    component/behavior as F1's S1 (US10)/S2 (US18) - no policy-specific variant."""
    policy = await db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    existing_claim_ids = (
        await db.execute(select(ClaimPolicy.claim_id).where(ClaimPolicy.policy_id == policy_id))
    ).scalars().all()
    existing_claims = []
    if existing_claim_ids:
        claims = (
            await db.execute(
                select(Claim)
                .options(selectinload(Claim.topic))
                .where(Claim.id.in_(existing_claim_ids))
            )
        ).scalars().all()
        alerted_ids = await _alerted_claim_ids(db, [c.id for c in claims])
        existing_claims = [await _to_list_item(db, c, c.id in alerted_ids) for c in claims]

    non_existing_claims_orm = (
        await db.execute(
            select(Claim).options(selectinload(Claim.topic)).where(Claim.policy_id == policy_id)
        )
    ).scalars().all()
    non_existing_claims = [await _to_list_item(db, c, False) for c in non_existing_claims_orm]

    return PolicyDetailRead(
        **PolicyRead.model_validate(policy).model_dump(),
        existing_claims=existing_claims,
        non_existing_claims=non_existing_claims,
    )


@router.get("/{policy_id}/file")
async def download_policy_file(policy_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Response:
    policy = await db.get(Policy, policy_id)
    if policy is None or policy.file_data is None:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(
        content=policy.file_data,
        media_type=policy.file_content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{policy.file_name or "policy"}"'},
    )
