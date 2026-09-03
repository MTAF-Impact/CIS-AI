"""Request/response schemas for the AI service's two F5 endpoints -
POST /api/v1/detection/runs and POST /api/v1/detection/snapshots/purge. Field
names mirror the backend's reference contract verbatim."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DetectorParameters(BaseModel):
    """The full detector configuration in force for this run, sent in full on every
    request - no partial-override concept."""

    window_days: int
    bin_width_seconds: int
    null_model_alpha: float
    dup_threshold: float
    sem_threshold: float
    min_post_length: int
    edge_threshold: float
    min_signal_families: int
    k_core: int
    leiden_resolution: float
    min_cluster_size: int
    min_internal_density: float
    beta_time: float
    beta_text: float
    beta_amp: float
    beta_meta: float
    beta_struct: float
    provenance_half_life_hours: float
    anchor_share: float
    min_claim_posts: int
    min_link_strength: float
    high_score_cutoff: float
    high_breadth_cutoff: int
    medium_score_cutoff: float
    medium_breadth_cutoff: int
    cadence_hours: int
    candidate_cap: int
    recurrence_threshold: float
    velocity_trigger_threshold: float


class ExclusionAccount(BaseModel):
    """One declared-legitimate account from the backend's allowlist, travelling
    with the request rather than read from a shared table."""

    platform: str
    platform_account_id: str
    handle: str


class Exclusions(BaseModel):
    """The declared-coordination allowlist and the common-phrase list - both
    backend-owned, travelling with the request rather than read from a table."""

    accounts: list[ExclusionAccount] = Field(default_factory=list)
    phrases: list[str] = Field(default_factory=list)


class DetectionRunRequest(BaseModel):
    """POST /api/v1/detection/runs. The backend computes window_start/window_end
    itself and already rejects Non-Existing/Synthetic claim_ids with 422 before
    calling us."""

    claim_ids: list[uuid.UUID] = Field(min_length=1)
    trigger_source: Literal["scheduled", "velocity", "on_demand"]
    window_start: datetime
    window_end: datetime
    parameters: DetectorParameters
    exclusions: Exclusions = Field(default_factory=Exclusions)


class DetectionRunResponse(BaseModel):
    """Acknowledgement only - the backend never polls, it reads detection_run
    directly."""

    run_id: uuid.UUID
    status: str


class PurgeSnapshotsRequest(BaseModel):
    """POST /api/v1/detection/snapshots/purge. The backend computes which networks
    are past retention (it alone can see whether a report was generated from a
    snapshot) and hands over the list; the rows are AI-owned, so deletion is ours."""

    network_ids: list[uuid.UUID] = Field(default_factory=list)


class PurgeSnapshotsResponse(BaseModel):
    snapshots_purged: int
