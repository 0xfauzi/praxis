"""Supply from the owner's existing transcripts: how many prompts, changes,
repositories, structured decisions and sessions a week of the owner's agent work holds
(design Q1, Q3 and Q15). It reads `<claude_projects>/*/*.jsonl` through
praxis.transcript, the main thread only, as E08 did, and keeps counts, ISO week labels
and hashed repository ids. It writes nothing and prints no path, no prompt text and no
repository name.

A prompt's kind is known only on a CLI version that writes `origin`. A session in which
no prompt carries `origin` is counted as unrecorded: its prompts have an unknown kind,
and its segments are not classified, because a segment ends at the next human prompt.
A week that ends before the first prompt on disk is unobservable, not zero: the agent
can delete old transcripts. Codex rollouts are counted and not read: praxis has no
Codex transcript reader.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from praxis.gitdiff import repo_id, toplevel
from praxis.recorder import MUTATING_TOOLS
from praxis.transcript import Session, TranscriptError, read

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

REDUCER = "supply-v1"
DECISION_TOOLS = ("AskUserQuestion", "ExitPlanMode")
# The tool names that start a subagent, on 2.1.303 and on earlier CLI versions.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
EDIT_TOOLS = MUTATING_TOOLS - {"Bash"}
DEFAULT_WEEKS = 4
ASSUMPTIONS = (
    "Weeks are ISO weeks in UTC.",
    "Bash counts as a mutating tool, as in the recorder.",
    "A Bash call can change no file.",
    "Subagent transcripts are not read; a subagent call is counted apart.",
    "A git worktree is its own repository, as in the ledger.",
    "A week before the first prompt on disk is unobservable.",
    "An edit call counts by the suffix of its file, whatever the prompt's kind.",
)
SEGMENT_KINDS = ("edit", "bash_only", "subagent_only", "empty")
# The reader warnings for a line that it could not use: not JSON, or not the shape.
_UNUSABLE = ("malformed_line", "unexpected_shape")


@dataclass
class _Tally:
    """One week's counts while the files are read. Sets hold session ids, repository
    ids and cwds only in memory, because the measure is a count of distinct ones."""

    sessions: set[str] = field(default_factory=set)
    prompts: int = 0
    human: int = 0
    unrecorded: int = 0
    absent: int = 0
    segments: Counter[str] = field(default_factory=Counter)
    repos: set[str] = field(default_factory=set)
    outside: set[str] = field(default_factory=set)
    decisions: dict[str, Counter[int | None]] = field(
        default_factory=lambda: {name: Counter() for name in DECISION_TOOLS}
    )
    suffixes: Counter[str] = field(default_factory=Counter)

    def add(self, other: _Tally) -> None:
        self.sessions |= other.sessions
        self.prompts += other.prompts
        self.human += other.human
        self.unrecorded += other.unrecorded
        self.absent += other.absent
        self.segments += other.segments
        self.repos |= other.repos
        self.outside |= other.outside
        for name in DECISION_TOOLS:
            self.decisions[name] += other.decisions[name]
        self.suffixes += other.suffixes


@dataclass(frozen=True)
class Week:
    """Counts for one ISO week, or for the whole window. None is unknown, never 0."""

    week: str
    coverage: str  # full, partial or unobservable
    sessions: int | None
    prompts: int | None
    human: int | None
    origin_unrecorded: int | None
    origin_absent: int | None
    segments: dict[str, int] | None
    repositories: tuple[str, ...] | None  # hashed ids, never names
    cwd_outside_repository: int | None
    decisions: dict[str, dict[str, int]] | None
    edit_suffixes: dict[str, int] | None  # "(none)": no suffix, "(unknown)": no file

    @property
    def mutating(self) -> int | None:
        """The registration denominator: human prompts with a mutating call after
        them and before the next human prompt."""
        if self.segments is None:
            return None
        return self.segments["edit"] + self.segments["bash_only"]

    @property
    def empty_rate(self) -> float | None:
        if self.segments is None or not self.human:
            return None
        return self.segments["empty"] / self.human

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
            if k != "repositories"
        }
        out["repositories"] = (
            None if self.repositories is None else len(self.repositories)
        )
        out["repository_ids"] = (
            None if self.repositories is None else list(self.repositories)
        )
        out["mutating"] = self.mutating
        out["empty_rate"] = self.empty_rate
        return out


@dataclass(frozen=True)
class Supply:
    weeks: tuple[Week, ...]
    total: Week
    files: int
    unreadable: int
    unusable_lines: int
    duplicate_session_ids: int
    bad_timestamps: int
    first_prompt: str | None
    codex_files: int | None  # None: no Codex sessions directory

    def to_json(self) -> dict[str, Any]:
        return {
            "reducer": REDUCER,
            "claim_class": "derived",
            "population": "claude_projects/*/*.jsonl, main thread",
            "assumptions": list(ASSUMPTIONS),
            "files": self.files,
            "unreadable": self.unreadable,
            "unusable_lines": self.unusable_lines,
            "duplicate_session_ids": self.duplicate_session_ids,
            "bad_timestamps": self.bad_timestamps,
            "first_prompt": self.first_prompt,
            "codex": {"files": self.codex_files, "read": False, "reason": "no_reader"},
            "weeks": [w.to_json() for w in self.weeks],
            "total": self.total.to_json(),
        }


def measure(
    claude_projects: Path,
    codex_sessions: Path | None,
    weeks: int,
    *,
    now: dt.datetime | None = None,
) -> Supply:
    """Counts for the `weeks` ISO weeks that end with the week of `now` (UTC)."""
    if weeks < 1:
        raise ValueError("weeks must be 1 or more")
    clock = now if now is not None else dt.datetime.now(dt.UTC)
    if clock.tzinfo is None:
        raise ValueError("now must carry a time zone")
    labels = _labels(clock, weeks)
    reader = _Reader(set(labels))
    for path in sorted(claude_projects.glob("*/*.jsonl")):
        reader.file(path)
    first = min(reader.stamps, default=None)
    built = [_week(label, reader.tallies[label], _observable(label, first))
             for label in labels]  # fmt: skip
    total = _Tally()
    for label in labels:
        if _observable(label, first):
            total.add(reader.tallies[label])
    seen = [w for w in built if w.coverage != "unobservable"]
    ids = Counter(p.stem for p in claude_projects.glob("*/*.jsonl"))
    return Supply(
        weeks=tuple(built),
        total=_week("total", total, bool(seen), _coverage(built)),
        files=reader.files,
        unreadable=reader.unreadable,
        unusable_lines=reader.unusable,
        duplicate_session_ids=sum(n - 1 for n in ids.values()),
        bad_timestamps=reader.bad_ts,
        first_prompt=None if first is None else first.astimezone(dt.UTC).isoformat(),
        codex_files=_codex(codex_sessions),
    )


def _labels(now: dt.datetime, weeks: int) -> list[str]:
    today = now.astimezone(dt.UTC).date()
    monday = today - dt.timedelta(days=today.weekday())
    days = [monday - dt.timedelta(weeks=k) for k in range(weeks - 1, -1, -1)]
    return [_label(d) for d in days]


def _label(day: dt.date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _observable(label: str, first: dt.datetime | None) -> bool:
    if first is None:
        return False
    end = dt.datetime.fromisocalendar(int(label[:4]), int(label[6:]), 1)
    return end.replace(tzinfo=dt.UTC) + dt.timedelta(weeks=1) > first


def _coverage(weeks: list[Week]) -> str:
    kinds = {w.coverage for w in weeks}
    return kinds.pop() if len(kinds) == 1 else "partial"


def _week(label: str, t: _Tally, observable: bool, coverage: str | None = None) -> Week:
    if not observable:
        return Week(label, "unobservable", None, None, None, None, None, None, None,
                    None, None, None)  # fmt: skip
    kind_known = t.unrecorded == 0 or t.prompts > t.unrecorded
    if coverage is None:
        coverage = "full" if t.unrecorded == 0 else "partial"
        coverage = coverage if kind_known else "unobservable"
    return Week(
        week=label,
        coverage=coverage,
        sessions=len(t.sessions),
        prompts=t.prompts,
        human=t.human if kind_known else None,
        origin_unrecorded=t.unrecorded,
        origin_absent=t.absent,
        segments={k: t.segments[k] for k in SEGMENT_KINDS} if kind_known else None,
        repositories=tuple(sorted(t.repos)),
        cwd_outside_repository=len(t.outside),
        decisions={name: _options(t.decisions[name]) for name in DECISION_TOOLS},
        edit_suffixes=dict(sorted(t.suffixes.items())),
    )


def _options(found: Counter[int | None]) -> dict[str, int]:
    """Calls, then the count by option count; an unscorable call has no option count."""
    out = {"calls": sum(found.values()), "unscorable": found[None]}
    out.update({f"options_{k}": n for k, n in sorted(found.items()) if k is not None})
    return out


def _codex(codex_sessions: Path | None) -> int | None:
    if codex_sessions is None or not codex_sessions.is_dir():
        return None
    return sum(1 for _ in codex_sessions.rglob("rollout-*.jsonl"))


class _Reader:
    def __init__(self, labels: set[str]) -> None:
        self.tallies = {label: _Tally() for label in labels}
        self.stamps: list[dt.datetime] = []
        self.files = self.unreadable = self.unusable = self.bad_ts = 0
        self.repo_of: dict[str, str | None] = {}

    def file(self, path: Path) -> None:
        self.files += 1
        try:
            session = read(path)
            raw = path.read_bytes().split(b"\n")
        except (TranscriptError, OSError):
            self.unreadable += 1
            return
        self.unusable += sum(w.startswith(_UNUSABLE) for w in session.warnings)
        weeks = {p.index: self._week_of(p.ts, keep=True) for p in session.prompts}
        recorded = any(p.origin_kind != "absent" for p in session.prompts)
        for p in session.prompts:
            tally = self.tallies.get(weeks[p.index] or "")
            if tally is not None:
                self._prompt(tally, session.session_id, p.origin_kind, recorded)
                self._where(tally, _cwd(raw, p.index))
        if recorded:
            for index, kind in segments(session):
                tally = self.tallies.get(weeks[index] or "")
                if tally is not None:
                    tally.segments[kind] += 1
        self._decisions(session)
        self._edits(session)

    def _week_of(self, ts: str, *, keep: bool = False) -> str | None:
        """The ISO week label; a prompt's time also goes into `stamps` (`keep`)."""
        try:
            when = dt.datetime.fromisoformat(ts)
        except ValueError:
            when = None
        if when is None or when.tzinfo is None:
            self.bad_ts += 1
            return None
        if keep:
            self.stamps.append(when)
        return _label(when.astimezone(dt.UTC).date())

    @staticmethod
    def _prompt(tally: _Tally, session_id: str, kind: str, recorded: bool) -> None:
        tally.sessions.add(session_id)
        tally.prompts += 1
        if not recorded:
            tally.unrecorded += 1
        elif kind == "absent":
            tally.absent += 1
        elif kind == "human":
            tally.human += 1

    def _where(self, tally: _Tally, cwd: str | None) -> None:
        if cwd is None:
            tally.outside.add("")  # a prompt line with no cwd: one unknown place
            return
        if cwd not in self.repo_of:
            top = toplevel(cwd) if Path(cwd).is_dir() else None
            self.repo_of[cwd] = None if top is None else repo_id(top)
        found = self.repo_of[cwd]
        if found is None:
            tally.outside.add(cwd)
        else:
            tally.repos.add(found)

    def _edits(self, session: Session) -> None:
        """Edit calls by file suffix: the language inventory of design 04 section 8."""
        for use in session.tools:
            if use.name not in EDIT_TOOLS:
                continue
            tally = self.tallies.get(self._week_of(use.ts) or "")
            if tally is not None:
                key = "(unknown)" if use.suffix is None else use.suffix or "(none)"
                tally.suffixes[key] += 1

    def _decisions(self, session: Session) -> None:
        for use in session.tools:
            if use.name not in DECISION_TOOLS:
                continue
            tally = self.tallies.get(self._week_of(use.ts) or "")
            if tally is not None:
                tally.decisions[use.name][use.option_count] += 1


