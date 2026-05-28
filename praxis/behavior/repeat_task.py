"""Token-overlap repeat-task detector.

Identifies recurring tasks within a window of clustered work so the
weekly digest can surface "you did this 3+ times - a skill could
reclaim ~X min/week" hints.

Matching is intentionally cheap and interpretable: the first user
sentence of each cluster is tokenized (lowercase, whitespace-split,
common English stopwords removed) and two clusters are considered the
same recurring task when their token sets overlap with ratio
|A intersect B| / max(|A|, |B|) >= 0.70. Clusters whose first sentence
yields fewer than MIN_TOKENS_FOR_COMPARISON content tokens are too
short to overlap meaningfully and are excluded from comparison.

A RepeatTask is emitted for each connected component of clusters
linked by 0.70+ pairwise overlap that contains at least three
clusters (the seed plus two or more others).
"""
from __future__ import annotations

from dataclasses import dataclass


# Documented English stopword list. Kept deliberately small: aggressive
# stopword removal collapses distinct tasks (e.g., "fix the auth bug" and
# "fix the cron job" already overlap on "fix" if we leave verbs in). These
# are the high-frequency function words a first user turn typically
# contains regardless of the underlying task.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "been", "but",
        "by", "can", "could", "did", "do", "does", "for", "from", "had",
        "has", "have", "i", "if", "in", "into", "is", "it", "its", "just",
        "me", "my", "no", "not", "of", "on", "or", "should", "so", "than",
        "that", "the", "their", "them", "then", "there", "these", "they",
        "this", "to", "was", "we", "were", "what", "when", "which", "who",
        "why", "will", "with", "would", "you", "your",
    }
)


# Two clusters with very short first sentences (under three content tokens
# after stopword removal) cannot overlap meaningfully: any single shared
# token would push the ratio above the threshold. Excluding them keeps the
# detector from grouping unrelated short prompts like "fix this" and
# "fix that" into a single recurring task.
MIN_TOKENS_FOR_COMPARISON: int = 3


# Spec: overlap_ratio >= 0.70 between two clusters' first-sentence token
# sets is the same-task threshold.
OVERLAP_THRESHOLD: float = 0.70


# A RepeatTask requires the seed cluster plus at least this many other
# clusters in the window. The total occurrence count is therefore
# MIN_OTHER_CLUSTERS_FOR_REPEAT + 1 (i.e., 3+).
MIN_OTHER_CLUSTERS_FOR_REPEAT: int = 2


# Punctuation stripped from each whitespace-split token before stopword
# removal. Real first user turns end with "?", ".", trailing commas, etc.;
# without this strip "fix?" and "fix" would be different tokens.
_TOKEN_PUNCTUATION: str = ".,!?;:\"'`()[]{}"


@dataclass(frozen=True)
class Cluster:
    """One unit of work to test for recurrence.

    `first_sentence` is the speaker's actual words from a representative
    session's first user turn (the caller decides which session to draw
    from - typically the earliest in the cluster). `session_ids` are the
    stable_ids of every session in the cluster, used to populate
    `example_session_ids` on the matching RepeatTask.
    """

    first_sentence: str
    session_ids: list[str]


@dataclass(frozen=True)
class RepeatTask:
    """A task the user has worked on 3+ times in the window.

    `canonical_first_sentence` is the first-sentence text of the seed
    cluster - the cluster that anchors the recurring group when it is
    first detected. `occurrences` counts clusters in the group, not
    underlying sessions: two clusters that each bundled three sessions
    of the same work still count as two occurrences. `example_session_ids`
    aggregates the session ids across all clusters in the group, in the
    order the clusters were encountered.
    """

    canonical_first_sentence: str
    occurrences: int
    example_session_ids: list[str]


def tokenize(s: str) -> set[str]:
    """Return the lowercase, whitespace-split content tokens of `s`.

    Each token is lowercased, has surrounding punctuation stripped, and
    is dropped if it is empty or appears in STOPWORDS. The result is a
    set (order does not matter for overlap_ratio).
    """
    tokens: set[str] = set()
    for raw in s.lower().split():
        word = raw.strip(_TOKEN_PUNCTUATION)
        if not word or word in STOPWORDS:
            continue
        tokens.add(word)
    return tokens


def overlap_ratio(a: set[str], b: set[str]) -> float:
    """Token-set overlap: |A intersect B| / max(|A|, |B|).

    Returns 0.0 when either side is empty; an empty set cannot
    meaningfully overlap with anything.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def detect_repeats(
    clusters: list[Cluster], window_days: int
) -> list[RepeatTask]:
    """Return one RepeatTask per recurring cluster group in the window.

    Two clusters are linked when their first-sentence token sets overlap
    at OVERLAP_THRESHOLD or higher. Groups are the transitive closure of
    that linking (union-find). A group becomes a RepeatTask only when it
    contains at least three clusters - the seed plus at least
    MIN_OTHER_CLUSTERS_FOR_REPEAT other clusters per spec.

    Clusters whose first sentence yields fewer than
    MIN_TOKENS_FOR_COMPARISON content tokens are excluded from comparison
    entirely; they neither anchor a group nor link to one. This prevents
    accidental matches between unrelated short prompts.

    `window_days` is the time window the caller already filtered the
    clusters down to. The detector trusts the caller's window selection
    rather than re-filtering by timestamp here; the parameter is part of
    the public surface so callers cannot omit it (and so the report
    layer can render the window length alongside each repeat). A
    non-positive window is rejected since a recurring task across zero
    days is not a meaningful concept.
    """
    if window_days <= 0:
        raise ValueError(
            f"window_days must be positive, got {window_days}"
        )
    if not clusters:
        return []

    token_sets: list[set[str]] = [tokenize(c.first_sentence) for c in clusters]
    eligible: list[bool] = [
        len(t) >= MIN_TOKENS_FOR_COMPARISON for t in token_sets
    ]

    n = len(clusters)
    parent: list[int] = list(range(n))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression for the next find.
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(n):
        if not eligible[i]:
            continue
        for j in range(i + 1, n):
            if not eligible[j]:
                continue
            if overlap_ratio(token_sets[i], token_sets[j]) >= OVERLAP_THRESHOLD:
                union(i, j)

    # Group cluster indices by root, preserving encounter order so the
    # seed cluster (lowest index in the group) is first.
    groups: dict[int, list[int]] = {}
    for i in range(n):
        if not eligible[i]:
            continue
        root = find(i)
        groups.setdefault(root, []).append(i)

    repeats: list[RepeatTask] = []
    for root, indices in groups.items():
        if len(indices) < MIN_OTHER_CLUSTERS_FOR_REPEAT + 1:
            continue
        seed = clusters[indices[0]]
        example_ids: list[str] = []
        for idx in indices:
            example_ids.extend(clusters[idx].session_ids)
        repeats.append(
            RepeatTask(
                canonical_first_sentence=seed.first_sentence,
                occurrences=len(indices),
                example_session_ids=example_ids,
            )
        )

    return repeats


__all__ = [
    "Cluster",
    "MIN_OTHER_CLUSTERS_FOR_REPEAT",
    "MIN_TOKENS_FOR_COMPARISON",
    "OVERLAP_THRESHOLD",
    "RepeatTask",
    "STOPWORDS",
    "detect_repeats",
    "overlap_ratio",
    "tokenize",
]
