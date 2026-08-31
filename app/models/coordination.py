"""F5 - Coordinated-Network Detector (PRD v1.4 Section 10). Table names, PK column
names, and every column here follow the backend's actual reference schema verbatim
(`CIS-Backend` docs/sql/01_f5_reference_schema.sql and internal/models/f5_ai_tables.go,
pulled and reviewed this session) - NOT the older PRD-10.10-only shape this file had
before. That reference schema is the real, concrete contract: the backend's GORM
models read these tables directly by name/column, so a mismatch here is a silent
production break on their side, not a build error on ours. See docs/COORDINATION.md
for the full picture, including the open questions sent back to the backend team
about a few of these columns.

10 tables total: the 9 PRD 10.10 pipeline-output tables plus `evidence_snapshot`
(the backend's "BEYOND 10.10" proposal for PDF chain-of-custody, gap 8)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import ConfidenceBand, DetectionRunStatus


class DetectionRun(Base):
    """PRD 10.5.1/10.5.8 - one pipeline execution. scope_claim_ids covers both a
    single claim and a multi-claim batch run (10.5.1 point 6) - the backend always
    sends an array (`claim_ids`), even for a single id."""

    __tablename__ = "detection_run"

    id: Mapped[uuid.UUID] = mapped_column(
        "run_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_claim_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # scheduled | velocity | on_demand (PRD 10.5.8) - recorded so an analyst can tell
    # an automated detection from one somebody asked for.
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parameters: Mapped[dict] = mapped_column("parameters_json", JSONB, nullable=True)
    model_versions: Mapped[dict] = mapped_column(JSONB, nullable=True)
    random_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    library_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signals_unavailable: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    truncated: Mapped[bool] = mapped_column("truncated_bool", Boolean, default=False, nullable=False)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending | running | completed | failed - pending is the state the row is
    # created in (synchronously, before the 202 response), before the background
    # task has even started.
    status: Mapped[DetectionRunStatus] = mapped_column(
        String(32), default=DetectionRunStatus.PENDING, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Account(Base):
    """PRD 10.4 - a public account observed in a claim-cluster. Durable across
    detection runs; membership (per-run) lives in NetworkAccount. bio/declared_location/
    client_app are the backend's "BEYOND 10.10" gap-1 proposal - three of w_meta's five
    sub-signals had no source column at all without them."""

    __tablename__ = "account"
    __table_args__ = (UniqueConstraint("platform", "platform_account_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        "account_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    handle: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at_platform: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)  # pHash
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_app: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CoordinatedNetwork(Base):
    """PRD 10.5.5/10.6 - a detected cluster that survived retention + confidence
    filters. Composite CoordinationScore is never computed without SY/DU/CO/PR/AU
    stored alongside it (10.6.1 transparency requirement). No review_status column -
    that's a human assessment on a backend-owned overlay table now (a value here would
    be erased by the next detection run); see docs/COORDINATION.md."""

    __tablename__ = "coordinated_network"

    id: Mapped[uuid.UUID] = mapped_column(
        "network_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("detection_run.run_id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    coordination_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sy: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    du: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    co: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pr: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    au: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    signal_breadth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence_band: Mapped[ConfidenceBand] = mapped_column(
        String(16), default=ConfidenceBand.LOW, nullable=False
    )

    # The raw integer observation behind each metric (US50: "43 of 47 accounts
    # posted within the same 6-minute window", not just the normalised score).
    raw_counts: Mapped[dict | None] = mapped_column("raw_counts_json", JSONB, nullable=True)

    account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platforms: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    internal_density: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    conductance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # BEYOND 10.10 (backend gap 7) - genuine unclustered accounts active on the same
    # claim, rendered for contrast in the graph (US51) and counted in the report.
    comparison_account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # PRD 10.5.7 recurrence tracking.
    fingerprint_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    parent_network_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="SET NULL"), nullable=True
    )

    # Membership >= 60% allowlisted (PRD 10.6.3 rule 3) - stored, not silently
    # dropped, so the suppression is stable and auditable as the allowlist changes
    # underneath it. Persisted with the network row rather than skipped entirely.
    allowlist_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # US56 retroactively marked this network after a member was allowlisted. See
    # docs/COORDINATION.md's open question to the backend team about how this gets set.
    relabelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NetworkAccount(Base):
    """PRD 10.5.6/US55 - per-detection membership + this account's individual
    contribution to the cluster's metrics (score_contribution). membership_role
    separates real cluster members from the comparison/contrast set (backend gap 7).
    layout_x/layout_y are the precomputed graph-layout coordinates per account
    (backend gap 4) - replaces the old single JSONB blob on the network row."""

    __tablename__ = "network_account"

    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id", ondelete="CASCADE"), primary_key=True
    )
    # member | comparison
    membership_role: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    posts_in_cluster: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplication_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    median_interpost_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    circadian_coverage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    degree_centrality: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    eigenvector_centrality: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    score_contribution: Mapped[dict | None] = mapped_column("score_contribution_json", JSONB, nullable=True)
    layout_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    layout_y: Mapped[float | None] = mapped_column(Float, nullable=True)


class NetworkEdge(Base):
    """PRD 10.5.3 - one retained pairwise edge with its full per-signal
    decomposition, so any account's membership is explainable down to specific
    behaviours (10.5, point 3). No surrogate id - composite PK, matching the
    backend's reference schema exactly."""

    __tablename__ = "network_edge"

    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="CASCADE"), primary_key=True
    )
    account_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id", ondelete="CASCADE"), primary_key=True
    )
    account_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id", ondelete="CASCADE"), primary_key=True
    )
    w_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # NOT NULL DEFAULT 0 per the backend's reference schema - an unavailable signal
    # family is recorded at the run level (DetectionRun.signals_unavailable), not
    # per-edge; see docs/COORDINATION.md's open question about this.
    w_time: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_text: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_amp: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_meta: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_struct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    signal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NetworkEvidencePost(Base):
    """PRD 10.5.6 point 3 - immutable, hashed content snapshot. Rendered from here,
    never re-fetched live, so a deleted post still has a durable record (US54).
    posted_at is separate from captured_at (see docs/COORDINATION.md's ingest-vs-
    publish-time gap - posted_at is currently backfilled from ContentItem.created_at
    as an interim stand-in, not real publish time). shared_span_start/end are the
    backend's "BEYOND 10.10" proposal for US54/report duplicate-text highlighting -
    left null until that sub-feature is built (see COORDINATION.md known gaps)."""

    __tablename__ = "network_evidence_post"

    id: Mapped[uuid.UUID] = mapped_column(
        "evidence_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id", ondelete="CASCADE"), nullable=False
    )
    post_platform_id: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_text: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duplicate_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    still_public: Mapped[bool] = mapped_column("still_public_bool", Boolean, default=True, nullable=False)
    shared_span_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shared_span_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class NetworkBurstBin(Base):
    """PRD 10.5.6 point 2 - per-bin volume series backing the burst timeline chart
    (US53). No surrogate id - composite PK (network_id, bin_start), matching the
    backend's reference schema exactly."""

    __tablename__ = "network_burst_bin"

    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="CASCADE"), primary_key=True
    )
    bin_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    bin_width_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    zscore: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_anomalous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class NetworkClaimLink(Base):
    """PRD 10.5.1a - the claim-relevance gate's output. Exactly one row per network
    has is_primary_claim=True (highest overlap_ratio); others are secondary links
    above omega_min."""

    __tablename__ = "network_claim_link"

    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="CASCADE"), primary_key=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    overlap_ratio: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    anchoring_share: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    claim_cluster_post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_primary_claim: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    passed_relevance_gate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class OfftopicCluster(Base):
    """PRD 10.5.1a point 7 - a coordinated cluster that failed anchoring/link-strength
    against the claim it passed through. Never surfaced in the network list or
    exported; retained only for aggregate recalibration review (now the backend's -
    it reads this table directly)."""

    __tablename__ = "offtopic_cluster"

    id: Mapped[uuid.UUID] = mapped_column(
        "cluster_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("detection_run.run_id", ondelete="CASCADE"), nullable=False
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    coordination_signals: Mapped[dict] = mapped_column("coordination_signals_json", JSONB, nullable=True)
    overlap_ratio: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    anchoring_share: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fingerprint_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    failed_test: Mapped[str] = mapped_column(String(32), nullable=False)  # anchoring|evidence_volume|link_strength
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvidenceSnapshot(Base):
    """Backend "BEYOND 10.10" gap 8 - a first-class identity and digest for the
    evidence captured for one network, for the PDF report's chain-of-custody section
    (PRD 10.8 item 10: snapshot ID, snapshot hash, detection run ID, export audit
    entry ID). expires_at implements the retention window (PRD 10.9.1 rule 7); the
    backend computes which networks are past it (it alone can see whether a report
    was generated) and calls POST /api/v1/detection/snapshots/purge with the list -
    see governance.py."""

    __tablename__ = "evidence_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(
        "snapshot_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("coordinated_network.network_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("detection_run.run_id", ondelete="CASCADE"), nullable=False
    )
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
