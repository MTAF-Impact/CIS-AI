from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.policy import Policy
from app.schemas.policy import PolicyCreate, PolicyRead

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("", response_model=list[PolicyRead])
async def list_policies(db: AsyncSession = Depends(get_db)) -> list[Policy]:
    result = await db.execute(select(Policy).order_by(Policy.title))
    return list(result.scalars().all())


@router.post("", response_model=PolicyRead, status_code=201)
async def create_policy(payload: PolicyCreate, db: AsyncSession = Depends(get_db)) -> Policy:
    """Minimal manual creation - F2 (Public Policy Bank) is out of scope; this exists
    only so claims have something to correlate to (see app.models.policy.Policy)."""
    policy = Policy(title=payload.title, description=payload.description)
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return policy
