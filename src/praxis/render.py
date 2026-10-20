"""The shapes `praxis show report` returns and their text (report.py and general.py
compute them).

The text refuses the words cause, impact and would unless a pre-registered rule says
met, and then names the rule (AGENTS.md invariant 3); no component id, path or prompt
reaches it, only counts, numbers and reason codes. A pipeline placebo below ALPHA
suspends every P1 arm contrast in the text (design 6.3): the numbers stay in Scores,
and the text prints the suspension in their place.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from collections.abc import Mapping

    from praxis.store import Evidence

ALPHA = 0.05  # docs/preregistration.md 1.4, one-sided
# The decomposition's floor; the Stage 2 minimum from the power simulation replaces it.
MIN_FORECASTS = 2
# Two points always correlate at +-1, as in scoring.split_half.
MIN_PAIRS = 3
# Research answers 2026-10-19: a 0.10 miscalibration to a 95 percent half-width.
BIN_MIN = {0.1: 35, 0.3: 81, 0.5: 97, 0.7: 81, 0.9: 35}
CONTRASTS = {"specific": "bound", "near": "related", "untargeted": "drawn_not_shown"}
SUSPENDED = "suspended: pipeline placebo differs"
CLAIMS = {"brier": "derived", "decomposition": "derived", "bss": "comparative",
          "arm contrast": "comparative", "test-retest": "derived"}  # fmt: skip
TEST_ASSUMPTIONS = (
    "arm contrasts: control minus treatment, per-task mean Brier as the unit, "
    "one-sided, with the treated task count fixed. The C(n, k) allocations are "
    "enumerated at 70 or fewer. Stage 2 names the allocation rule that the test draws "
    "again.",
    "pipeline placebo: first probes of drawn-not-shown items, in both arms and with "
    "the P1 exclusions, against first probes of never-drawn validation items. It "
    "excludes skipped, slow and truth-changed answers. It uses the Brier score per "
    "item, two-sided, with the count per label fixed. A p below "
    f"{ALPHA:g} suspends every P1 contrast. Design 6.3 says within arm. Pooling the "
    "arms admits the untargeted transfer of the treatment arm.",
    "f1_shuffle: the share of task pairings of forecasts whose BSS against R1 reaches "
    "the BSS of the true pairing.",
)
RETEST_ASSUMPTIONS = (
    "V1 test-retest: the first two probes of a never-drawn item in different ISO "
    "weeks, both on time, with one truth that did not change. Agreement is Pearson's "
    f"r of their Brier scores, NULL below {MIN_PAIRS} pairs or with a constant side. "
    "Validation items never enter a confirmatory test.",
)
GENERAL_ASSUMPTIONS = (
    "general effect: each repository draws its start week once from its committed "
    "candidate weeks, uniformly and independently of the other repositories. The test "
    "draws the start weeks again by this scheme (Marascuilo-Busk).",
    "general effect: the series is the mean Brier score per ISO week of first probes "
    "of never-drawn items, without skipped, slow and truth-changed answers. A week "
    "with no probe is left out, never zero.",
    "general effect: a candidate week that leaves a phase empty is dropped. A "
    "repository is left out when it has fewer than 3 weeks or no week on one side of "
    "its start. A repository is also left out when two candidate weeks give one "
    "split, because the earlier candidate week has no probe.",
    "general effect: the statistic is the mean over repositories of the mean Brier "
    "score before the start minus the mean after it, one-sided. It reads the set "
    "component map of each repository only.",
)
_REFUSED = re.compile(r"\b(caus\w*|impact\w*|would)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Bin:
    p: float
    rate: float
    n: int
    band: tuple[float, float] | None
    interpretable: bool


@dataclass(frozen=True)
class Block:
    measure: str
    arm: str
    role: str
    n: int
    tasks: int
    refused: str | None
    brier: float | None
    skill: Mapping[str, tuple[float | None, int]]  # reference -> (BSS, forecasts)
    decomposition: tuple[float, float, float] | None
    bins: tuple[Bin, ...]
    ev: Evidence


@dataclass(frozen=True)
class Rule:
    name: str
    met: bool
    reasons: tuple[str, ...]
    claim: str


@dataclass(frozen=True)
class General:
    """Design 6.4: the multiple-baseline test across repositories."""

    p: float | None
    repositories: int  # included in the test
    left_out: Mapping[str, int]  # reason code -> repositories
    run: bool  # False without the committed seed and draw count


@dataclass(frozen=True)
class Scores:
    repo_id: str
    map_version: str | None
    refused: str | None
    blocks: tuple[Block, ...]
    counts: Mapping[tuple[str, str, str], int]  # (measure, reason, arm) -> count
    weeks: Mapping[str, int]
    tests: Mapping[str, tuple[float, int] | None]
    rules: tuple[Rule, ...]
    retest: tuple[int, float | None]  # V1: (pairs, agreement r)
    general: General


def suspended(tests: Mapping[str, tuple[float, int] | None]) -> bool:
    placebo = tests.get("pipeline_placebo")
    return placebo is not None and placebo[0] < ALPHA


def _num(x: float | None, digits: int = 3) -> str:
    return "null" if x is None else f"{round(x, digits) + 0.0:.{digits}f}"


def _block_lines(b: Block) -> list[str]:
    head = f"{b.measure} {b.role} {b.arm}: n {b.n}, tasks {b.tasks}"
    if b.refused is not None:
        return [f"{head}, not scored: {b.refused} (needs {MIN_FORECASTS})"]
    skill = "; ".join(f"{ref} {_num(v)} (n {n})" for ref, (v, n) in b.skill.items())
    lines = [f"{head}, Brier {_num(b.brier)}, coverage {b.ev.coverage}",
             f"  skill: {skill}"]  # fmt: skip
    if b.decomposition is not None:
        rel, res, unc = b.decomposition
        lines.append(f"  REL {_num(rel)} RES {_num(res)} UNC {_num(unc)}")
    for c in b.bins:
        band = "null" if c.band is None else f"{_num(c.band[0])}-{_num(c.band[1])}"
        read = "read" if c.interpretable else f"not read below {BIN_MIN[c.p]}"
        lines.append(f"  CORP {c.p:g}: rate {_num(c.rate)}, n {c.n}, band {band}, "
                     f"{read}")  # fmt: skip
    return lines


def _test_lines(tests: Mapping[str, tuple[float, int] | None]) -> list[str]:
    if not tests:
        return ["tests: not run. They need the committed seed and the draw count."]
    stop = suspended(tests)

    def one(name: str, r: tuple[float, int] | None) -> str:
        if stop and name in CONTRASTS:
            return f"{name} {SUSPENDED}"
        if r is None:
            return f"{name} p unknown"
        return f"{name} p {_num(r[0], 4)} over {r[1]} allocations"

    return ["tests: " + "; ".join(one(name, r) for name, r in tests.items())]


def _general_line(g: General) -> str:
    head = "general effect across repositories:"
    if not g.run:
        return f"{head} not run. It needs the committed seed and the draw count."
    left = "; ".join(f"{k} {n}" for k, n in g.left_out.items()) or "none"
    needs = "" if g.p is not None else " (needs 3)"
    return (
        f"{head} p {_num(g.p, 4)}, repositories {g.repositories}{needs}, "
        f"left out: {left}"
    )


def _lines(s: Scores) -> list[str]:
    lines = [f"Report for repository {s.repo_id}, set component map "
             f"{s.map_version or 'none'}"]  # fmt: skip
    if s.refused is not None:
        lines.append(f"no scores: {s.refused}")
    lines.append("claims: " + ", ".join(f"{k} {v}" for k, v in CLAIMS.items()))
    lines.append(
        "P1 is accuracy on structural items about components bound to tasks, and "
        "nothing more. F1 is calibration on forecasts of agent work."
    )
    weeks = ", ".join(f"{w} {n}" for w, n in s.weeks.items()) or "none"
    lines.append(f"first probes by week: {weeks}")
    excluded = "; ".join(" ".join((*k, str(n))) for k, n in s.counts.items())
    lines.append(f"excluded: {excluded or 'none'}")
    for b in s.blocks:
        lines += _block_lines(b)
    lines += _test_lines(s.tests)
    pairs, agreement = s.retest
    needs = "" if agreement is not None else f" (needs {MIN_PAIRS})"
    lines.append(f"V1 test-retest: pairs {pairs}, agreement r {_num(agreement)}{needs}")
    lines.append(_general_line(s.general))
    for r in s.rules:
        lines.append(f"rule {r.name} met: {r.claim}" if r.met
                     else f"rule {r.name} not met: {', '.join(r.reasons)}")  # fmt: skip
    notes = _notes(s)
    lines += ["assumptions:", *(f"  - {a}" for a in notes)] if notes else []
    return lines


def _notes(s: Scores) -> list[str]:
    notes = dict.fromkeys(a for b in s.blocks for a in b.ev.assumptions)
    if s.tests:
        notes.update(dict.fromkeys(TEST_ASSUMPTIONS))
    if s.refused is None:
        notes.update(dict.fromkeys(RETEST_ASSUMPTIONS))
    if s.general.run:
        notes.update(dict.fromkeys(GENERAL_ASSUMPTIONS))
    return list(notes)


def _guard(text: str, scores: Scores) -> str:
    found = _REFUSED.search(text)
    if found is not None and not any(r.met for r in scores.rules):
        word = found.group(0).lower()
        raise ValueError(f"report refused: {word!r} needs a met pre-registered rule")
    return text


def render(scores: Scores, out: TextIO) -> None:
    """Write the report, or nothing: a refused word without a met rule raises."""
    out.write(_guard("\n".join(_lines(scores)) + "\n", scores))


def to_json(scores: Scores) -> str:
    """The report as one JSON object, for scripts: the numbers, reason codes and
    assumptions of the text, unrounded, with the same refusal. A test that cannot be
    computed is null; `suspended` keeps the contrasts' numbers beside the suspension."""
    s = scores
    doc = {
        "repo_id": s.repo_id,
        "map_version": s.map_version,
        "refused": s.refused,
        "claims": CLAIMS,
        "blocks": [_block_json(b) for b in s.blocks],
        "excluded": [
            {"measure": m, "reason": r, "arm": a, "n": n}
            for (m, r, a), n in s.counts.items()
        ],
        "weeks": dict(s.weeks),
        "tests": {
            name: None if r is None else {"p": r[0], "allocations": r[1]}
            for name, r in s.tests.items()
        },
        "suspended": suspended(s.tests),
        "retest": {"pairs": s.retest[0], "r": s.retest[1]},
        "general": {
            "run": s.general.run,
            "p": s.general.p,
            "repositories": s.general.repositories,
            "left_out": dict(s.general.left_out),
        },
        "rules": [asdict(r) for r in s.rules],
        "assumptions": _notes(s),
    }
    return _guard(json.dumps(doc, sort_keys=True), s)


def _block_json(b: Block) -> dict[str, object]:
    rel_res_unc = None if b.decomposition is None else dict(
        zip(("rel", "res", "unc"), b.decomposition, strict=True))  # fmt: skip
    return {
        "measure": b.measure, "arm": b.arm, "role": b.role, "n": b.n,
        "tasks": b.tasks, "refused": b.refused, "brier": b.brier,
        "skill": {ref: {"bss": v, "n": n} for ref, (v, n) in b.skill.items()},
        "decomposition": rel_res_unc,
        "bins": [asdict(c) for c in b.bins],
        "coverage": b.ev.coverage, "warnings": list(b.ev.warnings),
    }  # fmt: skip
