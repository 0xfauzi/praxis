"""The scoring functions against values computed by hand before the run.

scoring.py is pure arithmetic with no I/O, so the real thing here is the module itself:
nothing is stubbed. Every expected number below was worked out on paper (the working is
in the comments) before the first run; the grid-honesty claim is checked by exact
enumeration with Fraction rather than taken from the design.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from praxis import scoring as s

pytestmark = pytest.mark.integration


def test_snap_goes_to_nearest_grid_point_and_midpoints_toward_half() -> None:
    near = [s.snap(p) for p in (0.0, 0.15, 0.31, 0.69, 1.0)]
    assert near == [0.1, 0.1, 0.3, 0.7, 0.9]
    assert [s.snap(p) for p in (0.2, 0.4, 0.6, 0.8)] == [0.3, 0.5, 0.5, 0.7]
    for bad in (-0.01, 1.01, float("nan")):
        with pytest.raises(ValueError, match="probability"):
            s.snap(bad)


def test_grid_keeps_honest_reporting_optimal_under_brier() -> None:
    # Expected Brier of reporting g under belief q is q(1-g)^2 + (1-q)g^2, minimized by
    # the g nearest q. Over q = 1/1000 .. 999/1000 the only ties are the four exact
    # midpoints 0.2, 0.4, 0.6, 0.8. The design says 2 ties; exact arithmetic gives 4.
    grid = [Fraction(str(g)) for g in s.GRID]
    ties = 0
    for i in range(1, 1000):
        q = Fraction(i, 1000)
        loss = {g: q * (1 - g) ** 2 + (1 - q) * g**2 for g in grid}
        best = {g for g in grid if loss[g] == min(loss.values())}
        nearest = {g for g in grid if abs(g - q) == min(abs(h - q) for h in grid)}
        assert best == nearest
        assert Fraction(str(s.snap(i / 1000))) in best
        ties += len(best) > 1
    assert ties == 4


def test_brier_sum_and_skill() -> None:
    assert s.brier(0.7, 1) == pytest.approx(0.09)
    assert s.brier(0.7, 0) == pytest.approx(0.49)
    assert s.brier_sum([0.7, 0.3], [1, 1]) == pytest.approx(0.58)  # 0.09 + 0.49
    assert s.bss(0.58, 1.0) == pytest.approx(0.42)
    assert s.bss(0.58, 0.0) is None
    with pytest.raises(ValueError, match="outcome"):
        s.brier(0.5, 2)
    with pytest.raises(ValueError, match="no forecasts"):
        s.brier_sum([], [])
    with pytest.raises(ValueError, match="shorter"):
        s.brier_sum([0.1, 0.3], [1])


def test_decomposition_bias_corrected() -> None:
    # 0.1 x4 with one event, 0.9 x4 with three. N = 8, Y = 4, BS = 1.68 / 8 = 0.21.
    # REL = 0.0225, RES = 0.0625, UNC = 0.25.
    # Correction (1/8)(1*3/12 + 3*1/12) = 0.0625; spread 16 / (64 * 7) = 1/28.
    # REL' = -0.04, RES' = 1/28, UNC' = 16 / 56 = 2/7.
    ps = [0.1] * 4 + [0.9] * 4
    os = [0, 0, 0, 1, 1, 1, 1, 0]
    rel, res, unc = s.decomposition(ps, os)
    assert (rel, res, unc) == pytest.approx((-0.04, 1 / 28, 2 / 7))
    assert rel - res + unc == pytest.approx(s.brier_sum(ps, os) / 8)


def test_decomposition_skips_singleton_bins_in_the_correction() -> None:
    # 0.5 -> 1 alone; 0.1 -> 0, 1. N = 3, Y = 2. REL = 0.19, RES = 1/18, UNC = 2/9.
    # Correction (1/3)(1*1/2) = 1/6 (the singleton bin is outside the sum);
    # spread 2 / (9 * 2) = 1/9. REL' = 0.19 - 1/6, RES' = 0, UNC' = 1/3.
    rel, res, unc = s.decomposition([0.5, 0.1, 0.1], [1, 0, 1])
    assert (rel, res, unc) == pytest.approx((0.19 - 1 / 6, 0.0, 1 / 3))
    with pytest.raises(ValueError, match="grid"):
        s.decomposition([0.25, 0.1], [1, 0])
    with pytest.raises(ValueError, match="at least 2"):
        s.decomposition([0.1], [1])


def test_corp_pools_adjacent_violators() -> None:
    # 0.1: 1,1 (rate 1); 0.3: 0,0 (rate 0) violate and pool to 2/4; 0.5: 1,0,1,1 = 0.75.
    assert s.corp_reliability(
        [0.1, 0.1, 0.3, 0.3, 0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0, 1, 0, 1, 1]
    ) == [(0.1, 0.5, 2), (0.3, 0.5, 2), (0.5, 0.75, 4)]
    # Cascade: 0.1: 0,1 (0.5); 0.3: 1 (1.0); 0.5: 0,0 (0). 0.3 and 0.5 pool to 1/3,
    # which violates 0.5 below it, so all three pool to 2/5.
    assert s.corp_reliability([0.1, 0.1, 0.3, 0.5, 0.5], [0, 1, 1, 0, 0]) == [
        (0.1, 0.4, 2),
        (0.3, 0.4, 1),
        (0.5, 0.4, 2),
    ]


def test_multicategory_brier() -> None:
    assert s.multicategory_brier([0.7, 0.1, 0.3], 0) == pytest.approx(0.19)
    assert s.multicategory_brier([1 / 3] * 3, 2) == pytest.approx(2 / 3)  # (k-1)/k
    with pytest.raises(ValueError, match="chosen"):
        s.multicategory_brier([0.5, 0.5], 2)


def test_randomization_test_exact() -> None:
    # Treatment {0,1}: control 0.35 - treatment 0.15 = 0.2. Over the 6 allocations of 2
    # in 4 the statistics are 0.2, 0.1, 0, 0, -0.1, -0.2: one-sided 1/6, two-sided 2/6.
    values, arms = [0.1, 0.2, 0.3, 0.4], [1, 1, 0, 0]
    p, used = s.randomization_test(values, arms, seed=1, draws=0)
    assert p == pytest.approx(1 / 6)
    assert used == 6
    p2, _ = s.randomization_test(values, arms, seed=1, draws=0, one_sided=False)
    assert p2 == pytest.approx(1 / 3)
    # Unrestricted 4 of 8, perfectly separated: 1/70 = 0.0143 (design 6.5), still exact.
    p8, used8 = s.randomization_test([0.1] * 4 + [0.5] * 4, [1] * 4 + [0] * 4, 1, 0)
    assert p8 == pytest.approx(1 / 70)
    assert used8 == 70
    with pytest.raises(ValueError, match="both arms"):
        s.randomization_test([0.1, 0.2], [1, 1], 1, 10)


def test_randomization_test_monte_carlo() -> None:
    # 4 of 9 is 126 allocations, above 70, so Monte Carlo. Exact p would be 1/126
    # = 0.0079; with 2000 draws (1 + hits) / 2001 lands near it, and the seed fixes it.
    values, arms = [0.1] * 4 + [0.5] * 5, [1] * 4 + [0] * 5
    p, used = s.randomization_test(values, arms, seed=7, draws=2000)
    assert used == 2000
    assert 0.003 < p < 0.02
    assert s.randomization_test(values, arms, seed=7, draws=2000) == (p, used)


def test_koehler_levin() -> None:
    # A = [.3,.3,.1,.1] drawn at 2 from {1,2,3}: gaps 2/15, 0.2, 2/15.
    # B = [.3,.1,.1,.1] drawn at 1 from {1,2}: gaps 0.2, 0.1.
    # Unit means over the 6 combinations: 1/6, 7/60, 0.2, 0.15, 1/6, 7/60; the observed
    # (2, 1) = 0.2 is the unique maximum, so p = 1/6.
    series = {"a": [0.3, 0.3, 0.1, 0.1], "b": [0.3, 0.1, 0.1, 0.1]}
    eligible = {"a": [1, 2, 3], "b": [1, 2]}
    p = s.koehler_levin(series, {"a": 2, "b": 1}, 1, 100, eligible=eligible)
    assert p == pytest.approx(1 / 6)
    with pytest.raises(ValueError, match="hold the start"):
        s.koehler_levin(series, {"a": 2, "b": 3}, 1, 100, eligible=eligible)
    bad = {**eligible, "b": [1, 4]}
    with pytest.raises(ValueError, match="both phases"):
        s.koehler_levin(series, {"a": 2, "b": 1}, 1, 100, eligible=bad)


def test_split_half() -> None:
    # Odd-item means .15 .55 .85, even-item means .35 .55 .75 across three weeks.
    # Sxy = .14, Sxx = .246667, Syy = .08, r = .99662, 2r / (1 + r) = .99831.
    weeks = [[0.1, 0.3, 0.2, 0.4], [0.5, 0.5, 0.6, 0.6], [0.9, 0.7, 0.8, 0.8]]
    assert s.split_half(weeks) == pytest.approx(0.99831, abs=1e-4)
    assert s.split_half([[0.1, 0.1], [0.3, 0.3], [0.5, 0.5]]) == pytest.approx(1.0)
    assert s.split_half(weeks[:2]) is None
    assert s.split_half([[0.1, 0.2], [0.3, 0.2], [0.5, 0.2]]) is None  # constant half
    assert s.split_half([[0.1, 0.2], [0.3], [0.5, 0.2]]) is None


def test_pfnet_keeps_links_no_longer_than_the_minimax_path() -> None:
    # d01 = 1, d12 = 2, d02 = 3: the path 0-1-2 has largest step 2 < 3, so 0-2 goes.
    inf_free = [[0.0, 1.0, 3.0], [1.0, 0.0, 2.0], [3.0, 2.0, 0.0]]
    assert s.pfnet(inf_free) == [
        [False, True, False],
        [True, False, True],
        [False, True, False],
    ]
    # d02 = 2 ties the minimax path, and a tie survives: the triangle stays.
    tie = [[0.0, 1.0, 2.0], [1.0, 0.0, 2.0], [2.0, 2.0, 0.0]]
    assert s.pfnet(tie) == [[i != j for j in range(3)] for i in range(3)]
    with pytest.raises(ValueError, match="symmetric"):
        s.pfnet([[0.0, 1.0], [2.0, 0.0]])


def test_pfsim_link_similarity() -> None:
    # Path 0-1-2 has links {01, 12}; the triangle has {01, 02, 12}. C = 2, so
    # S = 2 / (2 + 3 - 2) = 2/3, and E[C] = 2 * 3 / 3 pairs = 2.0. With an isolated
    # fourth node the links are the same but P = 6, so E[C] = 1.0.
    path = [[False, True, False], [True, False, True], [False, True, False]]
    tri = [[i != j for j in range(3)] for i in range(3)]
    three = s.pfsim(path, tri)
    assert three is not None
    assert three[0] == pytest.approx(2 / 3)
    assert three[1] == pytest.approx(2.0)
    path4 = [[*row, False] for row in path] + [[False] * 4]
    tri4 = [[*row, False] for row in tri] + [[False] * 4]
    four = s.pfsim(path4, tri4)
    assert four is not None
    assert four[0] == pytest.approx(2 / 3)
    assert four[1] == pytest.approx(1.0)
    empty = [[False] * 3 for _ in range(3)]
    assert s.pfsim(empty, empty) is None
    with pytest.raises(ValueError, match="square"):
        s.pfsim(path, tri4)
