"""PRD 10.5.5 - Stage 4: cluster-level metrics (SY/DU/CO/PR/AU) and the composite
CoordinationScore. CO reuses DetectedCommunity's density/conductance from
clustering.py directly. PR/AU sub-components aren't given explicit weights in the
spec (unlike Harm Severity in Section 6), so each uses an unweighted mean of whatever
sub-signals are actually available - the same "mean of available sub-signals" pattern
already used for w_meta in signals/provenance.py, documented per component below."""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.services.coordination.clustering import DetectedCommunity
from app.services.coordination.signals.duplication import find_duplicate_post_pairs
from app.services.coordination.signals.provenance import HANDLE_TEMPLATE_RE
from app.services.coordination.types import SignalAccount, SignalPost
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

TIGHTEST_WINDOW_HOURS = 36
BURST_SIGMA_MULTIPLIER = 3
MAX_PLAUSIBLE_POSTS_PER_DAY = 50.0

WEIGHT_SY, WEIGHT_DU, WEIGHT_CO, WEIGHT_PR, WEIGHT_AU = 0.25, 0.25, 0.20, 0.15, 0.15


@dataclass(frozen=True)
class ClusterMetrics:
    sy: float
    du: float
    co: float
    pr: float
    au: float
    coordination_score: float
    # The raw integer observation behind each normalised score (US50: "43 of 47
    # accounts posted within the same 6-minute window", not just the score) -
    # backend's coordinated_network.raw_counts_json. Built from whichever
    # intermediate numerator/denominator each sub-metric already computes; not every
    # component reduces to a single clean count (AU averages four per-account
    # sub-signals), so this is a representative count per metric, not an exhaustive
    # breakdown.
    raw_counts: dict


# --- SY: synchrony ---------------------------------------------------------------


def _mean_within_cluster_w_time(
    member_ids: list[str], w_time: dict[tuple[str, str], float]
) -> float:
    member_set = set(member_ids)
    values = [score for (a, b), score in w_time.items() if a in member_set and b in member_set]
    return sum(values) / len(values) if values else 0.0


