"""F5 (PRD Section 10) request/response schemas for the AI service's one remaining
F5 endpoint - POST /coordination/detection-runs. Everything else (network list/detail,
review, allowlist, reports, F4 config schemas) moved to the backend along with the
endpoints that used them - see docs/COORDINATION.md."""

import uuid

from pydantic import BaseModel, Field


class DetectionRunOverrides(BaseModel):
    """Optional per-run parameter overrides (PRD 10.11's ~15 tunables). Defaults live
    in app.core.config.settings.COORDINATION_* - F4's DB-backed CoordinationSettings
    moved to the backend along with the rest of F5 config ownership, so this is the
    only way left to change a parameter for a given run; omitted fields fall back to
    the static defaults."""

    window_hours: float | None = Field(default=None, gt=0)
    a_max: int | None = Field(default=None, ge=1)
    theta_edge: float | None = Field(default=None, ge=0, le=1)
    k_core: int | None = Field(default=None, ge=1)
    leiden_resolution: float | None = Field(default=None, gt=0)
    n_min: int | None = Field(default=None, ge=1)
    rho_min: float | None = Field(default=None, ge=0, le=1)
    mu_anchor: float | None = Field(default=None, ge=0, le=1)
    p_min: int | None = Field(default=None, ge=0)
    omega_min: float | None = Field(default=None, ge=0, le=1)
    bin_width_seconds: int | None = Field(default=None, ge=1)
    null_model_alpha: float | None = Field(default=None, gt=0, lt=1)
    tau_dup: float | None = Field(default=None, ge=0, le=1)
    tau_sem: float | None = Field(default=None, ge=0, le=1)
    l_min: int | None = Field(default=None, ge=0)
    provenance_half_life_hours: float | None = Field(default=None, gt=0)
    self_exclusion_handles: list[str] | None = None


class DetectionRunTriggerRequest(BaseModel):
    """claim_id set -> single-claim run (covers what used to be the on-demand and
    velocity-triggered calls). claim_id omitted -> full sweep across every Active
    claim (covers the old scheduled trigger). All three PRD 10.5.8 trigger modes are
    now the backend's decision (when to call this, and with which shape), not ours."""

    claim_id: uuid.UUID | None = None
    overrides: DetectionRunOverrides | None = None


class DetectionRunTriggerResponse(BaseModel):
    claim_id: uuid.UUID | None
    status: str
