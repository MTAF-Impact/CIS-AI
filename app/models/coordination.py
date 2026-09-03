"""Coordinated-Network Detector tables. Table names, PK column names, and every
column here follow the backend's reference schema verbatim - the backend's GORM
models read these tables directly by name/column, so a mismatch here breaks
production silently on their side."""

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
    """One pipeline execution. scope_claim_ids covers both a single claim and a
    multi-claim batch run - the backend always sends an array."""

    __tablename__ = "detection_run"

    id: Mapped[uuid.UUID] = mapped_column(
        "run_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_claim_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False)  # scheduled | velocity | on_demand
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parameters: Mapped[dict] = mapped_column("parameters_json", JSONB, nullable=True)
    model_versions: Mapped[dict] = mapped_column(JSONB, nullable=True)
    random_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    library_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signals_unavailable: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    truncated: Mapped[bool] = mapped_column("truncated_bool", Boolean, default=False, nullable=False)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending | running | completed | failed
    status: Mapped[DetectionRunStatus] = mapped_column(
        String(32), default=DetectionRunStatus.PENDING, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Account(Base):
    """A public account observed in a claim-cluster. Durable across detection runs;
    membership (per-run) lives in NetworkAccount."""

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
    """A detected cluster that survived retention + confidence filters. Composite
    CoordinationScore is never computed without SY/DU/CO/PR/AU stored alongside it.
    No review_status column - that's a human assessment on a backend-owned overlay
    table, since a value here would be erased by the next detection run."""

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

    # Raw integer observation behind each metric, not just the normalised score.
    raw_counts: Mapped[dict | None] = mapped_column("raw_counts_json", JSONB, nullable=True)

    account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platforms: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    internal_density: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    conductance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Genuine unclustered accounts active on the same claim, for graph contrast.
    comparison_account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    fingerprint_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    parent_network_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_network.network_id", ondelete="SET NULL"), nullable=True
    )

    # Membership >= 60% allowlisted - stored, not dropped, so suppression stays
    # auditable as the allowlist changes underneath it.
    allowlist_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Retroactively marked after a member was allowlisted.
    relabelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NetworkAccount(Base):
    """Per-detection membership + this account's contribution to the cluster's
    metrics. membership_role separates real cluster members from the comparison
    set. layout_x/layout_y are the precomputed graph-layout coordinates."""

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
    """One retained pairwise edge with its full per-signal decomposition, so any
    account's membership is explainable down to specific behaviours. No surrogate
    id - composite PK, matching the backend's reference schema."""

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
    # An unavailable signal family is recorded at the run level
    # (DetectionRun.signals_unavailable), not per-edge.
    w_time: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_text: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_amp: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_meta: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w_struct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    signal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NetworkEvidencePost(Base):
    """Immutable, hashed content snapshot. Rendered from here, never re-fetched
    live, so a deleted post still has a durable record. posted_at is currently
    backfilled from ContentItem.created_at (ingest time), not real publish time.
    shared_span_start/end are left null until duplicate-text-span highlighting is
    built."""

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
    """Per-bin volume series backing the burst timeline chart. No surrogate id -
    composite PK (network_id, bin_start)."""

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
    """The claim-relevance gate's output. Exactly one row per network has
    is_primary_claim=True (highest overlap_ratio); others are secondary links
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
    """A coordinated cluster that failed anchoring/link-strength against the claim
    it passed through. Never surfaced or exported; retained only for aggregate
    recalibration review."""

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
    """Identity and digest for the evidence captured for one network, for the PDF
    report's chain-of-custody section. expires_at implements the retention window;
    the backend computes which networks are past it and calls
    POST /api/v1/detection/snapshots/purge with the list - see governance.py."""

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