def segments(session: Session) -> list[tuple[int, str]]:
    """(line index of the human prompt, kind) for each human prompt in a session that
    records `origin`. The segment runs to the next human prompt: a prompt of another
    kind does not end it (design 5.3)."""
    humans = [p.index for p in session.prompts if p.origin_kind == "human"]
    ends = [*humans[1:], None]
    return [
        (start, _kind(_names(session, start, end)))
        for start, end in zip(humans, ends, strict=True)
    ]


def _names(session: Session, start: int, end: int | None) -> list[str]:
    return [
        u.name
        for u in session.tools
        if u.index > start and (end is None or u.index < end)
    ]


def _kind(names: Iterable[str]) -> str:
    used = set(names)  # a presence test: which kinds of tool ran at all
    if used & EDIT_TOOLS:
        return "edit"
    if "Bash" in used:
        return "bash_only"
    return "subagent_only" if used & SUBAGENT_TOOLS else "empty"


def _cwd(raw: list[bytes], index: int) -> str | None:
    """The `cwd` of one transcript line; the line was already parsed by the reader."""
    try:
        entry = json.loads(raw[index])
    except (ValueError, IndexError, RecursionError):
        return None
    cwd = entry.get("cwd") if isinstance(entry, dict) else None
    return cwd if isinstance(cwd, str) else None


