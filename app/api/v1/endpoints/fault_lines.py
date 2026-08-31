"""Read-only listing of fault_lines - the living exemplar corpus a relevance-filtering
crawler fetches each run (see docs/crawler design), rather than a separately-curated list."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.fault_line import FaultLine
from app.schemas.fault_line import FaultLineRead

router = APIRouter(prefix="/fault-lines", tags=["fault-lines"])


@router.get("", response_model=list[FaultLineRead])
async def list_fault_lines(db: AsyncSession = Depends(get_db)) -> list[FaultLine]:
    result = await db.execute(select(FaultLine).order_by(FaultLine.community_name))
    return list(result.scalars().all())
