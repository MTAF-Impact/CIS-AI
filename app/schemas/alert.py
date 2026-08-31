import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ThresholdStatus(str, Enum):
    """Derived at read time by comparing FinalClaimScore against the global admin
    threshold (US29/US32) - never stored, so it can never go stale relative to the
    threshold or the claim's current score."""

    OVER_THRESHOLD = "over_threshold"
    UNDER_THRESHOLD = "under_threshold"


class AlertRow(BaseModel):
    """One [C3] watchlist table row."""

    claim_id: uuid.UUID
    claim_statement: str
    claim_created_date: datetime
    final_claim_score: float
    threshold_status: ThresholdStatus
    added_at: datetime


class AlertListResult(BaseModel):
    total: int
    items: list[AlertRow]


class ChartPoint(BaseModel):
    recorded_at: datetime
    final_claim_score: float


class ChartSeries(BaseModel):
    """[C1]/[C2] - one line per claim currently checked for charting."""

    claim_id: uuid.UUID
    claim_statement: str
    points: list[ChartPoint]
