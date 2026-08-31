"""F5 - Coordinated-Network Detector (PRD v1.4 Section 10). This is the AI service's
full F5 footprint per the backend integration doc's ownership split: exactly these 9
pipeline-output tables + a single run-trigger endpoint. Everything human-facing
(review workflow, allowlist, PDF/ZIP reports, export audit log, F4 config) moved to
the backend - see docs/COORDINATION.md. Table names/fields otherwise still follow PRD
10.10 as closely as this codebase's conventions allow (UUID PKs, additive schema via
Base.metadata.create_all - see scripts/reset_schema.py)."""

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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.enums import ConfidenceBand, DetectionRunStatus


class DetectionRun(Base):
    """PRD 10.5.1/10.5.8 - one pipeline execution. scope_claim_ids covers both
    single-claim and topic-batch runs (10.5.1 point 6)."""

    __tablename__ = "detection_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_claim_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    random_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    signals_unavailable: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    candidates_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DetectionRunStatus] = mapped_column(
        String(16), default=DetectionRunStatus.RUNNING, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Account(Base):
    """PRD 10.4 - a public account observed in a claim-cluster. Durable across
    detection runs; membership (per-run) lives in NetworkAccount."""

    __tablename__ = "coordination_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    handle: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at_platform: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # pHash
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CoordinatedNetwork(Base):
    """PRD 10.5.5/10.6 - a detected cluster that survived retention + confidence
    filters. Composite CoordinationScore is never computed without SY/DU/CO/PR/AU
    stored alongside it (10.6.1 transparency requirement)."""

    __tablename__ = "coordinated_networks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("detection_runs.id", ondelete="RESTRICT"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    coordination_score: Mapped[float] = mapped_column(Float, nullable=False)
    sy: Mapped[float] = mapped_column(Float, nullable=False)
    du: Mapped[float] = mapped_column(Float, nullable=False)
    co: Mapped[float] = mapped_column(Float, nullable=False)
    pr: Mapped[float] = mapped_column(Float, nullable=False)
    au: Mapped[float] = mapped_column(Float, nullable=False)
    signal_breadth: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_band: Mapped[ConfidenceBand] = mapped_column(String(8), nullable=False)

    account_count: Mapped[int] = mapped_column(Integer, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False)
    platforms: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    internal_density: Mapped[float] = mapped_column(Float, nullable=False)
    conductance: Mapped[float] = mapped_column(Float, nullable=False)

    # Precomputed layout (account_id -> [x, y]) so the UI and any future PDF render
    # identically (10.5.6 point 5) - Fruchterman-Reingold, not literal ForceAtlas2,
    # see evidence.build_graph_snapshot's docstring.
    graph_layout: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # PRD 10.5.7 recurrence tracking.
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_network_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_networks.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NetworkAccount(Base):
    """PRD 10.5.6/US55 - per-detection membership + this account's individual
    contribution to the cluster's metrics (score_contribution)."""

    __tablename__ = "network_accounts"

    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_networks.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordination_accounts.id", ondelete="RESTRICT"), primary_key=True
    )
    posts_in_cluster: Mapped[int] = mapped_column(Integer, nullable=False)
    duplication_rate: Mapped[float] = mapped_column(Float, nullable=False)
    median_interpost_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    circadian_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    degree_centrality: Mapped[float] = mapped_column(Float, nullable=False)
    eigenvector_centrality: Mapped[float] = mapped_column(Float, nullable=False)
    score_contribution: Mapped[dict] = mapped_column(JSONB, nullable=False)


class NetworkEdge(Base):
    """PRD 10.5.3 - one retained pairwise edge with its full per-signal
    decomposition, so any account's membership is explainable down to specific
    behaviours (10.5, point 3)."""

    __tablename__ = "network_edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_networks.id", ondelete="CASCADE"), nullable=False
    )
    account_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordination_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    account_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordination_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    w_total: Mapped[float] = mapped_column(Float, nullable=False)
    w_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    w_text: Mapped[float | None] = mapped_column(Float, nullable=True)
    w_amp: Mapped[float | None] = mapped_column(Float, nullable=True)
    w_meta: Mapped[float | None] = mapped_column(Float, nullable=True)
    w_struct: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_count: Mapped[int] = mapped_column(Integer, nullable=False)


class NetworkEvidencePost(Base):
    """PRD 10.5.6 point 3 - immutable, hashed content snapshot. Rendered from here,
    never re-fetched live, so a deleted post still has a durable record (US54)."""

    __tablename__ = "network_evidence_posts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_networks.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordination_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    post_platform_id: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_text: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duplicate_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    still_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class NetworkBurstBin(Base):
    """PRD 10.5.6 point 2 - per-bin volume series backing the burst timeline chart
    (US53)."""

    __tablename__ = "network_burst_bins"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_networks.id", ondelete="CASCADE"), nullable=False
    )
    bin_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bin_width_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False)
    zscore: Mapped[float] = mapped_column(Float, nullable=False)
    is_anomalous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class NetworkClaimLink(Base):
    """PRD 10.5.1a - the claim-relevance gate's output. Exactly one row per network
    has is_primary_claim=True (highest overlap_ratio); others are secondary links
    above ω_min."""

    __tablename__ = "network_claim_links"

    network_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coordinated_networks.id", ondelete="CASCADE"), primary_key=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    overlap_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    anchoring_share: Mapped[float] = mapped_column(Float, nullable=False)
    claim_cluster_post_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_primary_claim: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    passed_relevance_gate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class OfftopicCluster(Base):
    """PRD 10.5.1a point 7 - a coordinated cluster that failed anchoring/link-strength
    against the claim it passed through. Never surfaced in the network list or
    exported; retained only for aggregate recalibration review (F4)."""

    __tablename__ = "offtopic_clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("detection_runs.id", ondelete="CASCADE"), nullable=False
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    coordination_signals: Mapped[dict] = mapped_column(JSONB, nullable=False)
    overlap_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    anchoring_share: Mapped[float] = mapped_column(Float, nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, nullable=False)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    failed_test: Mapped[str] = mapped_column(String(32), nullable=False)  # anchoring|evidence_volume|link_strength
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


