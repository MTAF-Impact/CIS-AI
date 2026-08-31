from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

SINGLETON_ID = 1


class AdminSetting(Base):
    """F4 - a single global config row, upserted in place. `id` is always SINGLETON_ID."""

    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SINGLETON_ID)
    over_threshold: Mapped[float] = mapped_column(Float, default=70.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
