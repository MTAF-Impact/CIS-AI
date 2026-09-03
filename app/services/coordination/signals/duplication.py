"""Signal: content duplication (w_text). Two sub-signals on normalised text:
near-duplicate (MinHash/LSH) and semantic paraphrase (multilingual embeddings) -
the latter catches machine-rewritten variants MinHash misses."""

import re
from collections import defaultdict

import numpy as np
from datasketch import MinHash, MinHashLSH

from app.services.coordination.types import SignalPost, pair_key
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

DEFAULT_TAU_DUP = 0.80
DEFAULT_TAU_SEM = 0.90
DEFAULT_L_MIN = 25
MINHASH_NUM_PERM = 128
SHINGLE_SIZE = 5

_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_REPEATED_PUNCT_RE = re.compile(r"([!?.,])\1+")
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]+", flags=re.UNICODE
)


def normalize_text(text: str) -> str:
    """Lowercased, URLs/@-mentions placeholder'd, emoji/repeated punctuation
    stripped, whitespace collapsed - catches the common evasion of varying only
    punctuation and emoji."""
    normalized = text.lower()
    normalized = _URL_RE.sub("<url>", normalized)
    normalized = _MENTION_RE.sub("<mention>", normalized)
    normalized = _EMOJI_RE.sub("", normalized)
    normalized = _REPEATED_PUNCT_RE.sub(r"\1", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _shingles(text: str, k: int = SHINGLE_SIZE) -> set[str]:
    if len(text) < k:
        return {text} if text else set()
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _minhash(text: str) -> MinHash:
    m = MinHash(num_perm=MINHASH_NUM_PERM)
    for shingle in _shingles(text):
        m.update(shingle.encode("utf8"))
    return m


def find_duplicate_post_pairs(
    posts: list[SignalPost],
    common_phrase_allowlist: set[str] | None = None,
    tau_dup: float = DEFAULT_TAU_DUP,
    tau_sem: float = DEFAULT_TAU_SEM,
    l_min: int = DEFAULT_L_MIN,
    embedder: MultilingualEmbeddingService | None = None,
) -> tuple[list[SignalPost], set[tuple[int, int]]]:
    """Shared primitive: applies exclusions, then flags duplicate post pairs via
    MinHash/LSH or multilingual semantic similarity. Returns (eligible_posts,
    index_pairs), where index_pairs are positions into eligible_posts."""
    allowlist = common_phrase_allowlist or set()
    embedder = embedder or get_multilingual_embedding_service()

    # Exclude native reshares, posts shorter than L_min, and allowlisted phrases.
    eligible: list[SignalPost] = []
    for p in posts:
        if p.is_native_reshare:
            continue
        normalized = normalize_text(p.text)
        if len(normalized) < l_min or normalized in allowlist:
            continue
        eligible.append(p)

    if len(eligible) < 2:
        return eligible, set()

    normalized_texts = [normalize_text(p.text) for p in eligible]

    # 2a - near-duplicate via MinHash/LSH.
    lsh = MinHashLSH(threshold=tau_dup, num_perm=MINHASH_NUM_PERM)
    minhashes = [_minhash(t) for t in normalized_texts]
    for idx, mh in enumerate(minhashes):
        lsh.insert(str(idx), mh)

    duplicate_pairs: set[tuple[int, int]] = set()
    for idx, mh in enumerate(minhashes):
        for match in lsh.query(mh):
            match_idx = int(match)
            if match_idx != idx:
                duplicate_pairs.add((min(idx, match_idx), max(idx, match_idx)))

    # 2b - semantic paraphrase via multilingual embeddings.
    vectors = np.array(embedder.embed_batch(normalized_texts))
    similarity = vectors @ vectors.T  # embedder L2-normalizes, so dot product = cosine
    sem_i, sem_j = np.where(np.triu(similarity, k=1) >= tau_sem)
    for i, j in zip(sem_i.tolist(), sem_j.tolist(), strict=True):
        duplicate_pairs.add((i, j))

    return eligible, duplicate_pairs


def compute_content_duplication(
    posts: list[SignalPost],
    common_phrase_allowlist: set[str] | None = None,
    tau_dup: float = DEFAULT_TAU_DUP,
    tau_sem: float = DEFAULT_TAU_SEM,
    l_min: int = DEFAULT_L_MIN,
    embedder: MultilingualEmbeddingService | None = None,
) -> dict[tuple[str, str], float]:
    """Returns w_text(i,j) in [0,1]: share of each account's (eligible) posts that
    duplicate a post by the other account."""
    eligible, duplicate_pairs = find_duplicate_post_pairs(
        posts, common_phrase_allowlist, tau_dup, tau_sem, l_min, embedder
    )
    if not duplicate_pairs:
        return {}

    post_counts: dict[str, int] = defaultdict(int)
    for p in eligible:
        post_counts[p.account_id] += 1

    account_pair_dup_counts: dict[tuple[str, str], int] = defaultdict(int)
    for i, j in duplicate_pairs:
        acc_i, acc_j = eligible[i].account_id, eligible[j].account_id
        if acc_i != acc_j:
            account_pair_dup_counts[pair_key(acc_i, acc_j)] += 1

    results: dict[tuple[str, str], float] = {}
    for (acc_i, acc_j), dup_count in account_pair_dup_counts.items():
        denom = min(post_counts[acc_i], post_counts[acc_j])
        if denom <= 0:
            continue
        score = max(0.0, min(1.0, dup_count / denom))
        if score > 0:
            results[(acc_i, acc_j)] = round(score, 4)
    return results
