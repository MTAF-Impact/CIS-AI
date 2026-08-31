"""PRD 10.5.3 - Stage 2: signal fusion and edge pruning. The multi-signal rule here is
the pipeline's primary false-positive control - no edge, and therefore no network, may
ever be constructed from a single behavioural axis. Synchrony alone is a timezone.
Duplication alone is a hashtag. Provenance alone is a signup surge."""

from dataclasses import dataclass

SignalName = str  # "w_time" | "w_text" | "w_amp" | "w_meta" | "w_struct"
PairScores = dict[tuple[str, str], float]

DEFAULT_WEIGHTS: dict[SignalName, float] = {
    "w_time": 0.30,
    "w_text": 0.25,
    "w_amp": 0.20,
    "w_meta": 0.15,
    "w_struct": 0.10,
}
DEFAULT_THETA_EDGE = 0.35
MIN_SIGNAL_FAMILIES_PER_EDGE = 2
MULTI_SIGNAL_CONTRIBUTION_THRESHOLD = 0.25


@dataclass(frozen=True)
class FusedEdge:
    account_a: str
    account_b: str
    w_total: float
    per_signal: dict[SignalName, float]  # only families that actually contributed (>0)
    signal_count: int


def _effective_weights(
    signals: dict[SignalName, PairScores | None],
    base_weights: dict[SignalName, float],
) -> tuple[dict[SignalName, float], list[SignalName]]:
    """Redistributes an entirely-unavailable family's weight proportionally across
    the rest (10.5.2.5), distinct from a pair simply not clearing a signal's own
    threshold (which contributes 0.0 for that pair, not a redistribution)."""
    unavailable = [name for name in base_weights if signals.get(name) is None]
    available = [name for name in base_weights if name not in unavailable]
    unavailable_weight = sum(base_weights[name] for name in unavailable)
    available_weight_sum = sum(base_weights[name] for name in available)

    if not available_weight_sum:
        return dict(base_weights), unavailable

    weights = {
        name: base_weights[name]
        + (base_weights[name] / available_weight_sum) * unavailable_weight
        for name in available
    }
    return weights, unavailable


def fuse_and_prune(
    signals: dict[SignalName, PairScores | None],
    weights: dict[SignalName, float] | None = None,
    theta_edge: float = DEFAULT_THETA_EDGE,
) -> tuple[list[FusedEdge], list[SignalName]]:
    """signals maps each family name to its pairwise score dict, or None if that
    family was entirely unavailable this run (e.g. w_struct with no follower data).
    Returns (retained edges, unavailable family names) - the caller records the
    latter on detection_run.signals_unavailable."""
    base_weights = weights or DEFAULT_WEIGHTS
    effective_weights, unavailable = _effective_weights(signals, base_weights)

    all_pairs: set[tuple[str, str]] = set()
    for name in effective_weights:
        all_pairs.update((signals.get(name) or {}).keys())

    edges: list[FusedEdge] = []
    for account_a, account_b in all_pairs:
        per_signal: dict[SignalName, float] = {}
        total = 0.0
        for name, weight in effective_weights.items():
            value = (signals.get(name) or {}).get((account_a, account_b), 0.0)
            if value > 0:
                per_signal[name] = value
            total += weight * value

        if total < theta_edge:
            continue
        strong_families = sum(
            1 for v in per_signal.values() if v >= MULTI_SIGNAL_CONTRIBUTION_THRESHOLD
        )
        if strong_families < MIN_SIGNAL_FAMILIES_PER_EDGE:
            continue

        edges.append(
            FusedEdge(
                account_a=account_a,
                account_b=account_b,
                w_total=round(total, 4),
                per_signal={k: round(v, 4) for k, v in per_signal.items()},
                signal_count=len(per_signal),
            )
        )

    return edges, unavailable
