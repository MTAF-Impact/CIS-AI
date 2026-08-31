"""PRD 10.5.2.1 - Signal 1: temporal synchrony (w_time). Null-model correction is
mandatory: raw co-occurrence is dominated by the diurnal rhythm (everyone posts at
lunchtime), so this is never used unadjusted."""

from collections import defaultdict

import numpy as np
from scipy.stats import norm

from app.services.coordination.types import SignalPost, pair_key

DEFAULT_BIN_WIDTH_SECONDS = 60
DEFAULT_NULL_MODEL_ALPHA = 0.01


def _bin_index(post: SignalPost, bin_width_seconds: int) -> int:
    return int(post.created_at.timestamp() // bin_width_seconds)


def compute_temporal_synchrony(
    posts: list[SignalPost],
    bin_width_seconds: int = DEFAULT_BIN_WIDTH_SECONDS,
    alpha: float = DEFAULT_NULL_MODEL_ALPHA,
) -> dict[tuple[str, str], float]:
    """Returns w_time(i,j) in (0,1] for every pair whose observed bin co-occurrence
    significantly exceeds the null model's expectation; pairs that don't clear the
    null are simply absent (equivalent to w_time = 0 downstream).

    Null model: Poisson-binomial closed form (preferred per spec over 1,000
    permutations - ~100x cheaper, deterministic). Uses a rank-1 probabilistic
    relaxation of the fixed-marginal permutation null (a standard approximation for
    this class of co-occurrence null model): p(account active in bin b) is
    proportional to both the account's total active-bin count and bin b's share of
    global activity, so it reproduces the diurnal rhythm as the null hypothesis.
    """
    account_bins: dict[str, set[int]] = defaultdict(set)
    post_counts: dict[str, int] = defaultdict(int)
    for p in posts:
        account_bins[p.account_id].add(_bin_index(p, bin_width_seconds))
        post_counts[p.account_id] += 1

    accounts = list(account_bins.keys())
    n = len(accounts)
    if n < 2:
        return {}

    all_bins = sorted({b for bins in account_bins.values() for b in bins})
    bin_index = {b: i for i, b in enumerate(all_bins)}
    n_bins = len(all_bins)

    activity = np.zeros((n_bins, n), dtype=np.float64)
    for a_idx, account in enumerate(accounts):
        for b in account_bins[account]:
            activity[bin_index[b], a_idx] = 1.0

    account_active_bin_count = activity.sum(axis=0)
    bin_activity_count = activity.sum(axis=1)
    total_activations = bin_activity_count.sum()
    if total_activations == 0:
        return {}
    bin_share = bin_activity_count / total_activations

    # p_active[b, a] = min(1, account a's active-bin count * bin b's global share).
    p_active = np.minimum(1.0, np.outer(bin_share, account_active_bin_count))

    obs = activity.T @ activity
    mean = p_active.T @ p_active
    second_moment = (p_active**2).T @ (p_active**2)
    variance = np.maximum(mean - second_moment, 0.0)

    z = norm.ppf(1 - alpha)
    threshold = mean + z * np.sqrt(variance)

    n_posts = np.array([post_counts[a] for a in accounts], dtype=np.float64)

    results: dict[tuple[str, str], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            if obs[i, j] <= threshold[i, j]:
                continue
            denom = min(n_posts[i], n_posts[j]) - mean[i, j]
            if denom <= 0:
                continue
            score = float(np.clip((obs[i, j] - mean[i, j]) / denom, 0.0, 1.0))
            if score > 0:
                results[pair_key(accounts[i], accounts[j])] = round(score, 4)
    return results
