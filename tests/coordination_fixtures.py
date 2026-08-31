"""Shared request-building helpers for F5 integration tests. DEFAULT_PARAMETERS
mirrors CIS-Backend's CISDetectorSettings.DefaultDetectorSettings() defaults exactly
(internal/models/f5_detector_settings.go, pulled and reviewed this session) - these
are the real field names/values the backend actually sends, not a guess."""

from datetime import UTC, datetime, timedelta

DEFAULT_PARAMETERS = {
    "window_days": 7,
    "bin_width_seconds": 60,
    "null_model_alpha": 0.01,
    "dup_threshold": 0.80,
    "sem_threshold": 0.90,
    "min_post_length": 25,
    "edge_threshold": 0.35,
    "min_signal_families": 2,
    "k_core": 3,
    "leiden_resolution": 1.0,
    "min_cluster_size": 5,
    "min_internal_density": 0.30,
    "beta_time": 0.30,
    "beta_text": 0.25,
    "beta_amp": 0.20,
    "beta_meta": 0.15,
    "beta_struct": 0.10,
    "provenance_half_life_hours": 36,
    "anchor_share": 0.60,
    "min_claim_posts": 20,
    "min_link_strength": 0.15,
    "high_score_cutoff": 70,
    "high_breadth_cutoff": 3,
    "medium_score_cutoff": 55,
    "medium_breadth_cutoff": 2,
    "cadence_hours": 6,
    "candidate_cap": 5000,
    "recurrence_threshold": 0.50,
    "velocity_trigger_threshold": 70,
}


def detection_request(
    claim_ids: list,
    trigger_source: str = "on_demand",
    window_hours: float = 168,
    parameters: dict | None = None,
    accounts: list[dict] | None = None,
    phrases: list[str] | None = None,
) -> dict:
    """Builds a POST /api/v1/detection/runs body matching the backend's exact
    DetectionRunRequest shape (claim_ids, trigger_source, window_start/end,
    parameters, exclusions)."""
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=window_hours)
    return {
        "claim_ids": [str(c) for c in claim_ids],
        "trigger_source": trigger_source,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "parameters": {**DEFAULT_PARAMETERS, **(parameters or {})},
        "exclusions": {"accounts": accounts or [], "phrases": phrases or []},
    }
