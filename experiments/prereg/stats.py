"""Pure statistics for derive.py: nearest-rank quantiles, the Wilson bound, Otsu's cut,
ICC(1), FIFO pairing of line removals, half-width counts and the power simulation.
Nothing here reads the ledger; derive.py holds the rules that read it, and its
`--selfcheck` checks these functions on hand-worked inputs. Split out of derive.py so
that file stays under 800 lines; no value changed in the move.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from praxis import scoring

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Design choices (docs/preregistration.md 1.4 and 1.7) the simulation and bounds use.
K_E, ALPHA, POWER, N_CAP = 2, 0.05, 0.80, 4000
Z_ONE_SIDED, Z_TWO_SIDED, HALF_WIDTH = 1.645, 1.96, 0.10


@dataclass
class Inputs:
    """The power simulation's inputs; None is not measured."""

    sd_item: float | None = None
    icc: float | None = None
    sd_week: float | None = None
    rate: float | None = None
    answer: float | None = None
    supply: int | None = None
    measured: dict[str, int] = field(default_factory=dict)


def when(ts: str) -> dt.datetime:
    if (stamp := dt.datetime.fromisoformat(ts)).tzinfo is None:
        raise ValueError("a ledger timestamp without a zone is refused")
    return stamp


def nearest_rank(xs: Sequence[float], q: float) -> float:
    ordered = sorted(xs)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def ceil_to(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


def wilson_upper(k: int, n: int, z: float = Z_ONE_SIDED) -> float:
    p, z2 = k / n, z * z
    centre = p + z2 / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (centre + half) / (1 + z2 / n)


def otsu(xs: Sequence[float]) -> float | None:
    """The cut between two classes that maximizes between-class variance, at the
    midpoint of the gap it falls in; None with one distinct value."""
    ordered = sorted(xs)
    best: tuple[float, float] | None = None
    for i in range(1, len(ordered)):
        if ordered[i] == ordered[i - 1]:
            continue
        low, high = ordered[:i], ordered[i:]
        between = (
            len(low) * len(high) * (statistics.fmean(low) - statistics.fmean(high)) ** 2
        )
        if best is None or between > best[0]:
            best = (between, (ordered[i - 1] + ordered[i]) / 2)
    return None if best is None else best[1]


def icc1(groups: Sequence[Sequence[float]]) -> float | None:
    """One-way random-effects ICC(1) for unequal group sizes (groups of 2 or more)."""
    groups = [g for g in groups if len(g) >= 2]
    g, n = len(groups), sum(len(x) for x in groups)
    if g < 2:
        return None
    grand = math.fsum(map(math.fsum, groups)) / n
    msb = math.fsum(len(x) * (statistics.fmean(x) - grand) ** 2 for x in groups) / (
        g - 1
    )
    msw = math.fsum((v - statistics.fmean(x)) ** 2 for x in groups for v in x) / (n - g)
    n0 = (n - math.fsum(len(x) ** 2 for x in groups) / n) / (g - 1)
    den = msb + (n0 - 1) * msw
    return None if den == 0 else (msb - msw) / den


def fifo_gaps(events: Sequence[tuple[str, str, int, str]]) -> list[tuple[str, float]]:
    """(added ts, seconds to removal) per agent line removed, from (file, hash, delta,
    ts) in time order. Each removed copy pairs with the earliest unpaired added copy of
    the same line in the same file; a removal with no added copy is not an S4 event."""
    queues: dict[tuple[str, str], deque[str]] = defaultdict(deque)
    gaps: list[tuple[str, float]] = []
    for file, h, delta, ts in events:
        q = queues[file, h]
        if delta > 0:
            q.extend([ts] * delta)
            continue
        for _ in range(min(-delta, len(q))):
            added = q.popleft()
            gaps.append((added, (when(ts) - when(added)).total_seconds()))
    return gaps


def half_width_n(sd: float, design_effect: float = 1.0) -> int:
    return math.ceil((Z_TWO_SIDED * sd / HALF_WIDTH) ** 2 * design_effect)


# -- the power simulation -------------------------------------------------------------
def study(
    n: int, delta: float, x: Inputs, rho: float, rng: random.Random
) -> tuple[list[float], list[int]]:
    """One simulated confirmatory study: per-task mean Brier of answered bound items,
    week effects AR(1), a task effect for the ICC, Bernoulli(0.5) arms."""
    sd, icc, sd_w = x.sd_item or 0.0, max(x.icc or 0.0, 0.0), x.sd_week or 0.0
    rate, answer = x.rate or 1.0, 1.0 if x.answer is None else x.answer
    weeks = math.ceil(n / rate)
    effect, effects = rng.gauss(0, sd_w), []
    for _ in range(weeks):
        effects.append(effect)
        effect = rho * effect + math.sqrt(1 - rho * rho) * rng.gauss(0, sd_w)
    values, arms = [], []
    for i in range(n):
        arm = int(rng.random() < 0.5)
        task = effects[int(i // rate)] + rng.gauss(0, math.sqrt(icc) * sd)
        items = [
            task + rng.gauss(0, math.sqrt(1 - icc) * sd) - delta * arm
            for _ in range(K_E)
            if rng.random() < answer
        ]
        if items:
            values.append(statistics.fmean(items))
            arms.append(arm)
    return values, arms


def power(
    n: int, delta: float, x: Inputs, rho: float, sims: int, draws: int, seed: int
) -> float:
    """Share of simulated studies whose randomization test gives p below ALPHA; a study
    with one arm empty cannot be tested and counts as no rejection."""
    rng = random.Random(seed)
    hits = 0
    for _ in range(sims):
        values, arms = study(n, delta, x, rho, rng)
        if 0 < sum(arms) < len(arms):
            p, _ = scoring.randomization_test(values, arms, rng.getrandbits(32), draws)
            hits += p < ALPHA
    return hits / sims


def min_tasks(power_at: Callable[[int], float]) -> int | None:
    lo, hi = 4, 8
    while power_at(hi) < POWER:
        lo, hi = hi, hi * 2
        if hi > N_CAP:
            return None
    while hi - lo > max(2, hi // 50):
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if power_at(mid) >= POWER else (mid, hi)
    return hi


def mde(power_at: Callable[[float], float]) -> float | None:
    lo, hi = 0.0, 3.0
    if power_at(hi) < POWER:
        return None
    while hi - lo > 0.01:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if power_at(mid) >= POWER else (mid, hi)
    return round(hi, 2)


def worst(values: Sequence[float | None]) -> float | None:
    """The largest over the lag-1 grid; unknown if any grid point is unknown."""
    known = [v for v in values if v is not None]
    return max(known) if known and len(known) == len(values) else None
