"""PRD 10.5.2.4 - Signal 4: provenance & identity similarity (w_meta). Weak alone -
new accounts and heavy posters are both ordinary - and deliberately low-weighted in
fusion. Missing fields are absent, never zero-similarity: conflating "unknown" with
"different" would systematically depress scores for platforms with sparser metadata."""

import math
import re

from app.services.coordination.types import SignalAccount, pair_key

DEFAULT_CREATION_HALF_LIFE_HOURS = 36.0
HANDLE_TEMPLATE_RE = re.compile(r"^(?P<prefix>[a-z]+)_(?P<suffix>[a-z]+)(?P<digits>\d{2,4})$")


def _creation_time_proximity(
    a: SignalAccount, b: SignalAccount, half_life_hours: float
) -> float | None:
    if a.created_at_platform is None or b.created_at_platform is None:
        return None
    delta_hours = abs((a.created_at_platform - b.created_at_platform).total_seconds()) / 3600
    return math.exp(-delta_hours / half_life_hours)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i, *([0] * len(b))]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _handle_pattern_similarity(a: SignalAccount, b: SignalAccount) -> float | None:
    if not a.handle or not b.handle:
        return None
    handle_a, handle_b = a.handle.lower(), b.handle.lower()
    max_len = max(len(handle_a), len(handle_b))
    edit_similarity = 1.0 - (_levenshtein(handle_a, handle_b) / max_len) if max_len else 0.0

    template_bonus = 0.0
    match_a, match_b = HANDLE_TEMPLATE_RE.match(handle_a), HANDLE_TEMPLATE_RE.match(handle_b)
    if match_a and match_b and len(match_a.group("digits")) == len(match_b.group("digits")):
        template_bonus = 0.5
    elif handle_a[:3] == handle_b[:3] or handle_a[-3:] == handle_b[-3:]:
        template_bonus = 0.25

    return max(0.0, min(1.0, 0.5 * edit_similarity + template_bonus))


def _profile_image_similarity(a: SignalAccount, b: SignalAccount) -> float | None:
    if not a.profile_hash or not b.profile_hash or len(a.profile_hash) != len(b.profile_hash):
        return None
    try:
        bits = 4 * len(a.profile_hash)
        bits_a = bin(int(a.profile_hash, 16))[2:].zfill(bits)
        bits_b = bin(int(b.profile_hash, 16))[2:].zfill(bits)
    except ValueError:
        return None
    hamming = sum(1 for x, y in zip(bits_a, bits_b, strict=True) if x != y)
    return 1.0 - (hamming / bits)


def _bio_similarity(a: SignalAccount, b: SignalAccount, n: int = 4) -> float | None:
    if not a.bio or not b.bio:
        return None
    bio_a, bio_b = a.bio.lower(), b.bio.lower()
    grams_a = {bio_a[i : i + n] for i in range(max(1, len(bio_a) - n + 1))}
    grams_b = {bio_b[i : i + n] for i in range(max(1, len(bio_b) - n + 1))}
    union = grams_a | grams_b
    return len(grams_a & grams_b) / len(union) if union else None


def _exact_match(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return 1.0 if a == b else 0.0


def compute_provenance_similarity(
    accounts: list[SignalAccount],
    half_life_hours: float = DEFAULT_CREATION_HALF_LIFE_HOURS,
) -> dict[tuple[str, str], float]:
    """Returns w_meta(i,j): mean of the available sub-signals, renormalised over
    availability (never counting a missing field as zero)."""
    results: dict[tuple[str, str], float] = {}
    n = len(accounts)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = accounts[i], accounts[j]
            sub_scores = [
                s
                for s in (
                    _creation_time_proximity(a, b, half_life_hours),
                    _handle_pattern_similarity(a, b),
                    _profile_image_similarity(a, b),
                    _bio_similarity(a, b),
                    _exact_match(a.declared_location, b.declared_location),
                    _exact_match(a.client_app, b.client_app),
                )
                if s is not None
            ]
            if not sub_scores:
                continue
            score = round(sum(sub_scores) / len(sub_scores), 4)
            if score > 0:
                results[pair_key(a.account_id, b.account_id)] = score
    return results