# -- the command ----------------------------------------------------------------------

_ROWS: tuple[tuple[str, str, Callable[[Week], object]], ...] = (
    ("sessions", "Claude Code sessions with at least one prompt in the week.",
     lambda w: w.sessions),
    ("prompts", "User text entries in the main thread, of every kind.",
     lambda w: w.prompts),
    ("human prompts", "Prompts with origin.kind human. Unknown where no session "
     "recorded origin.", lambda w: w.human),
    ("origin unrecorded", "Prompts in sessions whose CLI version wrote no origin. "
     "Their kind is unknown.", lambda w: w.origin_unrecorded),
    ("origin absent", "Prompts with no origin in sessions that wrote it, such as "
     "command output.", lambda w: w.origin_absent),
    ("mutating", "Human prompts with a mutating call before the next human prompt. "
     "This is the registration denominator.", lambda w: w.mutating),
    ("bash only", "Mutating human prompts whose only mutating calls were Bash.",
     lambda w: _seg(w, "bash_only")),
    ("subagent only", "Human prompts with a subagent call and no mutating call. The "
     "main thread cannot show a change.", lambda w: _seg(w, "subagent_only")),
    ("empty", "Human prompts with no mutating call and no subagent call before the "
     "next human prompt.", lambda w: _seg(w, "empty")),
    ("empty rate", "Empty prompts divided by human prompts (design Q15).",
     lambda w: None if w.empty_rate is None else f"{w.empty_rate:.2f}"),
    ("repositories", "Distinct repositories, by the hashed git root of the cwd of "
     "each prompt.", lambda w: None if w.repositories is None else len(w.repositories)),
    ("cwd outside", "Distinct cwds of prompts that are not in a git repository on "
     "this disk.", lambda w: w.cwd_outside_repository),
    ("AskUserQuestion", "Choice questions from the agent, then the count by number "
     "of options.", lambda w: _dec(w, "AskUserQuestion")),
    ("ExitPlanMode", "Plans offered for approval, then the count by number of "
     "options.", lambda w: _dec(w, "ExitPlanMode")),
    ("edit suffixes", "Edit, Write and other file-tool calls by the suffix of the "
     "file. Praxis keeps no path.", lambda w: _suffixes(w)),
)  # fmt: skip


