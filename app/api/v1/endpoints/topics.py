from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.topic import Topic
from app.schemas.topic import TopicCreate, TopicRead

router = APIRouter(prefix="/topics", tags=["topics"])


@router.get("", response_model=list[TopicRead])
async def list_topics(db: AsyncSession = Depends(get_db)) -> list[Topic]:
    result = await db.execute(select(Topic).order_by(Topic.name))
    return list(result.scalars().all())


@router.post("", response_model=TopicRead, status_code=201)
async def create_topic(payload: TopicCreate, db: AsyncSession = Depends(get_db)) -> Topic:
    """Manual creation, outside the dynamic clustering-driven path."""
    topic = Topic(name=payload.name, description=payload.description)
    db.add(topic)
    await db.commit()
    await db.refresh(topic)
    return topic
