"""Tests for praxis.behavior.repeat_task.

US-014 acceptance criteria:
  - tokenize(s) is lowercase whitespace-split tokens with a documented
    English stopword list removed.
  - overlap_ratio(a, b) = |a&b| / max(|a|, |b|).
  - detect_repeats(clusters, window_days) returns RepeatTask entries
    for any cluster whose first-sentence overlap_ratio >= 0.70 with 2+
    other clusters in the window.
  - Clusters with first sentences of fewer than 3 non-stopword tokens
    are excluded from comparison.

US-015 acceptance criteria:
  - RepeatTask carries estimated_minutes_per_occurrence, computed as
    the median session duration across the cluster (across every session
    in every cluster of the recurring group).
  - detect_repeats returns [] when no cluster recurs 3+ times.
  - detect_repeats([], window_days) returns [] without raising.
"""
from __future__ import annotations

import pytest

from praxis.behavior.repeat_task import (
    MIN_OTHER_CLUSTERS_FOR_REPEAT,
    MIN_TOKENS_FOR_COMPARISON,
    OVERLAP_THRESHOLD,
    STOPWORDS,
    Cluster,
    RepeatTask,
    detect_repeats,
    overlap_ratio,
    tokenize,
)


# --- tokenize ----------------------------------------------------------------


def test_tokenize_lowercases_and_splits_on_whitespace() -> None:
    assert tokenize("Refactor Auth Module") == {"refactor", "auth", "module"}


def test_tokenize_removes_documented_stopwords() -> None:
    # Every removed token is in STOPWORDS; the remainder is the content.
    result = tokenize("I want to refactor the auth module")
    assert result == {"want", "refactor", "auth", "module"}
    # The removed tokens are all documented stopwords.
    for removed in {"i", "to", "the"}:
        assert removed in STOPWORDS


def test_tokenize_strips_surrounding_punctuation() -> None:
    # First user turns commonly end with "?" or ",".
    assert tokenize("Fix the auth bug?") == {"fix", "auth", "bug"}
    assert tokenize("Refactor, please.") == {"refactor", "please"}


def test_tokenize_empty_string_returns_empty_set() -> None:
    assert tokenize("") == set()


def test_tokenize_all_stopwords_returns_empty_set() -> None:
    assert tokenize("I have a") == set()


# --- overlap_ratio -----------------------------------------------------------


def test_overlap_ratio_identical_sets_is_one() -> None:
    assert overlap_ratio({"a", "b", "c"}, {"a", "b", "c"}) == 1.0


def test_overlap_ratio_disjoint_sets_is_zero() -> None:
    assert overlap_ratio({"a", "b"}, {"c", "d"}) == 0.0


def test_overlap_ratio_formula_uses_max_denominator() -> None:
    # |{a,b,c} & {a,b}| = 2; max(3, 2) = 3 => 2/3.
    ratio = overlap_ratio({"a", "b", "c"}, {"a", "b"})
    assert ratio == pytest.approx(2.0 / 3.0)


def test_overlap_ratio_empty_side_returns_zero() -> None:
    assert overlap_ratio(set(), {"a", "b"}) == 0.0
    assert overlap_ratio({"a", "b"}, set()) == 0.0
    assert overlap_ratio(set(), set()) == 0.0


def test_overlap_ratio_threshold_constant_is_seventy_percent() -> None:
    # The spec threshold is 0.70 - guard against silent edits.
    assert OVERLAP_THRESHOLD == 0.70


# --- detect_repeats ----------------------------------------------------------


def _cluster(
    first_sentence: str,
    *session_ids: str,
    duration_minutes: float = 10.0,
) -> Cluster:
    """Build a Cluster, defaulting every session's duration to the same value.

    Most US-014 tests do not care about durations and only need the
    Cluster to be constructible; the default keeps them terse. Tests that
    exercise estimated_minutes_per_occurrence should call
    `_cluster_with_durations` instead so the per-session values are
    explicit in the test body.
    """
    return Cluster(
        first_sentence=first_sentence,
        session_ids=list(session_ids),
        session_durations_minutes=[duration_minutes] * len(session_ids),
    )


def _cluster_with_durations(
    first_sentence: str,
    sessions: list[tuple[str, float]],
) -> Cluster:
    """Build a Cluster from an explicit list of (session_id, duration_minutes) pairs."""
    return Cluster(
        first_sentence=first_sentence,
        session_ids=[sid for sid, _ in sessions],
        session_durations_minutes=[dur for _, dur in sessions],
    )