def _seg(w: Week, kind: str) -> int | None:
    return None if w.segments is None else w.segments[kind]


def _suffixes(w: Week) -> str | None:
    if w.edit_suffixes is None:
        return None
    ranked = sorted(w.edit_suffixes.items(), key=lambda kv: (-kv[1], kv[0]))
    return " ".join(f"{k}:{n}" for k, n in ranked) or "0"


def _dec(w: Week, name: str) -> str | None:
    if w.decisions is None:
        return None
    found = w.decisions[name]
    parts = [f"{k.removeprefix('options_')}:{n}" for k, n in found.items()
             if k.startswith("options_")]  # fmt: skip
    if found["unscorable"]:
        parts.append(f"unscorable:{found['unscorable']}")
    return f"{found['calls']} ({' '.join(parts)})" if parts else str(found["calls"])


def show(home: Path, weeks: str | None, as_json: bool, out: TextIO) -> int:
    """`praxis show supply`: the transcripts under the owner's home, read only."""
    count = DEFAULT_WEEKS if weeks is None else _weeks(weeks)
    if count is None:
        out.write("praxis show supply: write a whole number of 1 or more after "
                  "--weeks.\n")  # fmt: skip
        return 2
    found = measure(home / ".claude" / "projects", home / ".codex" / "sessions", count)
    if as_json:
        out.write(json.dumps(found.to_json(), indent=2) + "\n")
        return 0
    render(found, out)
    return 0


def _weeks(text: str) -> int | None:
    return int(text) if text.isdigit() and int(text) >= 1 else None


def render(found: Supply, out: TextIO) -> None:
    columns = [*found.weeks, found.total]
    head = ["measure", *(w.week for w in columns)]
    rows = [[name, *(_cell(get(w)) for w in columns)] for name, _, get in _ROWS]
    rows.insert(0, ["coverage", *(w.coverage for w in columns)])
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    out.write(
        f"Supply in the last {len(found.weeks)} ISO weeks (UTC). Transcript files "
        f"read: {found.files}. Praxis stored nothing.\n"
    )
    out.write(
        f"Unreadable files: {found.unreadable}. Unusable lines: "
        f"{found.unusable_lines}. Codex rollouts: {_cell(found.codex_files)}, "
        "not read.\n\n"
    )
    for row in [head, *rows]:
        cells = (c.ljust(n) for c, n in zip(row, widths, strict=True))
        out.write("  ".join(cells).rstrip() + "\n")
    out.write("\n")
    out.write("coverage: full, partial, or unobservable when the kind or the week is "
              "unknown.\n")  # fmt: skip
    for name, meaning, _ in _ROWS:
        out.write(f"{name}: {meaning}\n")


def _cell(value: object) -> str:
    return "unknown" if value is None else str(value)
