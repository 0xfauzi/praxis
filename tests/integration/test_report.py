"""report.py against a real ledger under a temporary $PRAXIS_HOME, a component map
derived from a real git repository and frozen, and rows written only through
store.py's methods. Transcripts for R1 sit under a temporary $HOME, as Claude Code
writes them.

Every expected number below was computed by hand from the planted rows before the
first run; the arithmetic is written beside it. Six components (alpha .. zeta) give 30
ordered D1 pairs with 4 true edges, alpha -> beta -> gamma -> delta and eps -> alpha,
so R_base is 4/30, every unordered pair has one forward direction and the forward edge
rate is 4/15, on the grid 0.3. Only pairs whose direction the edges force are probed.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
from dataclasses import replace
from itertools import permutations
from typing import TYPE_CHECKING

import pytest

from praxis import report
from praxis.components import FIXED, ComponentMap, Rule, derive, map_version, to_json
from praxis.store import Evidence, Store
from praxis.unassisted import locate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

NAMES = ("alpha", "beta", "gamma", "delta", "eps", "zeta")
EDGES = {("alpha", "beta"), ("beta", "gamma"), ("gamma", "delta"), ("eps", "alpha")}
T0 = "2026-01-05T09:00:00.000Z"
EV = Evidence("full", ["test:planted"], "test-v1", [], [])
CANARY = "CANARYREPORT9f3e"
SEED_HASH = "0" * 64
PROMPTS = {
    1: f"refactor alpha and beta {CANARY}",
    2: "speed up src/pkg/gamma.py",
    3: "tidy beta",
    4: "look at eps",
}


def at(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def session(ordinal: int) -> str:
    return f"aaaaaaaa-0000-4000-8000-{ordinal:012d}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


def make_repo(root: Path) -> Path:
    pkg = root / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for name in NAMES:
        (pkg / f"{name}.py").write_text(f"NAME = {name!r}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    return tmp_path / "praxis-home"


class World:
    def __init__(self, repo: Path, home: Path) -> None:
        found = locate(repo)
        assert found is not None
        self.repo_id = found.repo_id
        self.store = Store(home / "ledger.db").open()
        cmap = derive(repo, self.repo_id)
        self.version = self.store.component_map_put(to_json(cmap), T0)
        self.store.component_map_freeze(self.repo_id, self.version, T0)
        pairs = list(permutations(NAMES, 2))
        ids = self.store.probe_items_put(
            rows=[("D1", a, b, (a, b) in EDGES, 0, None, None) for a, b in pairs],
            ev=EV, repo_id=self.repo_id, map_version=self.version, truth_ts=T0,
            extractor_version="x1-v1",
        )  # fmt: skip
        self.item = dict(zip(pairs, ids, strict=True))

    def task(
        self,
        ordinal: int,
        arm: str,
        roles: tuple[list[tuple[str, str]], ...],
        times: tuple[str, str],
        touched: set[str],
        forced: bool = False,
    ) -> str:
        tid = f"{ordinal:016x}"
        bound, related, drawn = ([self.item[p] for p in r] for r in roles)
        self.store.task_register(
            region=[], bound=bound, related=related, drawn=drawn, task_id=tid,
            repo_id=self.repo_id, map_version=self.version, ordinal=ordinal,
            ts=times[0], arm=arm, seed_hash=SEED_HASH, stratum="region",
            forced_control=forced,
        )  # fmt: skip
        self.store.task_bind(tid, session(ordinal), f"prompt-{ordinal}", times[0])
        rows = [(c, c in touched, None, None, None) for c in NAMES]
        self.store.outcome(tid, rows, times[1], ev=EV)
        self.store.task_close(tid, times[1])
        return tid

    def respond(
        self,
        pair: tuple[str, str],
        task_id: str,
        ts: str,
        p: float | None,
        *,
        truth: bool | None = None,
        deadline_met: bool = True,
    ) -> None:
        self.store.probe_response(
            ev=EV, item_id=self.item[pair], task_id=task_id, ts=ts, p=p, seconds=5.0,
            lag_days=8.0, deadline_met=deadline_met,
            truth_at_probe=pair in EDGES if truth is None else truth,
            truth_changed=False, head_sha=None, tree_hash=None, dirty=None,
            extractor_version="x1-v1",
        )  # fmt: skip


def transcripts(projects: Path) -> None:
    folder = projects / "-tmp-repo"
    folder.mkdir(parents=True)
    for ordinal, text in PROMPTS.items():
        entry = {"type": "user", "promptId": f"prompt-{ordinal}",
                 "timestamp": at(5, 10), "origin": {"kind": "human"},
                 "message": {"role": "user", "content": text}}  # fmt: skip
        path = folder / f"{session(ordinal)}.jsonl"
        path.write_text(json.dumps(entry) + "\n")


@pytest.fixture
def world(repo: Path, home: Path) -> World:
    """Four closed tasks, one void one, F1 forecasts in the two treatment tasks and
    fourteen probe responses, the first thirteen of them first probes."""
    w = World(repo, home)
    ab, ba, ac, ca = ("alpha", "beta"), ("beta", "alpha"), ("alpha", "gamma"), (
        "gamma", "alpha")  # fmt: skip
    cd, dc, ed, de = ("gamma", "delta"), ("delta", "gamma"), ("eps", "delta"), (
        "delta", "eps")  # fmt: skip
    bc, cb, ea, ae = ("beta", "gamma"), ("gamma", "beta"), ("eps", "alpha"), (
        "alpha", "eps")  # fmt: skip
    t1 = w.task(1, "treatment", ([ab, ba], [ac], [ca]), (at(5, 10), at(5, 11)),
                {"alpha", "beta"})  # fmt: skip
    t2 = w.task(2, "control", ([cd, dc], [ed], [de]), (at(5, 12), at(5, 13)),
                {"gamma", "delta"})  # fmt: skip
    t3 = w.task(3, "treatment", ([bc, cb], [], []), (at(5, 14), at(5, 15)),
                {"beta", "gamma"})  # fmt: skip
    t4 = w.task(4, "control", ([ea, ae], [], []), (at(5, 16), at(5, 17)),
                {"eps", "alpha"})  # fmt: skip
    w.store.task_register(
        region=[], bound=[w.item["zeta", "alpha"]], related=[], drawn=[],
        task_id=f"{5:016x}", repo_id=w.repo_id, map_version=w.version, ordinal=5,
        ts=at(5, 18), arm="treatment", seed_hash=SEED_HASH, stratum="region",
    )  # fmt: skip
    w.store.task_void(f"{5:016x}", "hook_error", at(5, 18, 30))
    given = {t1: (("alpha", 0.9), ("beta", 0.7), ("gamma", 0.3), ("delta", 0.1),
                  ("eps", None)),
             t3: (("beta", 0.9), ("gamma", 0.5), ("delta", 0.3), ("eps", 0.1),
                  ("zeta", 0.1))}  # fmt: skip
    for tid, rows in given.items():
        forecasts = [(c, p, "forecast", None) for c, p in rows]
        w.store.forecast(tid, forecasts, at(5, 10 if tid == t1 else 14, 1), ev=EV)
    answers = [(ab, t1, 0.9), (ba, t1, 0.3), (ac, t1, 0.5), (ca, t1, 0.1),
               (cd, t2, 0.5), (dc, t2, 0.5), (ed, t2, None), (de, t2, 0.3),
               (bc, t3, 0.7), (cb, t3, 0.1), (ea, t4, 0.3)]  # fmt: skip
    for minute, (pair, tid, p) in enumerate(answers):
        w.respond(pair, tid, at(13, 10, minute), p)
    w.respond(ae, t4, at(13, 10, 11), 0.1, deadline_met=False)
    w.respond(("zeta", "alpha"), f"{5:016x}", at(13, 10, 12), 0.9)
    w.respond(ab, t1, at(13, 10, 13), 0.1)  # a repeat probe: never scored
    return w


def block(scores: report.Scores, measure: str, role: str, arm: str) -> report.Block:
    (found,) = [b for b in scores.blocks
                if (b.measure, b.role, b.arm) == (measure, role, arm)]  # fmt: skip
    return found


def test_p1_scores_references_and_contrasts_by_hand(world: World) -> None:
    s = report.scores(world.store, world.repo_id, seed=7, draws=500)
    assert (s.refused, s.map_version) == (None, world.version)

    treated = block(s, "P1", "bound", "treatment")
    # BS: (0.9-1)^2 + (0.3-0)^2 + (0.7-1)^2 + (0.1-0)^2 = 0.01+0.09+0.09+0.01 = 0.20
    assert (treated.n, treated.tasks, treated.refused) == (4, 2, None)
    assert treated.brier == pytest.approx(0.05)
    # R_base 2/15 for every item: o=1 costs (13/15)^2 = 169/225, o=0 4/225.
    assert treated.skill["R_base"] == pytest.approx((1 - 0.20 / (346 / 225), 4))
    # R_layer: forward 0.3 (o=1, 0.49) twice, backward 0.1 (o=0, 0.01) twice: 1.00.
    assert treated.skill["R_layer"] == pytest.approx((0.8, 4))
    # R_fam unfitted for the first two probes (one truth value only); then PAV of
    # earlier truths on co-touch: beta-gamma at 1/4 gets 2/4, gamma-beta 3/5.
    # Owner 0.09 + 0.01 against 0.25 + 0.36.
    assert treated.skill["R_fam"] == pytest.approx((1 - 0.10 / 0.61, 2))
    # Pure bins: REL is the mean BS; RES 0.25 + 4/48; UNC 4/12.
    assert treated.decomposition == pytest.approx((0.05, 1 / 3, 1 / 3))
    assert [(b.p, b.rate, b.n, b.interpretable) for b in treated.bins] == [
        (0.1, 0.0, 1, False), (0.3, 0.0, 1, False), (0.7, 1.0, 1, False),
        (0.9, 1.0, 1, False)]  # fmt: skip
    assert all(b.band and 0 <= b.band[0] <= b.band[1] <= 1 for b in treated.bins)
    assert treated.ev.sources
    assert treated.ev.coverage == "full"

    control = block(s, "P1", "bound", "control")
    # 0.25 + 0.25 + 0.49 (ae is slow and excluded).
    assert (control.n, control.tasks) == (3, 2)
    assert control.brier == pytest.approx(0.33)
    assert control.skill["R_base"] == pytest.approx((1 - 0.99 / (342 / 225), 3))
    assert control.skill["R_layer"] == pytest.approx((0.0, 3))
    # R_fam: gamma-delta 2/4, delta-gamma 2/3, eps-alpha 3/6: 0.25 + 4/9 + 0.25.
    assert control.skill["R_fam"] == pytest.approx((1 - 0.99 / (17 / 18), 3))

    for role, arm in (("related", "treatment"), ("related", "control"),
                      ("drawn_not_shown", "treatment"),
                      ("drawn_not_shown", "control")):  # fmt: skip
        b = block(s, "P1", role, arm)
        assert (b.refused, b.brier, b.skill) == ("below_minimum", None, {})
    assert s.counts == {
        ("F1", "unanswered", "treatment"): 1,
        ("P1", "repeat_probe", "treatment"): 1,
        ("P1", "skipped", "control"): 1,
        ("P1", "slow", "control"): 1,
        ("P1", "task_not_closed", "treatment"): 1,
    }
    assert s.weeks == {"2026-W03": 13}
    # Per-task means 0.05, 0.25, 0.05, 0.49: of the C(4, 2) = 6 allocations only the
    # true one reaches 0.37 - 0.05. Drawn items: 0.09 - 0.01 against its mirror.
    assert s.tests["specific"] == pytest.approx((1 / 6, 6))
    assert s.tests["near"] is None
    assert s.tests["untargeted"] == pytest.approx((0.5, 2))
    assert s.tests["pipeline_placebo"] is None
    assert report.randomization(world.store, world.repo_id, 7, 500) == s.tests
    (rule,) = s.rules
    assert (rule.name, rule.met) == ("PR-1", False)
    assert "specific_p_not_below_alpha" in rule.reasons


def test_f1_against_r1_read_from_transcripts(world: World, home: Path) -> None:
    projects = home.parent / "user" / ".claude" / "projects"
    bare = block(report.scores(world.store, world.repo_id), "F1", "forecast",
                 "treatment")  # fmt: skip
    # No transcript: whether a prompt named a component is unknown, so R1 is NULL.
    assert bare.skill["R1"] == (None, 0)
    transcripts(projects)
    s = report.scores(world.store, world.repo_id, seed=7, draws=500)
    f1 = block(s, "F1", "forecast", "treatment")
    # Owner: task 1 0.01+0.09+0.09+0.01, task 3 0.01+0.25+0.09+0.01+0.01; 0.57 / 9.
    assert (f1.n, f1.tasks) == (9, 2)
    assert f1.brier == pytest.approx(0.57 / 9)
    # R1, task 1: no earlier task, 1/2 each, 1.00. Task 3 names beta: beta 2/3 (task
    # 1 named and touched it), gamma 1/3, delta 2/4, eps and zeta 1/4: 67/72.
    assert f1.skill["R1"] == pytest.approx((1 - 0.57 / (139 / 72), 9))
    # R0 and R_last exist for task 3 only: 0.25*3 + 0 + 0 and 0.81+0.01+0.81+0.01+0.01.
    assert f1.skill["R0"] == pytest.approx((1 - 0.37 / 0.75, 5))
    assert f1.skill["R_last"] == pytest.approx((1 - 0.37 / 1.65, 5))
    # Swapped, the forecasts reach BSS 1 - 1.77 / (11/12 + 5/4) only: p = 1/2 of 2.
    assert s.tests["f1_shuffle"] == pytest.approx((0.5, 2))
    refs = report.references(world.store, world.repo_id)
    assert refs[f"{1:016x}:alpha"] == {"R1": 0.5, "R0": None, "R_last": None}
    assert refs[f"{3:016x}:beta"] == pytest.approx(
        {"R1": 2 / 3, "R0": 0.5, "R_last": 0.1}
    )
    out = io.StringIO()
    report.render(s, out)
    leaked = [p for p in home.rglob("*") if p.is_file()
              and CANARY.encode() in p.read_bytes()]  # fmt: skip
    assert CANARY not in out.getvalue()
    assert leaked == []
    world.store.flag(ts=at(13, 12), code="version_change", detail={},
                     task_id=f"{1:016x}")  # fmt: skip
    again = report.scores(world.store, world.repo_id)
    assert block(again, "F1", "forecast", "treatment").n == 5
    assert again.counts["F1", "flagged", "treatment"] == 5


def test_edge_history_moves_the_earlier_graph(repo: Path, home: Path) -> None:
    w = World(repo, home)
    ab, ba, ze = ("alpha", "beta"), ("beta", "alpha"), ("zeta", "eps")
    ea, cd = ("eps", "alpha"), ("gamma", "delta")
    tid = w.task(1, "treatment", ([ab, ba, ze, ea, cd], [], []),
                 (at(5, 10), at(5, 11)), {"alpha"})  # fmt: skip

    def scan(ts: str, file: str, src: str, dst: str) -> None:
        w.store.file_edges(scanned={file: src}, edges=[(file, dst, 1, True)], ev=EV,
                           session_id="s1", repo_id=w.repo_id,
                           map_version=w.version, ts=ts,
                           extractor_version="x1-v1")  # fmt: skip

    w.respond(ab, tid, at(13, 10), 0.9)
    scan(at(13, 10, 30), "src/pkg/zeta.py", "zeta", "eps")
    w.respond(ba, tid, at(13, 11), 0.1)
    w.respond(ze, tid, at(13, 11, 30), 0.5, truth=False)  # a full read: gone again
    w.respond(ea, tid, at(13, 12), 0.9)
    scan(at(13, 12, 30), "src/pkg/delta.py", "delta", "alpha")  # closes a cycle
    w.respond(cd, tid, at(13, 13), 0.9)
    refs = report.references(w.store, w.repo_id)

    def base_layer(pair: tuple[str, str]) -> tuple[float | None, float | None]:
        r = refs[w.item[pair]]
        return r["R_base"], r["R_layer"]

    assert base_layer(ab) == pytest.approx((4 / 30, 0.3))  # the pool alone
    assert base_layer(ba) == pytest.approx((5 / 30, 0.1))  # the scan adds zeta-eps
    assert base_layer(ze) == pytest.approx((5 / 30, 0.3))  # forward 5/15 on the grid
    assert base_layer(ea) == pytest.approx((4 / 30, 0.3))  # the later read wins
    assert base_layer(cd) == pytest.approx((5 / 30, None))  # a cycle has no order


def test_render_names_the_rule_and_refuses_words_without_it(world: World) -> None:
    s = report.scores(world.store, world.repo_id, seed=7, draws=500)
    out = io.StringIO()
    report.render(s, out)
    text = out.getvalue()
    assert "P1 bound treatment: n 4, tasks 2, Brier 0.050, coverage full" in text
    assert "rule PR-1 not met: pilot_validity_not_recorded" in text
    assert "specific p 0.1667 over 6 allocations" in text
    assert not re.search(r"\b(caus\w*|impact\w*|would)\b", text, re.IGNORECASE)
    assert not [n for n in NAMES if re.search(rf"\b{n}\b", text)]
    worded = replace(s, counts={("P1", "would", "control"): 1})
    refused = io.StringIO()
    with pytest.raises(ValueError, match="would"):
        report.render(worded, refused)
    assert refused.getvalue() == ""
    met = report.Rule("PR-1", True, (), "the instrument changed accuracy")
    licensed = io.StringIO()
    report.render(replace(worded, rules=(met,)), licensed)
    assert "rule PR-1 met: the instrument changed accuracy" in licensed.getvalue()
    assert "P1 would control 1" in licensed.getvalue()


def test_refuses_without_a_map_or_with_too_few_components(
    repo: Path, home: Path
) -> None:
    found = locate(repo)
    assert found is not None
    store = Store(home / "ledger.db").open()
    s = report.scores(store, found.repo_id)
    assert (s.refused, s.blocks, s.map_version) == ("no_frozen_map", (), None)
    assert report.references(store, found.repo_id) == {}
    assert set(report.randomization(store, found.repo_id, 1, 10).values()) == {None}
    out = io.StringIO()
    report.render(s, out)
    assert "no scores: no_frozen_map" in out.getvalue()
    rules = (*FIXED, *(Rule(n, (f"src/pkg/{n}.py",)) for n in NAMES[:4]))
    small = ComponentMap(found.repo_id, "owner", rules, map_version(rules), ())
    store.component_map_put(to_json(small), T0)
    store.component_map_freeze(found.repo_id, small.version, T0)
    assert report.scores(store, found.repo_id).refused == "too_few_components"


# -- never-drawn validation probes: the pipeline placebo and V1 test-retest ------------


def validate(w: World, pair: tuple[str, str], ts: str, p: float | None) -> None:
    """A never-drawn validation probe: no task, no lag."""
    w.store.probe_response(
        ev=EV, item_id=w.item[pair], task_id=None, ts=ts, p=p, seconds=5.0,
        lag_days=None, deadline_met=True, truth_at_probe=pair in EDGES,
        truth_changed=False, head_sha=None, tree_hash=None, dirty=None,
        extractor_version="x1-v1",
    )  # fmt: skip


def sure(pair: tuple[str, str], right: bool) -> float:
    """0.9 or 0.1 on the side of the truth when right, the other side when not."""
    return 0.9 if (pair in EDGES) == right else 0.1


DRAWN = [("alpha", "beta"), ("beta", "gamma"), ("gamma", "beta"), ("delta", "eps")]
NEVER = [("gamma", "delta"), ("eps", "alpha"), ("zeta", "eps"), ("eps", "zeta")]


@pytest.mark.parametrize("never_right", [False, True])
def test_pipeline_placebo_suspends_the_contrasts_when_the_pipeline_leaks(
    repo: Path, home: Path, never_right: bool
) -> None:
    w = World(repo, home)
    for i, pair in enumerate(DRAWN, 1):
        arm = "treatment" if i % 2 else "control"
        tid = w.task(i, arm, ([], [], [pair]), (at(5, 2 * i), at(5, 2 * i + 1)),
                     set())  # fmt: skip
        w.respond(pair, tid, at(13, 10, i), sure(pair, right=True))
    for i, pair in enumerate(NEVER):
        validate(w, pair, at(13, 11, i), sure(pair, right=never_right))
    s = report.scores(w.store, w.repo_id, seed=7, draws=500)
    out = io.StringIO()
    report.render(s, out)
    text = out.getvalue()
    (rule,) = s.rules
    # Drawn-not-shown Brier 0.01 x 4. Never-drawn 0.81 x 4 when wrong: of the C(8, 4)
    # = 70 labellings only the true one and its mirror reach |0.01 - 0.81|, p 2/70.
    # When right, every labelling reaches the observed 0 difference: p 1.
    placebo = (1.0, 70) if never_right else (2 / 70, 70)
    assert s.tests["pipeline_placebo"] == pytest.approx(placebo)
    assert s.counts[("P1", "not_exposed", "unexposed")] == 4
    if never_right:
        assert "suspended" not in text
        assert "pipeline_placebo_differs" not in rule.reasons
        assert "untargeted p 1.0000 over 6 allocations" in text
    else:
        for name in ("specific", "near", "untargeted"):
            assert f"{name} suspended: pipeline placebo differs" in text
        assert "pipeline_placebo p 0.0286 over 70 allocations" in text
        assert "pipeline_placebo_differs" in rule.reasons
    assert report.randomization(w.store, w.repo_id, 7, 500) == s.tests


def test_v1_test_retest_pairs_never_drawn_items_across_weeks(
    repo: Path, home: Path
) -> None:
    w = World(repo, home)
    plan = [  # pair, first answer (week 3), second answer (week 4, or None)
        (("alpha", "beta"), 0.9, 0.7),  # Brier 0.01 then 0.09
        (("beta", "alpha"), 0.3, 0.1),  # 0.09 then 0.01
        (("alpha", "gamma"), 0.5, 0.5),  # 0.25 then 0.25
        (("beta", "gamma"), 0.9, None),  # second skipped: no pair
        (("gamma", "beta"), 0.1, 0.1),  # second in the same week: no pair
        (("gamma", "alpha"), 0.1, None),  # asked once
    ]
    for minute, (pair, first, _) in enumerate(plan):
        validate(w, pair, at(13, 10, minute), first)
    validate(w, ("gamma", "beta"), at(15, 10), 0.1)
    assert report.scores(w.store, w.repo_id).retest == (0, None)
    for minute, (pair, _, second) in enumerate(plan[:2]):
        validate(w, pair, at(20, 10, minute), second)
    validate(w, ("beta", "gamma"), at(20, 10, 3), None)
    assert report.scores(w.store, w.repo_id).retest == (2, None)
    validate(w, ("alpha", "gamma"), at(20, 10, 2), 0.5)
    s = report.scores(w.store, w.repo_id)
    # In hundredths x = (1, 9, 25), y = (9, 1, 25), mean 35/3: Sxy = 2112/9,
    # Sxx = Syy = 2688/9, r = 2112/2688 = 11/14.
    assert s.retest == pytest.approx((3, 11 / 14))
    assert s.counts == {("P1", "not_exposed", "unexposed"): 6,
                        ("P1", "repeat_probe", "unexposed"): 5}  # fmt: skip
    assert all(b.n == 0 for b in s.blocks)
    out = io.StringIO()
    report.render(s, out)
    assert "V1 test-retest: pairs 3, agreement r 0.786\n" in out.getvalue()


# wave 3: pins


def test_bands_keep_zero_rates_and_the_shuffle_statistic_squares() -> None:
    # Every resample spans both values, so each gets one rate per draw; a rate of 0.0
    # at 0.1 is a rate, not a missing one.
    drawn = report._resampled({f"t{i}": [0.1, 0.9] for i in range(5)}, [0.1, 0.9])
    assert [len(drawn[v]) for v in (0.1, 0.9)] == [report.BAND_DRAWS] * 2
    assert 0.0 in drawn[0.1]
    touched = {"t1": {"x": 1, "y": 0}, "t2": {"x": 0, "y": 1}}
    given = {"t1": [("x", 0.9), ("y", 0.3)], "t2": [("x", 0.1), ("y", 0.7)]}
    r1: dict[str, dict[str, float | None]] = {t: {"x": 0.5, "y": 0.5} for t in given}
    tasks = ["t1", "t2"]
    # In place: owner 0.01 + 0.09 + 0.01 + 0.09 = 0.2 against 4 x 0.25 = 1.0.
    # Swapped: 0.81 + 0.49 + 0.81 + 0.49 = 2.6 against 1.0.
    skill = report._shuffled_skill(touched, tasks, given, r1, tasks)
    assert skill == pytest.approx(0.8)
    swapped = report._shuffled_skill(touched, tasks, given, r1, ["t2", "t1"])
    assert swapped == pytest.approx(-1.6)


# design 6.4: the general effect across repositories


def started(tmp_path: Path, home: Path, name: str, weeks: tuple[str, ...]) -> World:
    """A repository whose start week is the last of its candidate weeks."""
    w = World(make_repo(tmp_path / name), home)
    w.store.repository_start_put(
        candidates=weeks, repo_id=w.repo_id, map_version=w.version, drawn_ts=T0,
        start_week=weeks[-1], seed_hash=SEED_HASH,
    )  # fmt: skip
    return w


# One never-drawn probe a week, Monday to Monday: 2026-W03 to 2026-W06.
DAYS = ((1, 13), (1, 20), (1, 27), (2, 3))


def weekly(w: World, right: tuple[bool, ...]) -> None:
    for (month, day), pair, ok in zip(DAYS, NEVER, right, strict=False):
        validate(w, pair, f"2026-{month:02d}-{day:02d}T10:00:00.000Z", sure(pair, ok))


def test_the_general_effect_redraws_the_start_weeks_by_hand(
    tmp_path: Path, home: Path
) -> None:
    # Each repository: Brier 0.81, 0.81 before its start in W05, 0.01, 0.01 from it.
    # Candidates W04 (split 1) and W05 (split 2). Split 2 gaps 0.81 - 0.01 = 0.80;
    # split 1 gaps 0.81 - 0.83 / 3. Of the 2^3 = 8 start combinations only the drawn
    # one reaches a mean gap of 0.80: p = 1/8.
    level = (False, False, True, True)
    worlds = [started(tmp_path, home, f"r{i}", ("2026-W04", "2026-W05"))
              for i in range(2)]  # fmt: skip
    late = started(tmp_path, home, "late", ("2026-W06", "2026-W07"))
    gap = started(tmp_path, home, "gap", ("2026-W04", "2026-W05"))
    for w in (*worlds, late):
        weekly(w, level)
    for (month, day), pair in zip((DAYS[0], *DAYS[2:]), NEVER, strict=False):
        validate(gap, pair, f"2026-{month:02d}-{day:02d}T10:00:00.000Z", 0.5)
    s = report.scores(late.store, late.repo_id, seed=7, draws=500)
    # late's start W07 is after its last week; gap has no W04 probe, so W04 and W05
    # are one split, two assignments koehler_levin cannot weight: both left out.
    left = {"candidates_share_a_split": 1, "no_week_after_start": 1}
    assert s.general == report.General(None, 2, left, run=True)
    third = started(tmp_path, home, "r2", ("2026-W04", "2026-W05"))
    weekly(third, level)
    s = report.scores(late.store, late.repo_id, seed=7, draws=500)
    assert s.general.p == pytest.approx(1 / 8)
    assert s.general == report.General(s.general.p, 3, left, run=True)
    out = io.StringIO()
    report.render(s, out)
    assert (
        "general effect across repositories: p 0.1250, repositories 3, left out:"
        " candidates_share_a_split 1; no_week_after_start 1\n"
    ) in out.getvalue()
    assert "Marascuilo-Busk" in out.getvalue()
    assert not report.scores(late.store, late.repo_id).general.run


def test_a_forced_control_task_is_out_of_the_arm_contrasts(
    repo: Path, home: Path
) -> None:
    w = World(repo, home)
    ab, ba = ("alpha", "beta"), ("beta", "alpha")
    forced = w.task(1, "control", ([ab], [], []), (at(5, 10), at(5, 11)), set(), True)
    drawn = w.task(2, "control", ([ba], [], []), (at(5, 12), at(5, 13)), set())
    w.respond(ab, forced, at(13, 10), 0.9)
    w.respond(ba, drawn, at(13, 11), 0.1)
    s = report.scores(w.store, w.repo_id)
    assert s.counts == {("P1", "forced_control", "control"): 1}
    assert block(s, "P1", "bound", "control").n == 1