def test_detect_repeats_finds_three_clusters_with_high_overlap() -> None:
    clusters = [
        _cluster("refactor the auth module for tenants", "s1"),
        _cluster("refactor auth module tenants today", "s2"),
        _cluster("refactor the auth module again", "s3"),
        _cluster("write the deckgen export pipeline", "s4"),
    ]
    repeats = detect_repeats(clusters, window_days=7)
    assert len(repeats) == 1
    assert repeats[0].occurrences == 3
    # canonical is the seed (first encountered) cluster's first sentence.
    assert repeats[0].canonical_first_sentence == clusters[0].first_sentence
    assert repeats[0].example_session_ids == ["s1", "s2", "s3"]


def test_detect_repeats_skips_groups_smaller_than_three() -> None:
    # Two clusters with high overlap is NOT enough; we require 3+.
    clusters = [
        _cluster("debug the slow query in the dashboard", "s1"),
        _cluster("debug the slow query in dashboard yet again", "s2"),
    ]
    assert detect_repeats(clusters, window_days=7) == []
    # The minimum-other-clusters threshold is the spec's "2+ others".
    assert MIN_OTHER_CLUSTERS_FOR_REPEAT == 2


def test_detect_repeats_short_first_sentences_are_excluded_from_comparison() -> None:
    # First sentences with < MIN_TOKENS_FOR_COMPARISON content tokens (after
    # stopword removal) must not participate in matching - otherwise three
    # short prompts like "fix this" / "fix that" / "fix it" would all
    # collapse into a single recurring task even though they share only one
    # content token ("fix"). The detector excludes them entirely so the
    # group never forms.
    short = "fix this"  # tokenize -> {"fix"} -> too short
    assert len(tokenize(short)) < MIN_TOKENS_FOR_COMPARISON
    clusters = [
        _cluster(short, "s1"),
        _cluster("fix that", "s2"),
        _cluster("fix it now", "s3"),
        _cluster("fix please", "s4"),
    ]
    assert detect_repeats(clusters, window_days=7) == []


def test_detect_repeats_distinct_tasks_emit_separate_repeats() -> None:
    # Two distinct recurring tasks in the window must each get their own
    # RepeatTask entry.
    clusters = [
        _cluster("refactor the auth module for tenants", "a1"),
        _cluster("refactor auth module again tenants", "a2"),
        _cluster("refactor the auth module yet again", "a3"),
        _cluster("debug the slow dashboard query timing", "b1"),
        _cluster("debug slow dashboard query again", "b2"),
        _cluster("debug the slow dashboard query timing yet", "b3"),
    ]
    repeats = detect_repeats(clusters, window_days=7)
    assert len(repeats) == 2
    canonicals = {r.canonical_first_sentence for r in repeats}
    assert clusters[0].first_sentence in canonicals
    assert clusters[3].first_sentence in canonicals


def test_detect_repeats_below_threshold_overlap_does_not_group() -> None:
    # The two clusters share exactly one content token out of four+ on
    # each side, so the ratio is well below 0.70 and they should not be
    # grouped together with a third overlapping cluster.
    clusters = [
        _cluster("refactor the auth module for tenants", "a1"),
        _cluster("refactor a totally different unrelated thing", "a2"),
        _cluster("refactor the auth module once more", "a3"),
    ]
    repeats = detect_repeats(clusters, window_days=7)
    # a1 and a3 share 3 content tokens; ratio = 3/5 = 0.60, below 0.70.
    # No group of 3+ should emerge.
    assert repeats == []


def test_detect_repeats_aggregates_session_ids_from_each_cluster() -> None:
    # Token sets:
    #   c1: {refactor, auth, module, tenants}     (4 tokens)
    #   c2: {refactor, auth, module}              (3 tokens)
    #   c3: {refactor, auth, module, tenants}     (4 tokens)
    # Pairwise overlaps: c1<->c2 = 3/4 = 0.75; c1<->c3 = 4/4 = 1.0;
    # c2<->c3 = 3/4 = 0.75. All clear OVERLAP_THRESHOLD, so the three
    # clusters form one recurring group.
    clusters = [
        _cluster("refactor auth module tenants", "s1", "s1b"),
        _cluster("refactor auth module", "s2"),
        _cluster("refactor auth module tenants", "s3", "s3b"),
    ]
    repeats = detect_repeats(clusters, window_days=7)
    assert len(repeats) == 1
    assert repeats[0].example_session_ids == ["s1", "s1b", "s2", "s3", "s3b"]


