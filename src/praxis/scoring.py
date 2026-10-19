"""Proper scores, their decomposition and the small-n tests, as pure functions.

No I/O and no state: every function takes numbers and returns numbers, and refuses
(ValueError) malformed input at the boundary rather than guessing. An answer that the
data cannot give is None, never 0: a skill score against a perfect reference, a
reliability with too few weeks, a similarity between two networks without links.

Outcomes are 0 or 1. Lower Brier is better, so every test statistic is oriented so that
a larger value favours the treatment (control minus treatment, before minus after).
"""

from __future__ import annotations

import math
import random
from itertools import combinations, product
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

GRID = (0.1, 0.3, 0.5, 0.7, 0.9)
EXACT_MAX = 70
# Statistics closer than this are ties: re-summing the same Brier values in another
# order moves the last bits, and the observed allocation must count itself.
_TIE = 1e-12


def _probability(p: float) -> float:
    if not 0.0 <= p <= 1.0:
        raise ValueError("probability outside [0, 1]")
    return p


def _outcome(o: int) -> int:
    if o not in (0, 1):
        raise ValueError("outcome is not 0 or 1")
    return o


def _pairs(ps: Sequence[float], os: Sequence[int]) -> list[tuple[float, int]]:
    pairs = [(_probability(p), _outcome(o)) for p, o in zip(ps, os, strict=True)]
    if not pairs:
        raise ValueError("no forecasts")
    return pairs


def _bins(ps: Sequence[float], os: Sequence[int]) -> dict[float, tuple[int, int]]:
    """Forecast value -> (forecasts, events), in ascending forecast order."""
    bins: dict[float, tuple[int, int]] = {}
    for p, o in sorted(_pairs(ps, os)):
        n, y = bins.get(p, (0, 0))
        bins[p] = (n + 1, y + o)
    return bins


def snap(p: float) -> float:
    """Nearest grid point; an exact midpoint goes to the point nearer 0.5."""
    _probability(p)
    return min(GRID, key=lambda g: (round(abs(g - p), 9), abs(g - 0.5)))


def brier(p: float, o: int) -> float:
    return (_probability(p) - _outcome(o)) ** 2


def brier_sum(ps: Sequence[float], os: Sequence[int]) -> float:
    """Summed binary Brier: proper for the vector of marginals, blind to dependence."""
    return sum((p - o) ** 2 for p, o in _pairs(ps, os))


def bss(bs_owner: float, bs_ref: float) -> float | None:
    return None if bs_ref == 0 else 1 - bs_owner / bs_ref


def decomposition(ps: Sequence[float], os: Sequence[int]) -> tuple[float, float, float]:
    """(REL, RES, UNC), bias-corrected after Ferro and Fricker (2012), as written in
    Siegert (2013, arXiv:1303.6182, eqs. 20-22). With A_d forecasts and B_d events in
    grid bin d, N forecasts and Y events:

        REL' = REL - (1/N) sum_{A_d > 1} B_d (A_d - B_d) / (A_d (A_d - 1))
        RES' = RES - (1/N) sum_{A_d > 1} B_d (A_d - B_d) / (A_d (A_d - 1))
                   + Y (N - Y) / (N^2 (N - 1))
        UNC' = Y (N - Y) / (N (N - 1))

    REL' - RES' + UNC' equals the mean Brier score exactly. REL' and RES' can be
    negative; they are returned as computed, never clipped.
    """
    if any(p not in GRID for p in ps):
        raise ValueError("decomposition bins are the five grid values")
    bins = _bins(ps, os)
    n = sum(a for a, _ in bins.values())
    y = sum(b for _, b in bins.values())
    if n < 2:
        raise ValueError("the corrected uncertainty needs at least 2 forecasts")
    base = y / n
    rel = sum(a * (p - b / a) ** 2 for p, (a, b) in bins.items()) / n
    res = sum(a * (b / a - base) ** 2 for a, b in bins.values()) / n
    corr = sum(b * (a - b) / (a * (a - 1)) for a, b in bins.values() if a > 1) / n
    spread = y * (n - y) / (n * n * (n - 1))
    return rel - corr, res - corr + spread, y * (n - y) / (n * (n - 1))