def _burst_share(cluster_posts: list[SignalPost], bin_width_seconds: int = 60) -> tuple[float, int, int]:
    """Share of C's posts falling inside bins whose volume exceeds mean + 3*std of
    C's own baseline (its own posting rhythm, not the candidate set's). Returns
    (share, posts_in_burst, total_posts) - the last two back SY's raw_counts entry."""
    total = len(cluster_posts)
    if not cluster_posts:
        return 0.0, 0, 0
    bin_counts: dict[int, int] = defaultdict(int)
    for p in cluster_posts:
        bin_counts[int(p.created_at.timestamp() // bin_width_seconds)] += 1
    counts = list(bin_counts.values())
    mean = sum(counts) / len(counts)
    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    threshold = mean + BURST_SIGMA_MULTIPLIER * math.sqrt(variance)
    anomalous_bins = {b for b, c in bin_counts.items() if c > threshold}
    if not anomalous_bins:
        return 0.0, 0, total
    in_burst = sum(
        1
        for p in cluster_posts
        if int(p.created_at.timestamp() // bin_width_seconds) in anomalous_bins
    )
    return in_burst / total, in_burst, total


def _synchrony(
    member_ids: list[str], cluster_posts: list[SignalPost], w_time: dict[tuple[str, str], float]
) -> tuple[float, int, int]:
    burst_share, posts_in_burst, total_posts = _burst_share(cluster_posts)
    score = 100 * (0.6 * _mean_within_cluster_w_time(member_ids, w_time) + 0.4 * burst_share)
    return score, posts_in_burst, total_posts


# --- DU: duplication ---------------------------------------------------------------


def _duplication(
    cluster_posts: list[SignalPost],
    embedder: MultilingualEmbeddingService | None,
    common_phrase_allowlist: set[str] | None = None,
) -> tuple[float, int, int]:
    total = len(cluster_posts)
    if not cluster_posts:
        return 0.0, 0, 0
    embedder = embedder or get_multilingual_embedding_service()
    eligible, duplicate_pairs = find_duplicate_post_pairs(
        cluster_posts, common_phrase_allowlist=common_phrase_allowlist, embedder=embedder
    )
    duplicated_ids = {eligible[i].id for pair in duplicate_pairs for i in pair}
    return 100 * (len(duplicated_ids) / total), len(duplicated_ids), total


# --- CO: cohesion (reuses clustering.py's own density/conductance) ----------------


def _cohesion(community: DetectedCommunity) -> float:
    return 100 * (1 - community.conductance)


# --- PR: provenance anomaly ---------------------------------------------------------


def _tightest_window_share(
    creation_times: list[datetime], window_hours: float = TIGHTEST_WINDOW_HOURS
) -> tuple[float, int, int]:
    """Returns (share, accounts_in_tightest_window, total_accounts_with_known_creation_date)."""
    times = sorted(creation_times)
    window_seconds = window_hours * 3600
    best, left = 1, 0
    for right in range(len(times)):
        while (times[right] - times[left]).total_seconds() > window_seconds:
            left += 1
        best = max(best, right - left + 1)
    return best / len(times), best, len(times)


def _handle_template_share(accounts: list[SignalAccount]) -> float:
    groups: dict[tuple[str, int], list[str]] = defaultdict(list)
    for a in accounts:
        match = HANDLE_TEMPLATE_RE.match(a.handle.lower()) if a.handle else None
        if match:
            groups[(match.group("prefix"), len(match.group("digits")))].append(a.account_id)
    shared = sum(len(v) for v in groups.values() if len(v) >= 2)
    return shared / len(accounts) if accounts else 0.0


def _duplicate_profile_image_share(accounts: list[SignalAccount]) -> float:
    by_hash: dict[str, list[str]] = defaultdict(list)
    for a in accounts:
        if a.profile_hash:
            by_hash[a.profile_hash].append(a.account_id)
    shared = sum(len(v) for v in by_hash.values() if len(v) >= 2)
    return shared / len(accounts) if accounts else 0.0


def _age_percentile_inverted(
    accounts: list[SignalAccount],
    platform_age_baseline_hours: list[float] | None,
    now: datetime,
) -> float | None:
    """Inverted percentile of this cluster's median account age against a supplied
    platform-wide baseline - younger-than-typical scores higher. Absent (not 0) when
    no baseline is supplied: this service doesn't yet have a live platform-wide age
    distribution to compare against, so the caller must provide one explicitly."""
    if not platform_age_baseline_hours:
        return None
    ages = sorted(
        (now - a.created_at_platform).total_seconds() / 3600
        for a in accounts
        if a.created_at_platform is not None
    )
    if not ages:
        return None
    median_age = ages[len(ages) // 2]
    baseline = sorted(platform_age_baseline_hours)
    rank = sum(1 for b in baseline if b <= median_age) / len(baseline)
    return 1.0 - rank


def _provenance_anomaly(
    accounts: list[SignalAccount],
    platform_age_baseline_hours: list[float] | None,
    now: datetime,
) -> tuple[float, int, int]:
    creation_times = [a.created_at_platform for a in accounts if a.created_at_platform is not None]
    window_share, window_count, window_total = (
        _tightest_window_share(creation_times) if len(creation_times) >= 2 else (None, 0, 0)
    )
    components = [
        window_share,
        _handle_template_share(accounts) if accounts else None,
        _duplicate_profile_image_share(accounts) if accounts else None,
        _age_percentile_inverted(accounts, platform_age_baseline_hours, now),
    ]
    available = [c for c in components if c is not None]
    score = 100 * (sum(available) / len(available)) if available else 0.0
    return score, window_count, window_total


# --- AU: automation & behavioural anomaly ------------------------------------------


def _interpost_regularity(post_times: list[datetime]) -> float | None:
    """1 - normalised entropy of inter-post intervals: perfectly regular spacing
    (scripted posting) -> low entropy -> high regularity score."""
    if len(post_times) < 3:
        return None
    times = sorted(post_times)
    deltas = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    num_bins = min(10, len(deltas))
    lo, hi = min(deltas), max(deltas)
    if hi == lo:
        return 1.0
    bin_width = (hi - lo) / num_bins
    counts = [0] * num_bins
    for d in deltas:
        idx = min(int((d - lo) / bin_width), num_bins - 1)
        counts[idx] += 1
    total = len(deltas)
    entropy = -sum((c / total) * math.log(c / total) for c in counts if c > 0)
    max_entropy = math.log(num_bins)
    normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
    return 1.0 - normalized_entropy


def _circadian_coverage(post_times: list[datetime]) -> float:
    """Share of the 24 hourly buckets active - a fully-covered clock implies no sleep
    cycle."""
    if not post_times:
        return 0.0
    return len({t.hour for t in post_times}) / 24


def _posting_rate_anomaly(post_times: list[datetime], window_hours: float) -> float:
    if not post_times or window_hours <= 0:
        return 0.0
    days = max(window_hours / 24, 1 / 24)
    rate_per_day = len(post_times) / days
    return max(0.0, min(1.0, rate_per_day / MAX_PLAUSIBLE_POSTS_PER_DAY))


def _reshare_ratio(posts: list[SignalPost]) -> float:
    if not posts:
        return 0.0
    reshares = sum(1 for p in posts if p.is_native_reshare or p.reshared_post_id)
    return reshares / len(posts)


def _automation_anomaly(
    member_ids: list[str], cluster_posts: list[SignalPost], window_hours: float
) -> tuple[float, int, int]:
    """Returns (score, cluster_active_hours, 24) - the cluster's combined circadian
    coverage (union of hours any member posted in) as AU's representative raw count.
    AU itself averages four per-account sub-signals, which doesn't reduce to one
    clean count the way SY/DU/PR's numerator/denominator do."""
    posts_by_account: dict[str, list[SignalPost]] = defaultdict(list)
    for p in cluster_posts:
        posts_by_account[p.account_id].append(p)

    per_account_scores: list[float] = []
    for account_id in member_ids:
        account_posts = posts_by_account.get(account_id, [])
        post_times = [p.created_at for p in account_posts]
        components = [
            _interpost_regularity(post_times),
            _circadian_coverage(post_times),
            _posting_rate_anomaly(post_times, window_hours),
            _reshare_ratio(account_posts),
        ]
        available = [c for c in components if c is not None]
        if available:
            per_account_scores.append(sum(available) / len(available))

    score = 100 * (sum(per_account_scores) / len(per_account_scores)) if per_account_scores else 0.0
    cluster_active_hours = len({p.created_at.hour for p in cluster_posts})
    return score, cluster_active_hours, 24


# --- Composite -----------------------------------------------------------------------


def compute_cluster_metrics(
    community: DetectedCommunity,
    cluster_posts: list[SignalPost],
    accounts: list[SignalAccount],
    w_time: dict[tuple[str, str], float],
    window_hours: float,
    now: datetime,
    platform_age_baseline_hours: list[float] | None = None,
    embedder: MultilingualEmbeddingService | None = None,
    common_phrase_allowlist: set[str] | None = None,
) -> ClusterMetrics:
    sy, sy_count, sy_total = _synchrony(community.account_ids, cluster_posts, w_time)
    du, du_count, du_total = _duplication(cluster_posts, embedder, common_phrase_allowlist)
    co = _cohesion(community)
    pr, pr_count, pr_total = _provenance_anomaly(accounts, platform_age_baseline_hours, now)
    au, au_count, au_total = _automation_anomaly(community.account_ids, cluster_posts, window_hours)

    coordination_score = WEIGHT_SY * sy + WEIGHT_DU * du + WEIGHT_CO * co + WEIGHT_PR * pr + WEIGHT_AU * au
    return ClusterMetrics(
        sy=round(sy, 2),
        du=round(du, 2),
        co=round(co, 2),
        pr=round(pr, 2),
        au=round(au, 2),
        coordination_score=round(max(0.0, min(100.0, coordination_score)), 2),
        raw_counts={
            "sy": {"posts_in_burst": sy_count, "total_posts": sy_total},
            "du": {"duplicated_posts": du_count, "total_posts": du_total},
            "pr": {"accounts_in_tightest_window": pr_count, "total_accounts": pr_total},
            "au": {"active_hours": au_count, "total_hours": au_total},
        },
    )