def test_detect_repeats_invalid_window_days_raises() -> None:
    with pytest.raises(ValueError):
        detect_repeats([], window_days=0)
    with pytest.raises(ValueError):
        detect_repeats([], window_days=-1)


def test_repeat_task_is_a_frozen_dataclass() -> None:
    # The RepeatTask result is meant to be safe to share across the
    # report renderers; freezing it prevents downstream mutation.
    task = RepeatTask(
        canonical_first_sentence="x",
        occurrences=3,
        example_session_ids=["a"],
        estimated_minutes_per_occurrence=10.0,
    )
    with pytest.raises(Exception):
        task.occurrences = 99  # type: ignore[misc]


# --- US-015: estimated_minutes_per_occurrence -------------------------------


def test_cluster_rejects_mismatched_durations_length() -> None:
    # The two parallel lists drive the median in detect_repeats; if a
    # caller can hand us a Cluster where session_ids and durations have
    # different lengths, we silently lose data. The constructor must
    # refuse the mismatch up front.
    with pytest.raises(ValueError):
        Cluster(
            first_sentence="refactor the auth module",
            session_ids=["s1", "s2"],
            session_durations_minutes=[10.0],
        )


def test_estimated_minutes_is_median_across_all_sessions_in_group() -> None:
    # Three clusters in one recurring group, with explicit per-session
    # durations. All seven sessions feed the median:
    #   sorted: [3, 5, 8, 10, 12, 15, 20]  -> median = 10.0 (middle value)
    clusters = [
        _cluster_with_durations(
            "refactor the auth module for tenants",
            [("a1", 3.0), ("a2", 20.0)],
        ),
        _cluster_with_durations(
            "refactor auth module tenants again",
            [("b1", 5.0), ("b2", 15.0), ("b3", 10.0)],
        ),
        _cluster_with_durations(
            "refactor the auth module yet again",
            [("c1", 8.0), ("c2", 12.0)],
        ),
    ]
    repeats = detect_repeats(clusters, window_days=7)
    assert len(repeats) == 1
    assert repeats[0].estimated_minutes_per_occurrence == 10.0


def test_estimated_minutes_uses_median_average_for_even_session_count() -> None:
    # Six sessions across three clusters; sorted durations:
    #   [4, 6, 8, 10, 12, 14]  -> median = (8 + 10) / 2 = 9.0
    clusters = [
        _cluster_with_durations(
            "debug the slow dashboard query timing",
            [("a1", 4.0), ("a2", 14.0)],
        ),
        _cluster_with_durations(
            "debug slow dashboard query again",
            [("b1", 6.0), ("b2", 12.0)],
        ),
        _cluster_with_durations(
            "debug the slow dashboard query yet",
            [("c1", 8.0), ("c2", 10.0)],
        ),
    ]
    repeats = detect_repeats(clusters, window_days=7)
    assert len(repeats) == 1
    assert repeats[0].estimated_minutes_per_occurrence == 9.0


def test_detect_repeats_empty_clusters_returns_empty_without_raising() -> None:
    # The report layer hands the detector an empty cluster list when a
    # week's sessions were all uncluster-able; we must return [] cleanly
    # so callers do not need to special-case the empty path.
    assert detect_repeats([], window_days=7) == []


def test_detect_repeats_no_recurrence_returns_empty_list() -> None:
    # Three completely unrelated clusters in the window; nothing should
    # be flagged as a recurring task and the report layer must NOT
    # receive a fabricated "you did X 3+ times" suggestion.
    clusters = [
        _cluster("refactor the auth module for tenants", "s1"),
        _cluster("write the deckgen export pipeline", "s2"),
        _cluster("debug the slow dashboard query timing", "s3"),
    ]
    assert detect_repeats(clusters, window_days=7) == []


def test_detect_repeats_no_recurrence_when_only_two_clusters_match() -> None:
    # Two clusters with high overlap is not a recurrence (spec requires
    # 2+ OTHER clusters, i.e. 3+ total). The report layer must not see
    # a half-formed RepeatTask under any circumstance.
    clusters = [
        _cluster("refactor the auth module for tenants", "s1"),
        _cluster("refactor auth module tenants today", "s2"),
        _cluster("write the deckgen export pipeline", "s3"),
    ]
    assert detect_repeats(clusters, window_days=7) == []
