import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FaultLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    community_name: str
    grievance_theme: str
    description: str | None
    created_at: datetime