def corp_reliability(
    ps: Sequence[float], os: Sequence[int]
) -> list[tuple[float, float, int]]:
    """(forecast value, recalibrated event rate, forecasts at that value), ascending.

    The recalibration is isotonic regression of outcome on forecast by
    pool-adjacent-violators (Dimitriadis, Gneiting and Jordan 2021, CORP).
    """
    bins = _bins(ps, os)
    blocks: list[tuple[int, int, int]] = []  # (events, forecasts, values pooled)
    for a, b in bins.values():
        block = (b, a, 1)
        # Integer cross-multiplication decides a violation exactly, never by rounding.
        while blocks and blocks[-1][0] * block[1] > block[0] * blocks[-1][1]:
            e, f, c = blocks.pop()
            block = (block[0] + e, block[1] + f, block[2] + c)
        blocks.append(block)
    rates = [e / f for e, f, c in blocks for _ in range(c)]
    return [(p, r, a) for (p, (a, _)), r in zip(bins.items(), rates, strict=True)]


def multicategory_brier(ps: Sequence[float], chosen: int) -> float:
    """Sum over the k options of (p_i - [i == chosen])^2. The probabilities need not sum
    to 1 (one grid probability per option); the uniform 1/k forecast scores (k-1)/k.
    """
    if len(ps) < 2 or not 0 <= chosen < len(ps):
        raise ValueError("need at least 2 options and a chosen index among them")
    return sum((_probability(p) - (i == chosen)) ** 2 for i, p in enumerate(ps))


def randomization_test(
    values: Sequence[float],
    arms: Sequence[int],
    seed: int,
    draws: int,
    one_sided: bool = True,
) -> tuple[float, int]:
    """p of mean(control) - mean(treatment) under re-drawn allocations of the same
    treated count, one value per task (arm 1 treatment, 0 control). Exact enumeration
    when C(n, k) <= 70, else Monte Carlo with (1 + hits) / (draws + 1).
    Returns (p, allocations evaluated).
    """
    treated = [i for i, a in enumerate(arms) if _outcome(a) == 1]
    n, k = len(values), len(treated)
    if n != len(arms) or k in (0, n):
        raise ValueError("one arm per value, and both arms non-empty")
    total = math.fsum(values)

    def stat(idx: Sequence[int]) -> float:
        t = math.fsum(values[i] for i in idx)
        return (total - t) / (n - k) - t / k

    hit = _exceeds(stat(treated), one_sided)
    if math.comb(n, k) <= EXACT_MAX:
        allocs = list(combinations(range(n), k))
        return sum(map(hit, map(stat, allocs))) / len(allocs), len(allocs)
    if draws < 1:
        raise ValueError("Monte Carlo needs at least one draw")
    rng = random.Random(seed)
    hits = sum(hit(stat(sorted(rng.sample(range(n), k)))) for _ in range(draws))
    return (1 + hits) / (draws + 1), draws


def _exceeds(observed: float, one_sided: bool) -> Callable[[float], bool]:
    if one_sided:
        return lambda s: s >= observed - _TIE
    return lambda s: abs(s) >= abs(observed) - _TIE


def koehler_levin(
    series: dict[str, list[float]],
    start_points: dict[str, int],
    seed: int,
    draws: int,
    *,
    eligible: Mapping[str, Sequence[int]],
) -> float:
    """One-sided p of a level change at randomized start points across units.

    series[u][t] is unit u's outcome in period t (Brier: lower is better);
    start_points[u] is the first treated period actually drawn; eligible[u] is the set
    it was drawn from, uniformly and independently per unit (the rule in design 6.4).
    Statistic: mean over units of mean(before) - mean(after). The reference
    distribution is every combination of eligible start points, enumerated when there
    are at most `draws` of them, else `draws` Monte Carlo combinations.
    """
    units = sorted(series)
    _check_baselines(units, series, start_points, eligible)
    choices = [sorted(eligible[u]) for u in units]

    def stat(starts: Sequence[int]) -> float:
        gaps = []
        for u, s in zip(units, starts, strict=True):
            xs = series[u]
            gaps.append(math.fsum(xs[:s]) / s - math.fsum(xs[s:]) / (len(xs) - s))
        return math.fsum(gaps) / len(gaps)

    hit = _exceeds(stat([start_points[u] for u in units]), True)
    if math.prod(map(len, choices)) <= draws:
        combos = list(product(*choices))
        return sum(map(hit, map(stat, combos))) / len(combos)
    rng = random.Random(seed)
    hits = sum(hit(stat([rng.choice(c) for c in choices])) for _ in range(draws))
    return (1 + hits) / (draws + 1)


