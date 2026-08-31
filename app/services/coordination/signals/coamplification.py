"""PRD 10.5.2.3 - Signal 3: co-amplification (w_amp). Two accounts consistently
boosting the same *unpopular* things share real signal; two accounts boosting a post
with millions of views share nothing - the inverse-popularity weighting is
non-negotiable per spec."""

from collections import defaultdict

import numpy as np

from app.services.coordination.types import SignalPost, pair_key


def _targets(post: SignalPost) -> set[str]:
    targets: set[str] = set()
    if post.reshared_post_id:
        targets.add(f"reshare:{post.reshared_post_id}")
    if post.quoted_post_id:
        targets.add(f"quote:{post.quoted_post_id}")
    if post.replied_to_post_id:
        targets.add(f"reply:{post.replied_to_post_id}")
    targets.update(f"url:{u}" for u in post.outbound_urls)
    targets.update(f"domain:{d}" for d in post.outbound_domains)
    return targets


def compute_co_amplification(posts: list[SignalPost]) -> dict[tuple[str, str], float]:
    """Returns w_amp(i,j) in [0,1]: cosine similarity of TF-IDF-weighted target
    incidence rows (targets = reshares, quotes, replies, outbound links/domains)."""
    account_targets: dict[str, set[str]] = defaultdict(set)
    for p in posts:
        account_targets[p.account_id] |= _targets(p)

    accounts = [a for a, targets in account_targets.items() if targets]
    if len(accounts) < 2:
        return {}

    all_targets = sorted({t for targets in account_targets.values() for t in targets})
    target_index = {t: i for i, t in enumerate(all_targets)}

    incidence = np.zeros((len(accounts), len(all_targets)), dtype=np.float64)
    for a_idx, account in enumerate(accounts):
        for t in account_targets[account]:
            incidence[a_idx, target_index[t]] = 1.0

    document_frequency = incidence.sum(axis=0)
    idf = np.log((1 + len(accounts)) / (1 + document_frequency)) + 1.0
    weighted = incidence * idf

    norms = np.linalg.norm(weighted, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = weighted / norms
    similarity = normalized @ normalized.T

    results: dict[tuple[str, str], float] = {}
    n = len(accounts)
    for i in range(n):
        for j in range(i + 1, n):
            score = float(np.clip(similarity[i, j], 0.0, 1.0))
            if score > 0:
                results[pair_key(accounts[i], accounts[j])] = round(score, 4)
    return results
