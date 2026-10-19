"""The general effect across repositories: design 03 section 6.4's multiple baseline.

Each repository is all control until its start week, drawn once from its candidate
weeks (task.draw_start, the repository_starts table). Its series is the weekly mean
Brier score of first probes of never-drawn items, the items no task exposed, so the
arms of its tasks cannot reach them. scoring.koehler_levin tests the level change at the
start weeks over every combination of candidate weeks. A week with no probe is not in
the series, so a start point is the count of weeks with probes before it. A candidate
that empties a phase is dropped (design 6.4). Two candidates with one split stand for
two assignments that koehler_levin cannot weight, so their repository is left out and
counted, never folded into one.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections import Counter
from typing import TYPE_CHECKING

from praxis import scoring
from praxis.render import General
from praxis.task import week_of

if TYPE_CHECKING:
    import datetime as dt
    import sqlite3
    from collections.abc import Mapping, Sequence

# Design 6.4: below 3 repositories the general effect is descriptive only.
MIN_REPOSITORIES = 3
# A series of 2 weeks has one split: no randomization distribution to speak of.
MIN_WEEKS = 3
Unit = tuple[list[float], int, list[int]]  # (series, start point, eligible points)


def effect(
    starts: Sequence[sqlite3.Row],
    probes: Mapping[str, Sequence[tuple[dt.datetime, float]] | str],
    seed: int,
    draws: int,
) -> General:
    """``probes`` maps each repository with a start to its (time, Brier) first probes
    of never-drawn items, or to the reason code it has none (no set map, say)."""
    series: dict[str, list[float]] = {}
    points: dict[str, int] = {}
    eligible: dict[str, list[int]] = {}
    left: Counter[str] = Counter()
    for start in starts:
        repo_id = str(start["repo_id"])
        found = probes[repo_id]
        unit = found if isinstance(found, str) else _unit(start, found)
        if isinstance(unit, str):
            left[unit] += 1
            continue
        series[repo_id], points[repo_id], eligible[repo_id] = unit
    p = None
    if len(series) >= MIN_REPOSITORIES:
        p = scoring.koehler_levin(series, points, seed, draws, eligible=eligible)
    return General(p, len(series), dict(sorted(left.items())), run=True)


def _unit(
    start: sqlite3.Row, probes: Sequence[tuple[dt.datetime, float]]
) -> Unit | str:
    weeks: dict[str, list[float]] = {}
    for when, brier in probes:
        weeks.setdefault(week_of(when), []).append(brier)
    order = sorted(weeks)
    n = len(order)
    point = bisect_left(order, start["start_week"])
    splits = [bisect_left(order, c) for c in json.loads(start["candidates_json"])]
    kept = [s for s in splits if 0 < s < n]
    checks = (
        ("fewer_than_3_weeks", n < MIN_WEEKS),
        ("no_week_before_start", point == 0),
        ("no_week_after_start", point == n),
        ("candidates_share_a_split", len(set(kept)) != len(kept)),
    )
    reason = next((code for code, hit in checks if hit), None)
    if reason is not None:
        return reason
    return [math.fsum(weeks[w]) / len(weeks[w]) for w in order], point, kept