def _check_baselines(
    units: list[str],
    series: Mapping[str, Sequence[float]],
    start_points: Mapping[str, int],
    eligible: Mapping[str, Sequence[int]],
) -> None:
    if not units or set(units) != set(start_points) or set(units) != set(eligible):
        raise ValueError("series, start points and eligible sets name the same units")
    for u in units:
        allowed = list(eligible[u])
        if len(set(allowed)) != len(allowed) or start_points[u] not in allowed:
            raise ValueError("eligible start points are distinct and hold the start")
        if not all(1 <= s < len(series[u]) for s in allowed):
            raise ValueError("every start point leaves both phases non-empty")


def split_half(groups: Sequence[Sequence[float]]) -> float | None:
    """Spearman-Brown corrected odd/even reliability, 2r / (1 + r).

    Each group is one unit of the correlation (a week) holding its item scores in
    administration order; r is Pearson's across groups between the mean of items
    1, 3, 5, ... and the mean of items 2, 4, 6, .... None with fewer than 3 groups (two
    points always correlate at +-1), a group of fewer than 2 items, a constant half, or
    r = -1.
    """
    if len(groups) < 3 or any(len(g) < 2 for g in groups):
        return None
    odd = [math.fsum(g[0::2]) / len(g[0::2]) for g in groups]
    even = [math.fsum(g[1::2]) / len(g[1::2]) for g in groups]
    r = _pearson(odd, even)
    return None if r is None or r == -1 else 2 * r / (1 + r)


def _pearson(x: list[float], y: list[float]) -> float | None:
    # Constancy is decided on the inputs: a constant column's float mean can miss its
    # value by one ulp and leave a spurious non-zero spread.
    if max(x) == min(x) or max(y) == min(y):
        return None
    mx, my = math.fsum(x) / len(x), math.fsum(y) / len(y)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = math.fsum((a - mx) ** 2 for a in x)
    syy = math.fsum((b - my) ** 2 for b in y)
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def pfnet(distances: list[list[float]]) -> list[list[bool]]:
    """PFnet(r = infinity, q = n - 1): link i-j survives iff d_ij is no larger than the
    minimax path distance (the smallest, over paths, of the largest step), computed by
    Floyd-Warshall with max in place of sum. The diagonal is ignored.
    """
    n = len(distances)
    _check_distances(distances)
    w = [row[:] for row in distances]
    for k in range(n):
        for i in range(n):
            for j in range(n):
                w[i][j] = min(w[i][j], max(w[i][k], w[k][j]))
    return [[i != j and distances[i][j] <= w[i][j] for j in range(n)] for i in range(n)]


def _check_distances(distances: list[list[float]]) -> None:
    n = len(distances)
    if any(len(row) != n for row in distances):
        raise ValueError("distances must be square")
    off = [(i, j) for i in range(n) for j in range(n) if i != j]
    for i, j in off:
        d = distances[i][j]
        if not math.isfinite(d) or d < 0 or d != distances[j][i]:
            raise ValueError("distances must be finite, non-negative, symmetric")


def pfsim(a: list[list[bool]], b: list[list[bool]]) -> tuple[float, float] | None:
    """Schvaneveldt's network similarity S = C / (|L1| + |L2| - C): L1 and L2 are the
    undirected link sets, C their overlap (JPathfinder manual, "Network Similarity"; the
    PFSIM of Trumpower, Sharara and Goldsmith 2010). Returns (S, E[C]), where
    E[C] = |L1| |L2| / P is the hypergeometric mean overlap of two random link sets over
    the P = n(n-1)/2 pairs; the caller subtracts chance. None when neither network has a
    link. The per-node "neighbourhood closeness" of Goldsmith and Davenport (1990) is
    NOT this and stays unverified (design Q13).
    """
    if len(a) != len(b) or any(len(r) != len(a) for r in (*a, *b)):
        raise ValueError("both networks are square over the same nodes")
    la, lb = _links(a), _links(b)
    union = len(la) + len(lb) - len(la & lb)
    if union == 0:
        return None
    pairs = len(a) * (len(a) - 1) / 2
    return len(la & lb) / union, len(la) * len(lb) / pairs


def _links(net: list[list[bool]]) -> set[tuple[int, int]]:
    return {
        (u, v)
        for u, row in enumerate(net)
        for v, linked in enumerate(row)
        if u < v and (linked or net[v][u])
    }
